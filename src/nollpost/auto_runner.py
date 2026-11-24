"""Headless auto-triage selection, locking, and execution runner."""

from __future__ import annotations

import asyncio
import fcntl
import subprocess
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, Optional

from nollpost.classifier import BASE_SECURITY_DOMAINS, SECURITY_KEYWORDS_RE, _domain_matches
from nollpost.config import TriageConfig
from nollpost.executor import BatchExecutor
from nollpost.mail_bridge import AppleMailBridge, is_gmail_all_mail_destination
from nollpost.models import ClassifiedItem
from nollpost.scanner import run_scan
from nollpost.store import TriageStore


def _is_mail_running() -> bool:
    try:
        res = subprocess.run(["pgrep", "-x", "Mail"], capture_output=True, text=True, check=False)
        return res.returncode == 0
    except Exception:
        return False


@contextmanager
def acquire_run_lock(data_dir: Path, *, wait_seconds: float = 0.0) -> Iterator[bool]:
    """Acquire exclusive flock on `<data_dir>/run.lock`, optionally waiting up to `wait_seconds`."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "run.lock"
    fh = lock_path.open("a+", encoding="utf-8")
    acquired = False
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    acquired = False
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


@asynccontextmanager
async def async_acquire_run_lock(data_dir: Path, *, wait_seconds: float = 0.0) -> AsyncIterator[bool]:
    """Non-blocking asyncio variant of `acquire_run_lock`."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "run.lock"
    fh = lock_path.open("a+", encoding="utf-8")
    acquired = False
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    acquired = False
                    break
                await asyncio.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _describe_active_rules(config: TriageConfig, store: TriageStore) -> dict[str, Any]:
    sender_count = len(store.list_sender_memory(filter_kind="auto_archive")) if config.auto_archive_sender_rules else 0
    return {
        "sender_auto_archive": sender_count,
        "auto_decline_consulting": bool(config.auto_decline_consulting),
        "auto_archive_transactional": bool(config.auto_archive_transactional),
        "auto_archive_bot_noise": bool(config.auto_archive_bot_noise),
        "auto_archive_always_archive": bool(config.auto_archive_always_archive),
        "auto_archive_recent_unsubs": bool(config.auto_archive_recent_unsubs),
    }


def _is_gmail_item(item: ClassifiedItem) -> bool:
    return is_gmail_all_mail_destination(item.archive_mailbox) or "gmail" in item.header.account.lower()


def select_auto_actions(
    items: list[ClassifiedItem],
    config: TriageConfig,
    store: TriageStore,
) -> tuple[list[tuple[ClassifiedItem, str]], list[str]]:
    """Select eligible unread INBOX items and concrete actions for autopilot."""
    selected: list[tuple[ClassifiedItem, str]] = []
    warnings: list[str] = []
    warned_ui_menu = False

    for item in items:
        h = item.header
        if h.is_read or str(h.mailbox or "").strip().upper() != "INBOX":
            continue
        if config.auto_skip_security_alerts:
            if item.subcategory == "Security Alert":
                continue
            combined = f"{h.subject}\n{h.sender}\n{item.extracted.plain_excerpt}".lower()
            if SECURITY_KEYWORDS_RE.search(combined) or _domain_matches(h.sender_domain, BASE_SECURITY_DOMAINS):
                continue
        if config.auto_skip_pdf_invoices and item.extracted.pdf_attachments:
            continue

        chosen_action: Optional[str] = None
        if config.auto_archive_sender_rules and item.learned_preference == "auto_archive":
            chosen_action = "archive"
        elif config.auto_archive_always_archive and item.learned_preference == "archive":
            chosen_action = "archive"
        elif config.auto_decline_consulting and item.category == "consulting" and item.subcategory == "Consulting Mismatch":
            chosen_action = "decline_and_archive" if item.extracted.consulting_links.decline_url else "send_decline_and_archive"
        elif config.auto_archive_transactional and item.subcategory == "Transactional":
            chosen_action = "archive"
        elif config.auto_archive_bot_noise and item.subcategory == "Bot Noise":
            chosen_action = "archive"
        elif config.auto_archive_recent_unsubs and "Unsubbed recently" in item.badges:
            chosen_action = "archive"

        if not chosen_action:
            continue

        if config.gmail_archive_strategy == "ui_menu" and _is_gmail_item(item):
            if not warned_ui_menu:
                warnings.append(
                    "Skipped Gmail auto-triage because gmail_archive_strategy='ui_menu' opens Mail windows; "
                    "switch to 'background' or 'tag_staging' for unattended Gmail archiving."
                )
                warned_ui_menu = True
            continue

        if store.has_auto_failure(h.account, h.internal_id, h.rfc_message_id, chosen_action):
            continue

        selected.append((item, chosen_action))

    return selected, warnings


async def async_run_auto_triage(
    config: TriageConfig,
    store: TriageStore,
    bridge: AppleMailBridge,
    *,
    dry_run: bool = False,
    force: bool = False,
    headless: bool = True,
    allow_private_urls: bool = False,
    mail_running_checker: Optional[Callable[[], bool]] = None,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Run one headless auto-triage pass and return structured JSON-ready summary."""
    status_file = config.data_dir / "auto_status.json"
    checker = mail_running_checker or _is_mail_running
    if not config.auto_enabled and not dry_run and not force:
        return {
            "status": "skipped",
            "reason": "disabled",
            "unseen_count": store.get_auto_digest()["unseen_count"],
        }
    if not checker():
        return {
            "status": "skipped",
            "reason": "mail_not_running",
            "unseen_count": store.get_auto_digest()["unseen_count"],
        }

    def _run_locked() -> dict[str, Any]:
        return _execute_auto_pass(
            config=config,
            store=store,
            bridge=bridge,
            dry_run=dry_run,
            headless=headless,
            allow_private_urls=allow_private_urls,
            status_file=status_file,
        )

    if acquire_lock:
        with acquire_run_lock(config.data_dir) as got_lock:
            if not got_lock:
                return {
                    "status": "skipped",
                    "reason": "busy",
                    "unseen_count": store.get_auto_digest()["unseen_count"],
                }
            return await _run_locked()
    return await _run_locked()


async def _execute_auto_pass(
    *,
    config: TriageConfig,
    store: TriageStore,
    bridge: AppleMailBridge,
    dry_run: bool,
    headless: bool,
    allow_private_urls: bool,
    status_file: Path,
) -> dict[str, Any]:
    try:
        snap = await asyncio.to_thread(
            run_scan,
            config,
            store,
            bridge,
            mode="inbox",
            sync_mail=True,
        )
        pairs, select_warnings = select_auto_actions(snap.items, config, store)
        warnings = [*snap.warnings, *select_warnings]
        active_rules = _describe_active_rules(config, store)
        if dry_run:
            return {
                "status": "dry_run",
                "warnings": warnings,
                "active_rules": active_rules,
                "planned": [
                    {
                        "uid": it.uid,
                        "account": it.header.account,
                        "sender": it.header.sender,
                        "subject": it.header.subject,
                        "action": act,
                        "reason": it.reason,
                    }
                    for it, act in pairs
                ],
                "unseen_count": store.get_auto_digest()["unseen_count"],
            }

        if not pairs:
            summary = {"archived": 0, "declined": 0, "failed": 0}
            digest = store.record_auto_run_status(
                status="ok",
                summary=summary,
                warnings=warnings,
                status_file=status_file,
            )
            return {
                "status": "ok",
                "executed": 0,
                "summary": summary,
                "warnings": warnings,
                "active_rules": active_rules,
                "unseen_count": digest["unseen_count"],
                "last_run_at": digest["last_run_at"],
            }

        executor = BatchExecutor(
            config=config,
            store=store,
            bridge=bridge,
            allow_private_urls=allow_private_urls,
        )
        acct_addrs = {a.name: a.addresses for a in snap.accounts}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        results = await executor.execute_batch(
            pairs,
            acct_addrs,
            headless=headless,
            source="auto",
            run_id_prefix=f"auto-{stamp}-",
        )
        summary = store._summarize_auto_rows([r.to_dict() for r in results])
        digest = store.record_auto_run_status(
            status="ok",
            summary=summary,
            warnings=warnings,
            status_file=status_file,
        )
        return {
            "status": "ok",
            "executed": len(results),
            "summary": summary,
            "warnings": warnings,
            "active_rules": active_rules,
            "results": [r.to_dict() for r in results],
            "unseen_count": digest["unseen_count"],
            "last_run_at": digest["last_run_at"],
        }
    except Exception as exc:
        digest = store.record_auto_run_status(
            status="error",
            summary={"archived": 0, "declined": 0, "failed": 0},
            error=str(exc),
            status_file=status_file,
        )
        return {
            "status": "error",
            "error": str(exc),
            "unseen_count": digest["unseen_count"],
            "last_run_at": digest["last_run_at"],
        }


def run_auto_triage(
    config: TriageConfig,
    store: TriageStore,
    bridge: AppleMailBridge,
    *,
    dry_run: bool = False,
    force: bool = False,
    headless: bool = True,
    allow_private_urls: bool = False,
    mail_running_checker: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Synchronous wrapper around `async_run_auto_triage` for CLI invocations."""
    return asyncio.run(
        async_run_auto_triage(
            config,
            store,
            bridge,
            dry_run=dry_run,
            force=force,
            headless=headless,
            allow_private_urls=allow_private_urls,
            mail_running_checker=mail_running_checker,
            acquire_lock=True,
        )
    )
