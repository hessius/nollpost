"""Shared scan and briefing builder used by both CLI and local Web UI."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from nollpost.classifier import classify_email
from nollpost.config import TriageConfig
from nollpost.extractor import extract_email_info
from nollpost.mail_bridge import AppleMailBridge
from nollpost.models import (
    AccountInfo,
    ClassifiedItem,
    ConsultingLinks,
    ExtractedEmailInfo,
    MailHeader,
    PdfAttachment,
    UnsubTarget,
)
from nollpost.patterns_loader import UnsubPatterns, load_unsub_patterns
from nollpost.store import TriageStore

EXTRACTOR_VERSION = "v5"


def _deserialize_extracted(data: dict[str, Any]) -> ExtractedEmailInfo:
    ut = data.get("unsub_target")
    cl = data.get("consulting_links") or {}
    return ExtractedEmailInfo(
        unsub_target=UnsubTarget(**ut) if ut else None,
        fallback_unsub_url=data.get("fallback_unsub_url"),
        mailto_unsub=data.get("mailto_unsub"),
        newsletter_signals=list(data.get("newsletter_signals") or []),
        consulting_links=ConsultingLinks(**cl),
        pertinent_url=data.get("pertinent_url"),
        pertinent_label=data.get("pertinent_label"),
        pdf_attachments=[PdfAttachment(**p) for p in (data.get("pdf_attachments") or [])],
        headers_snapshot=data.get("headers_snapshot", ""),
        footer_html_snapshot=data.get("footer_html_snapshot", ""),
        candidate_links=list(data.get("candidate_links") or []),
        plain_excerpt=data.get("plain_excerpt", ""),
    )


@dataclass
class ScanSnapshot:
    mode: str
    accounts: list[AccountInfo]
    items: list[ClassifiedItem]
    tray: list[dict[str, Any]]
    missed: list[dict[str, Any]]
    stats: dict[str, int]
    briefing: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    consulting_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "accounts": [a.to_dict() for a in self.accounts],
            "items": [i.to_dict() for i in self.items],
            "tray": self.tray,
            "missed": self.missed,
            "stats": self.stats,
            "briefing": self.briefing,
            "warnings": self.warnings,
            "consulting_enabled": self.consulting_enabled,
        }


def _patterns_cache_sig(config: TriageConfig) -> str:
    p = config.patterns_path
    if p is not None and p.exists():
        return f"{p.resolve()}:{p.stat().st_mtime_ns}"
    return "builtin"


def _extract_cached(
    bridge: AppleMailBridge,
    store: TriageStore,
    header: MailHeader,
    config: TriageConfig,
    bypass_cache: bool = False,
    *,
    patterns: UnsubPatterns | None = None,
    patterns_sig: str | None = None,
) -> ExtractedEmailInfo:
    packs_key = ",".join(sorted(config.enabled_packs))
    wanted_key = ",".join(sorted(config.wanted_domains))
    psig = patterns_sig if patterns_sig is not None else _patterns_cache_sig(config)
    cache_key = (
        f"{EXTRACTOR_VERSION}:c={int(config.consulting_enabled)}:p={packs_key}:w={wanted_key}:pat={psig}:"
        f"{header.account}:{header.internal_id}:{header.rfc_message_id}"
    )
    if not bypass_cache:
        cached = store.get_cached_extraction(cache_key)
        if cached is not None:
            return _deserialize_extracted(cached)
    raw = bridge.get_raw_source(header.account, header.mailbox, header.internal_id)
    extracted = extract_email_info(
        raw,
        sender_domain=header.sender_domain,
        consulting_enabled=config.consulting_enabled,
        wanted_domains=config.wanted_domains,
        enabled_packs=config.enabled_packs,
        patterns=patterns or load_unsub_patterns(config.patterns_path),
    )
    payload = extracted.to_dict()
    payload["headers_snapshot"] = extracted.headers_snapshot
    payload["footer_html_snapshot"] = extracted.footer_html_snapshot
    store.set_cached_extraction(cache_key, payload)
    return extracted


def _build_briefing(accounts: list[AccountInfo], items: list[ClassifiedItem]) -> dict[str, Any]:
    per_account: dict[str, dict[str, int]] = {}
    for acct in accounts:
        acct_items = [i for i in items if i.header.account == acct.name]
        per_account[acct.name] = {
            "total": len(acct_items),
            "unread": sum(1 for i in acct_items if not i.header.is_read),
        }

    by_category = Counter(i.category for i in items)
    attention_highlights = [
        {
            "uid": i.uid,
            "account": i.header.account,
            "subcategory": i.subcategory,
            "sender": i.header.sender,
            "subject": i.header.subject,
            "is_read": i.header.is_read,
            "default_action": i.default_action,
            "pertinent_url": i.extracted.pertinent_url,
        }
        for i in items
        if i.category in ("attention", "updates") or (i.category == "consulting" and i.subcategory == "Consulting Match")
    ]
    return {
        "total_items": len(items),
        "per_account": per_account,
        "by_category": dict(by_category),
        "attention_highlights": attention_highlights[:25],
    }


def run_scan(
    config: TriageConfig,
    store: TriageStore,
    bridge: AppleMailBridge,
    mode: str = "inbox",
    bypass_cache: bool = False,
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
) -> ScanSnapshot:
    """Scan all configured Mail.app accounts in either 'inbox' or 'catchup' mode."""
    def _emit(phase: str, message: str, done: int = 0, total: int = 0, account: str = "") -> None:
        if on_progress is not None:
            on_progress({
                "active": True,
                "mode": mode,
                "phase": phase,
                "message": message,
                "done": done,
                "total": total,
                "account": account,
            })

    _emit("accounts", "Listing Mail.app accounts…")
    accounts = bridge.list_accounts()
    total_accounts = len(accounts)
    items: list[ClassifiedItem] = []
    warnings: list[str] = []
    unsub_patterns = load_unsub_patterns(config.patterns_path)
    patterns_sig = _patterns_cache_sig(config)

    if mode == "catchup":
        for acct_idx, acct in enumerate(accounts, start=1):
            _emit(
                "mailbox",
                f"Scanning {acct.name}/{acct.archive_mailbox} ({acct_idx}/{total_accounts})…",
                done=acct_idx - 1,
                total=total_accounts,
                account=acct.name,
            )
            try:
                headers = bridge.scan_mailbox_metadata(
                    acct.name,
                    mailbox=acct.archive_mailbox,
                    limit=config.catchup_scan_limit,
                    include_attachments=False,
                    days_back=config.catchup_days_back,
                )
            except Exception as exc:
                warnings.append(f"{acct.name}/{acct.archive_mailbox}: {exc}")
                continue

            grouped: dict[str, list[MailHeader]] = defaultdict(list)
            for h in headers:
                key = store.normalize_sender_key(h.sender_email, h.sender_domain)
                if key:
                    grouped[key].append(h)

            repeat_groups = sorted(
                ((k, grp) for k, grp in grouped.items() if len(grp) >= 2),
                key=lambda pair: len(pair[1]),
                reverse=True,
            )
            total_groups = len(repeat_groups)
            for grp_idx, (_key, grp) in enumerate(repeat_groups, start=1):
                if grp_idx == 1 or grp_idx == total_groups or grp_idx % 5 == 0:
                    _emit(
                        "extract",
                        f"Inspecting repeat senders in {acct.name} ({grp_idx}/{total_groups})…",
                        done=grp_idx,
                        total=total_groups,
                        account=acct.name,
                    )
                sample = grp[0]
                sender_mem = store.get_sender_memory(sample.sender_email, sample.sender_domain)
                if sender_mem and (sender_mem.unsubscribed_at or sender_mem.keep_subscribed):
                    continue
                try:
                    extracted = _extract_cached(
                        bridge,
                        store,
                        sample,
                        config=config,
                        bypass_cache=bypass_cache,
                        patterns=unsub_patterns,
                        patterns_sig=patterns_sig,
                    )
                except Exception:
                    continue
                item = classify_email(
                    sample,
                    extracted,
                    config=config,
                    archive_mailbox=acct.archive_mailbox,
                    sender_mem=sender_mem,
                    catchup_mode=True,
                )
                # Strict catch-up safety: never surface non-newsletters (Kivra, Fortnox, security, human GitHub, etc.)
                if item.category != "newsletters" or item.subcategory != "Newsletter":
                    continue
                item.group_count = len(grp)
                item.badges.insert(0, f"{len(grp)}× in {acct.archive_mailbox}")
                items.append(item)
    else:
        active_uids: set[str] = set()
        any_inbox_scanned = False
        for acct_idx, acct in enumerate(accounts, start=1):
            _emit(
                "mailbox",
                f"Scanning {acct.name}/{acct.inbox_mailbox} ({acct_idx}/{total_accounts})…",
                done=acct_idx - 1,
                total=total_accounts,
                account=acct.name,
            )
            try:
                headers = bridge.scan_mailbox_metadata(
                    acct.name,
                    mailbox=acct.inbox_mailbox,
                    limit=config.max_inbox_messages,
                    include_attachments=True,
                )
                any_inbox_scanned = True
            except Exception as exc:
                warnings.append(f"{acct.name}/{acct.inbox_mailbox}: {exc}")
                continue

            sender_counts = Counter(
                store.normalize_sender_key(h.sender_email, h.sender_domain)
                for h in headers
                if h.sender_email or h.sender_domain
            )
            total_headers = len(headers)
            for msg_idx, h in enumerate(headers, start=1):
                if msg_idx == 1 or msg_idx == total_headers or msg_idx % 5 == 0:
                    _emit(
                        "extract",
                        f"Classifying {acct.name}/{acct.inbox_mailbox} ({msg_idx}/{total_headers})…",
                        done=msg_idx,
                        total=total_headers,
                        account=acct.name,
                    )
                uid = f"{h.account}:{h.mailbox}:{h.internal_id}"
                active_uids.add(uid)
                try:
                    extracted = _extract_cached(
                        bridge,
                        store,
                        h,
                        config=config,
                        bypass_cache=bypass_cache,
                        patterns=unsub_patterns,
                        patterns_sig=patterns_sig,
                    )
                except Exception:
                    extracted = ExtractedEmailInfo()
                sender_mem = store.get_sender_memory(h.sender_email, h.sender_domain)
                item = classify_email(
                    h,
                    extracted,
                    config=config,
                    archive_mailbox=acct.archive_mailbox,
                    sender_mem=sender_mem,
                    catchup_mode=False,
                )
                skey = store.normalize_sender_key(h.sender_email, h.sender_domain)
                item.group_count = sender_counts.get(skey, 1)
                items.append(item)

        if any_inbox_scanned and not warnings:
            store.prune_tray_not_in_inbox(active_uids)

    _emit("done", "Scan complete", done=total_accounts, total=total_accounts)
    return ScanSnapshot(
        mode=mode,
        accounts=accounts,
        items=items,
        tray=store.list_active_tray(),
        missed=store.list_missed_unsubscribes(include_resolved=False),
        stats=store.get_stats(),
        briefing=_build_briefing(accounts, items),
        warnings=warnings,
        consulting_enabled=config.consulting_enabled,
    )
