"""Batch execution orchestrator enforcing Archive-only safety, unsub verification, and preference learning."""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from collections import defaultdict
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from nollpost.config import TriageConfig
from nollpost.extractor import extract_pdf_attachment
from nollpost.mail_bridge import AppleMailBridge
from nollpost.models import ClassifiedItem, ExecutionItemResult, PlaywrightOutcome
from nollpost.patterns_loader import load_unsub_patterns
from nollpost.playwright_runner import is_public_http_url, run_playwright_action, try_rfc8058_one_click
from nollpost.store import TriageStore

ARCHIVE_CHUNK_SIZE = 8


class BatchExecutor:
    """Executes user-confirmed triage batches safely."""

    def __init__(
        self,
        config: TriageConfig,
        store: TriageStore,
        bridge: AppleMailBridge,
        allow_private_urls: bool = False,
    ):
        self.config = config
        self.store = store
        self.bridge = bridge
        self.allow_private_urls = allow_private_urls

    def build_confirmation_preview(
        self,
        items_with_actions: list[tuple[ClassifiedItem, str]],
        account_addresses: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Produce a complete summary of every side-effect before confirmation."""
        counts: dict[str, int] = defaultdict(int)
        archive_targets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        links_to_open: list[dict[str, str]] = []
        pdfs_to_open: list[dict[str, str]] = []
        unsubs: list[dict[str, str]] = []
        declines: list[dict[str, str]] = []
        draft_declines: list[dict[str, str]] = []
        send_declines: list[dict[str, str]] = []

        seen_unsub_senders: set[str] = set()
        for item, action in items_with_actions:
            if action == "no_action":
                continue
            counts[action] += 1
            h = item.header
            if action in ("archive", "archive_and_unsub", "decline_and_archive", "send_decline_and_archive"):
                archive_targets[h.account][item.archive_mailbox] += 1
            if action == "read_open_link" and item.extracted.pertinent_url:
                links_to_open.append({"subject": h.subject, "sender": h.sender, "url": item.extracted.pertinent_url})
            elif action == "read_show_invoice" and item.extracted.pdf_attachments:
                pdfs_to_open.append({"subject": h.subject, "filename": item.extracted.pdf_attachments[0].filename})
            elif action in ("archive_and_unsub", "unsub_only") and item.extracted.unsub_target:
                sender_key = self.store.normalize_sender_key(h.sender_email, h.sender_domain) or f"url:{item.extracted.unsub_target.url}"
                if sender_key not in seen_unsub_senders:
                    seen_unsub_senders.add(sender_key)
                    unsubs.append(
                        {
                            "sender": h.sender,
                            "method": item.extracted.unsub_target.kind,
                            "url": item.extracted.unsub_target.url,
                            "account": h.account,
                        }
                    )
            elif action == "decline_and_archive":
                declines.append({"sender": h.sender, "subject": h.subject, "url": item.extracted.consulting_links.decline_url or ""})
            elif action == "draft_decline":
                draft_declines.append({"sender": h.sender, "subject": h.subject})
            elif action == "send_decline_and_archive":
                send_declines.append({"sender": h.sender, "subject": h.subject})

        return {
            "total_actions": sum(counts.values()),
            "deleted_count": 0,
            "counts": dict(counts),
            "archive_targets": {acct: dict(boxes) for acct, boxes in archive_targets.items()},
            "links_to_open": links_to_open,
            "pdfs_to_open": pdfs_to_open,
            "unsubs": unsubs,
            "declines": declines,
            "draft_declines": draft_declines,
            "send_declines": send_declines,
            "boilerplate_preview": self.config.consulting_decline_boilerplate if send_declines else "",
        }

    def _learn_if_opted_out_of_unsub(self, item: ClassifiedItem, chosen_action: str) -> None:
        """Remember preference ONLY when default was archive_and_unsub and user explicitly opted out."""
        if (
            item.category == "newsletters"
            and item.default_action == "archive_and_unsub"
            and item.repeat_offender_days is None
            and item.extracted.unsub_target is not None
            and chosen_action in ("archive", "mark_read", "read_open_link")
        ):
            self.store.record_sender_preference(
                sender_email=item.header.sender_email,
                sender_domain=item.header.sender_domain,
                display_name=item.header.sender,
                preferred_action=chosen_action,
                keep_subscribed=True,
            )

    async def execute_batch(
        self,
        items_with_actions: list[tuple[ClassifiedItem, str]],
        account_addresses: dict[str, list[str]],
        headless: bool = True,
        on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> list[ExecutionItemResult]:
        run_id = uuid.uuid4().hex[:10]
        results: list[ExecutionItemResult] = []
        stage1_total = sum(1 for _, act in items_with_actions if act != "no_action")
        mail_est = sum(
            1
            for _, act in items_with_actions
            if act in ("mark_read", "read_open_link", "read_show_invoice", "archive", "archive_and_unsub", "decline_and_archive", "send_decline_and_archive")
        )
        total = max(1, stage1_total + mail_est + 1)
        done = 0

        def emit(msg: str, result: Optional[ExecutionItemResult] = None) -> None:
            if on_progress:
                on_progress(
                    {
                        "done": min(done, total),
                        "total": total,
                        "message": msg,
                        "result": result.to_dict() if result else None,
                    }
                )

        unsub_cache: dict[str, PlaywrightOutcome] = {}
        to_mark_read: dict[tuple[str, str], list[tuple[ClassifiedItem, ExecutionItemResult]]] = defaultdict(list)
        to_archive: dict[tuple[str, str, str], list[tuple[ClassifiedItem, ExecutionItemResult]]] = defaultdict(list)

        for item, action in items_with_actions:
            if action == "no_action":
                continue
            h = item.header
            acct_addrs = account_addresses.get(h.account, [])
            user_email = acct_addrs[0] if acct_addrs else ""

            self._learn_if_opted_out_of_unsub(item, action)

            if action == "mark_read":
                res = ExecutionItemResult(
                    uid=item.uid,
                    account=h.account,
                    internal_id=h.internal_id,
                    sender=h.sender,
                    subject=h.subject,
                    action=action,
                    status="ok",
                    detail="Marked as read",
                )
                to_mark_read[(h.account, h.mailbox)].append((item, res))

            elif action == "read_open_link":
                url = item.extracted.pertinent_url
                if url and is_public_http_url(url, allow_private=self.allow_private_urls):
                    await asyncio.to_thread(subprocess.run, ["open", url], check=False)
                    self.store.add_opened_tray_item(
                        message_uid=item.uid,
                        account=h.account,
                        mailbox=h.mailbox,
                        internal_id=h.internal_id,
                        rfc_message_id=h.rfc_message_id,
                        subject=h.subject,
                        sender=h.sender,
                        opened_target=url,
                    )
                    self.store.increment_stat("total_opened", 1)
                    res = ExecutionItemResult(
                        uid=item.uid,
                        account=h.account,
                        internal_id=h.internal_id,
                        sender=h.sender,
                        subject=h.subject,
                        action=action,
                        status="ok",
                        detail="Opened link in browser & added to Step 2 Safety Tray",
                        opened_target=url,
                    )
                    to_mark_read[(h.account, h.mailbox)].append((item, res))
                else:
                    res = ExecutionItemResult(
                        uid=item.uid,
                        account=h.account,
                        internal_id=h.internal_id,
                        sender=h.sender,
                        subject=h.subject,
                        action=action,
                        status="failed",
                        detail="No valid public HTTP(S) URL found",
                    )

            elif action == "read_show_invoice":
                if item.extracted.pdf_attachments:
                    try:
                        raw = await asyncio.to_thread(
                            self.bridge.get_raw_source, h.account, h.mailbox, h.internal_id
                        )
                        pdf_path = extract_pdf_attachment(
                            raw,
                            item.extracted.pdf_attachments[0].part_index,
                            self.config.invoices_dir,
                            prefix=h.internal_id,
                        )
                        await asyncio.to_thread(
                            subprocess.run, ["open", "-a", "Preview", str(pdf_path)], check=False
                        )
                        self.store.add_opened_tray_item(
                            message_uid=item.uid,
                            account=h.account,
                            mailbox=h.mailbox,
                            internal_id=h.internal_id,
                            rfc_message_id=h.rfc_message_id,
                            subject=h.subject,
                            sender=h.sender,
                            opened_target=str(pdf_path),
                        )
                        self.store.increment_stat("total_opened", 1)
                        res = ExecutionItemResult(
                            uid=item.uid,
                            account=h.account,
                            internal_id=h.internal_id,
                            sender=h.sender,
                            subject=h.subject,
                            action=action,
                            status="ok",
                            detail=f"Opened {pdf_path.name} in Preview & added to Safety Tray",
                            opened_target=str(pdf_path),
                        )
                        to_mark_read[(h.account, h.mailbox)].append((item, res))
                    except Exception as exc:
                        res = ExecutionItemResult(
                            uid=item.uid,
                            account=h.account,
                            internal_id=h.internal_id,
                            sender=h.sender,
                            subject=h.subject,
                            action=action,
                            status="failed",
                            detail=f"Failed extracting PDF: {exc}",
                        )
                else:
                    res = ExecutionItemResult(
                        uid=item.uid,
                        account=h.account,
                        internal_id=h.internal_id,
                        sender=h.sender,
                        subject=h.subject,
                        action=action,
                        status="failed",
                        detail="No PDF attachment found",
                    )

            elif action == "archive":
                if item.category == "newsletters" and item.extracted.unsub_target is None and item.extracted.newsletter_signals:
                    self.store.enqueue_missed_unsubscribe(
                        account=h.account,
                        mailbox=h.mailbox,
                        internal_id=h.internal_id,
                        rfc_message_id=h.rfc_message_id,
                        sender=h.sender,
                        sender_domain=h.sender_domain,
                        subject=h.subject,
                        stage="extract",
                        failure_reason="Newsletter signals detected but no reliable unsubscribe button/header found",
                        candidate_links=item.extracted.candidate_links,
                        headers_snapshot=item.extracted.headers_snapshot,
                        footer_html_snapshot=item.extracted.footer_html_snapshot,
                    )
                res = ExecutionItemResult(
                    uid=item.uid,
                    account=h.account,
                    internal_id=h.internal_id,
                    sender=h.sender,
                    subject=h.subject,
                    action=action,
                    status="ok",
                    detail=f"Marked read & archived to {h.account}/{item.archive_mailbox}",
                )
                to_archive[(h.account, h.mailbox, item.archive_mailbox)].append((item, res))

            elif action in ("archive_and_unsub", "unsub_only"):
                unsub_t = item.extracted.unsub_target
                if unsub_t is None:
                    res = ExecutionItemResult(
                        uid=item.uid,
                        account=h.account,
                        internal_id=h.internal_id,
                        sender=h.sender,
                        subject=h.subject,
                        action=action,
                        status="failed",
                        detail="No reliable unsubscribe target available",
                    )
                else:
                    sender_key = self.store.normalize_sender_key(h.sender_email, h.sender_domain) or f"url:{unsub_t.url}"
                    if sender_key in unsub_cache:
                        outcome = unsub_cache[sender_key]
                    else:
                        force_pw = item.repeat_offender_days is not None
                        low_unsub_url = unsub_t.url.lower()
                        try_post_first = (
                            unsub_t.kind == "rfc8058_one_click"
                            or "one-click" in low_unsub_url
                            or "oneclick" in low_unsub_url
                        ) and not force_pw
                        unsub_patterns = load_unsub_patterns(self.config.patterns_path)
                        if try_post_first:
                            outcome = await try_rfc8058_one_click(
                                unsub_t.url,
                                allow_private=self.allow_private_urls,
                                patterns=unsub_patterns,
                            )
                            if outcome.status == "failed":
                                outcome = await run_playwright_action(
                                    unsub_t.url,
                                    mode="unsubscribe",
                                    user_email=user_email,
                                    screenshots_dir=self.config.screenshots_dir,
                                    headless=headless,
                                    allow_private=self.allow_private_urls,
                                    browser_locale=self.config.browser_locale,
                                    accept_language=self.config.accept_language,
                                    patterns=unsub_patterns,
                                )
                        else:
                            outcome = await run_playwright_action(
                                unsub_t.url,
                                mode="unsubscribe",
                                user_email=user_email,
                                screenshots_dir=self.config.screenshots_dir,
                                headless=headless,
                                allow_private=self.allow_private_urls,
                                browser_locale=self.config.browser_locale,
                                accept_language=self.config.accept_language,
                                patterns=unsub_patterns,
                            )
                        fb_url = item.extracted.fallback_unsub_url
                        if outcome.status == "failed" and fb_url and fb_url != unsub_t.url:
                            outcome = await run_playwright_action(
                                fb_url,
                                mode="unsubscribe",
                                user_email=user_email,
                                screenshots_dir=self.config.screenshots_dir,
                                headless=headless,
                                allow_private=self.allow_private_urls,
                                browser_locale=self.config.browser_locale,
                                accept_language=self.config.accept_language,
                                patterns=unsub_patterns,
                            )
                        unsub_cache[sender_key] = outcome
                        if outcome.status == "success":
                            self.store.record_unsubscribed(
                                sender_email=h.sender_email,
                                sender_domain=h.sender_domain,
                                display_name=h.sender,
                            )
                        else:
                            self.store.enqueue_missed_unsubscribe(
                                account=h.account,
                                mailbox=h.mailbox,
                                internal_id=h.internal_id,
                                rfc_message_id=h.rfc_message_id,
                                sender=h.sender,
                                sender_domain=h.sender_domain,
                                subject=h.subject,
                                stage="playwright",
                                failure_reason=outcome.detail,
                                candidate_links=item.extracted.candidate_links,
                                headers_snapshot=item.extracted.headers_snapshot,
                                footer_html_snapshot=item.extracted.footer_html_snapshot,
                                final_url=outcome.final_url,
                                screenshot_path=outcome.screenshot_path or "",
                            )

                    if outcome.status == "success":
                        endpoint_host = urlparse(outcome.final_url or unsub_t.url).netloc
                        host_note = f" [{endpoint_host}]" if endpoint_host and endpoint_host not in outcome.detail else ""
                        res = ExecutionItemResult(
                            uid=item.uid,
                            account=h.account,
                            internal_id=h.internal_id,
                            sender=h.sender,
                            subject=h.subject,
                            action=action,
                            status="ok",
                            detail=f"{outcome.detail}{host_note}",
                            opened_target=outcome.final_url or unsub_t.url,
                            screenshot_path=outcome.screenshot_path,
                        )
                        if action == "archive_and_unsub":
                            to_archive[(h.account, h.mailbox, item.archive_mailbox)].append((item, res))
                    else:
                        res = ExecutionItemResult(
                            uid=item.uid,
                            account=h.account,
                            internal_id=h.internal_id,
                            sender=h.sender,
                            subject=h.subject,
                            action=action,
                            status=outcome.status,
                            detail=f"{outcome.detail} (kept in {h.mailbox})",
                            opened_target=outcome.final_url,
                            screenshot_path=outcome.screenshot_path,
                        )

            elif action == "decline_and_archive":
                decline_url = item.extracted.consulting_links.decline_url
                if not decline_url:
                    res = ExecutionItemResult(
                        uid=item.uid,
                        account=h.account,
                        internal_id=h.internal_id,
                        sender=h.sender,
                        subject=h.subject,
                        action=action,
                        status="failed",
                        detail="No Decline URL found",
                    )
                else:
                    outcome = await run_playwright_action(
                        decline_url,
                        mode="consulting_decline",
                        user_email=user_email,
                        screenshots_dir=self.config.screenshots_dir,
                        headless=headless,
                        allow_private=self.allow_private_urls,
                        browser_locale=self.config.browser_locale,
                        accept_language=self.config.accept_language,
                        patterns=load_unsub_patterns(self.config.patterns_path),
                    )
                    if outcome.status == "success":
                        self.store.increment_stat("total_declined", 1)
                        res = ExecutionItemResult(
                            uid=item.uid,
                            account=h.account,
                            internal_id=h.internal_id,
                            sender=h.sender,
                            subject=h.subject,
                            action=action,
                            status="ok",
                            detail=outcome.detail,
                            opened_target=outcome.final_url or decline_url,
                            screenshot_path=outcome.screenshot_path,
                        )
                        to_archive[(h.account, h.mailbox, item.archive_mailbox)].append((item, res))
                    else:
                        res = ExecutionItemResult(
                            uid=item.uid,
                            account=h.account,
                            internal_id=h.internal_id,
                            sender=h.sender,
                            subject=h.subject,
                            action=action,
                            status=outcome.status,
                            detail=f"{outcome.detail} (kept in {h.mailbox})",
                            opened_target=outcome.final_url,
                            screenshot_path=outcome.screenshot_path,
                        )

            elif action == "draft_decline":
                try:
                    await asyncio.to_thread(
                        self.bridge.create_or_send_reply,
                        h.account,
                        h.mailbox,
                        h.internal_id,
                        self.config.consulting_decline_boilerplate,
                        send_now=False,
                        open_window=True,
                    )
                    res = ExecutionItemResult(
                        uid=item.uid,
                        account=h.account,
                        internal_id=h.internal_id,
                        sender=h.sender,
                        subject=h.subject,
                        action=action,
                        status="drafted",
                        detail="Opened boilerplate decline draft in Mail.app (kept in Inbox until sent)",
                    )
                except Exception as exc:
                    res = ExecutionItemResult(
                        uid=item.uid,
                        account=h.account,
                        internal_id=h.internal_id,
                        sender=h.sender,
                        subject=h.subject,
                        action=action,
                        status="failed",
                        detail=f"Draft error: {exc}",
                    )

            elif action == "send_decline_and_archive":
                try:
                    await asyncio.to_thread(
                        self.bridge.create_or_send_reply,
                        h.account,
                        h.mailbox,
                        h.internal_id,
                        self.config.consulting_decline_boilerplate,
                        send_now=True,
                        open_window=False,
                    )
                    self.store.increment_stat("total_declined", 1)
                    res = ExecutionItemResult(
                        uid=item.uid,
                        account=h.account,
                        internal_id=h.internal_id,
                        sender=h.sender,
                        subject=h.subject,
                        action=action,
                        status="ok",
                        detail=f"Sent boilerplate decline & archived to {h.account}/{item.archive_mailbox}",
                    )
                    to_archive[(h.account, h.mailbox, item.archive_mailbox)].append((item, res))
                except Exception as exc:
                    res = ExecutionItemResult(
                        uid=item.uid,
                        account=h.account,
                        internal_id=h.internal_id,
                        sender=h.sender,
                        subject=h.subject,
                        action=action,
                        status="failed",
                        detail=f"Send decline error: {exc}",
                    )
            else:
                res = ExecutionItemResult(
                    uid=item.uid,
                    account=h.account,
                    internal_id=h.internal_id,
                    sender=h.sender,
                    subject=h.subject,
                    action=action,
                    status="skipped",
                    detail=f"Unsupported action: {action}",
                )

            results.append(res)
            done += 1
            emit(f"Step 1/3 · Web & link prep ({done}/{stage1_total}): {h.sender}", res)

        # Recalibrate exact Stage 2 Mail.app operation count (+1 reserved for Stage 3 inbox refresh)
        mail_total = sum(len(v) for v in to_mark_read.values()) + sum(len(v) for v in to_archive.values())
        total = max(1, done + mail_total + 1)

        # Apply chunked Mail.app read/archive calls so progress updates live and large folders never hit 180s
        for (acct, box), pair_list in to_mark_read.items():
            total_chunks = (len(pair_list) + ARCHIVE_CHUNK_SIZE - 1) // ARCHIVE_CHUNK_SIZE
            for chunk_idx, start in enumerate(range(0, len(pair_list), ARCHIVE_CHUNK_SIZE), start=1):
                chunk_pairs = pair_list[start : start + ARCHIVE_CHUNK_SIZE]
                ids = [it.header.internal_id for it, _ in chunk_pairs]
                emit(f"Step 2/3 · Marking read in {acct}/{box} (chunk {chunk_idx}/{total_chunks})...")
                try:
                    marked = set(await asyncio.to_thread(self.bridge.mark_read, acct, box, ids))
                    for it, res in chunk_pairs:
                        if it.header.internal_id not in marked:
                            res.status = "failed"
                            res.detail = f"{res.detail} (Mail.app could not mark id #{it.header.internal_id} read)"
                except Exception as exc:
                    for _, res in chunk_pairs:
                        res.status = "failed"
                        res.detail = f"Mail.app mark_read error: {exc}"
                done += len(chunk_pairs)
                emit(f"Step 2/3 · Marked read in {acct}/{box} (chunk {chunk_idx}/{total_chunks})")

        synced_accounts: set[str] = set()
        for (acct, src_box, arch_box), pair_list in to_archive.items():
            total_chunks = (len(pair_list) + ARCHIVE_CHUNK_SIZE - 1) // ARCHIVE_CHUNK_SIZE
            acct_moved = 0
            for chunk_idx, start in enumerate(range(0, len(pair_list), ARCHIVE_CHUNK_SIZE), start=1):
                chunk_pairs = pair_list[start : start + ARCHIVE_CHUNK_SIZE]
                ids = [it.header.internal_id for it, _ in chunk_pairs]
                emit(f"Step 2/3 · Archiving {acct}/{src_box} → {arch_box} (chunk {chunk_idx}/{total_chunks})...")
                try:
                    moved = set(
                        await asyncio.to_thread(
                            self.bridge.mark_read_and_archive,
                            acct,
                            src_box,
                            arch_box,
                            ids,
                            mark_read=self.config.mark_read_on_archive,
                        )
                    )
                    chunk_moved = 0
                    for it, res in chunk_pairs:
                        if it.header.internal_id in moved:
                            chunk_moved += 1
                            acct_moved += 1
                        else:
                            res.status = "failed"
                            res.detail = f"{res.detail} (archive skipped: id #{it.header.internal_id} not found in {src_box})"
                    if chunk_moved:
                        self.store.increment_stat("total_archived", chunk_moved)
                except Exception as exc:
                    reconciled_moved: set[str] = set()
                    if isinstance(exc, TimeoutError) and hasattr(self.bridge, "list_mailbox_ids"):
                        try:
                            still_present = await asyncio.to_thread(self.bridge.list_mailbox_ids, acct, src_box)
                            if still_present is not None:
                                reconciled_moved = {mid for mid in ids if mid not in still_present}
                        except Exception:
                            reconciled_moved = set()
                    if reconciled_moved:
                        self.store.increment_stat("total_archived", len(reconciled_moved))
                        acct_moved += len(reconciled_moved)
                    for it, res in chunk_pairs:
                        if it.header.internal_id in reconciled_moved:
                            res.detail = f"{res.detail} (verified moved after Mail timeout)"
                        else:
                            res.status = "failed"
                            res.detail = f"{res.detail} (archive error: {exc})"
                done += len(chunk_pairs)
                emit(
                    f"Step 2/3 · Archived {acct_moved}/{len(pair_list)} in {acct}/{src_box} (chunk {chunk_idx}/{total_chunks})"
                )
            if acct not in synced_accounts and hasattr(self.bridge, "synchronize_account"):
                synced_accounts.add(acct)
                await asyncio.to_thread(self.bridge.synchronize_account, acct, 8)

        for res in results:
            self.store.log_action(
                run_id=run_id,
                account=res.account,
                internal_id=res.internal_id,
                sender=res.sender,
                subject=res.subject,
                action=res.action,
                status=res.status,
                detail=res.detail,
            )

        return results

    def sweep_opened_tray(self, message_uids: list[str], archive_map: dict[str, str]) -> dict[str, Any]:
        """Move approved Step 2 Safety Tray items from INBOX to their account Archive."""
        active = {row["message_uid"]: row for row in self.store.list_active_tray()}
        by_target: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)

        for uid in message_uids:
            row = active.get(uid)
            if not row:
                continue
            acct = row["account"]
            arch_box = archive_map.get(acct, "Archive")
            by_target[(acct, row["mailbox"], arch_box)].append((uid, row["internal_id"]))

        swept_uids: list[str] = []
        try:
            for (acct, src_box, arch_box), pairs in by_target.items():
                for start in range(0, len(pairs), ARCHIVE_CHUNK_SIZE):
                    chunk_pairs = pairs[start : start + ARCHIVE_CHUNK_SIZE]
                    ids = [iid for _, iid in chunk_pairs]
                    moved = set(
                        self.bridge.mark_read_and_archive(
                            acct,
                            src_box,
                            arch_box,
                            ids,
                            mark_read=self.config.mark_read_on_archive,
                        )
                    )
                    chunk_swept: list[str] = []
                    for uid, iid in chunk_pairs:
                        if iid in moved:
                            chunk_swept.append(uid)
                            swept_uids.append(uid)
                    if chunk_swept:
                        self.store.mark_tray_swept(chunk_swept)
                        self.store.increment_stat("total_archived", len(chunk_swept))
        finally:
            pass
        return {"swept_count": len(swept_uids), "swept_uids": swept_uids}
