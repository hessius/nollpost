"""Local authenticated FastAPI web server for the Nollpost Review Board."""

from __future__ import annotations

import asyncio
import hmac
import re
import secrets
import subprocess
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nollpost.config import TriageConfig
from nollpost.executor import BatchExecutor
from nollpost.mail_bridge import AppleMailBridge, parse_sender_parts
from nollpost.models import ClassifiedItem
from nollpost.patterns_loader import load_unsub_patterns
from nollpost.playwright_runner import is_public_http_url, run_playwright_action
from nollpost.scanner import ScanSnapshot, run_scan
from nollpost.store import TriageStore

STATIC_DIR = Path(__file__).parent / "static"


class ActionSelection(BaseModel):
    uid: str
    action: str


class ExecuteRequest(BaseModel):
    mode: str = "inbox"
    items: list[ActionSelection]
    headless: bool = True


class PreferenceRequest(BaseModel):
    sender_email: str
    sender_domain: str = ""
    display_name: str = ""
    preference: str


class TrayActionRequest(BaseModel):
    uids: list[str]


class MissedEnqueueRequest(BaseModel):
    uid: str


class MissedActionRequest(BaseModel):
    note: str = ""
    headless: bool = True
    url: str = ""
    archive: bool = True


class MarkUnsubscribedRequest(BaseModel):
    uid: str = ""
    missed_id: Optional[int] = None
    account: str = ""
    mailbox: str = ""
    internal_id: str = ""
    sender: str = ""
    subject: str = ""
    archive: bool = True
    note: str = "Marked as unsubscribed by user"


@dataclass
class AppRuntime:
    config: TriageConfig
    store: TriageStore
    bridge: AppleMailBridge
    executor: BatchExecutor
    token: str
    snapshots: dict[str, ScanSnapshot] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    scan_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    scan_progress: dict[str, Any] = field(
        default_factory=lambda: {
            "active": False,
            "mode": "inbox",
            "phase": "idle",
            "message": "",
            "done": 0,
            "total": 0,
            "account": "",
        }
    )

    def _enrich_tray(self, inbox_snap: Optional[ScanSnapshot] = None) -> list[dict[str, Any]]:
        rows = self.store.list_active_tray()
        ref = inbox_snap or self.snapshots.get("inbox")
        if not ref:
            return rows
        by_uid = {item.uid: item for item in ref.items}
        for row in rows:
            item = by_uid.get(row["message_uid"])
            if item and item.extracted.pertinent_url:
                row["latest_pertinent_url"] = item.extracted.pertinent_url
                row["latest_pertinent_label"] = item.extracted.pertinent_label or ""
            else:
                row["latest_pertinent_url"] = row.get("opened_target", "")
                row["latest_pertinent_label"] = ""
        return rows

    async def get_snapshot(
        self,
        mode: str = "inbox",
        refresh: bool = False,
        bypass_cache: bool = False,
    ) -> ScanSnapshot:
        if mode not in ("inbox", "catchup"):
            raise HTTPException(status_code=400, detail="Invalid mode")
        async with self.scan_lock:
            if refresh or mode not in self.snapshots:
                self.scan_progress = {
                    "active": True,
                    "mode": mode,
                    "phase": "starting",
                    "message": f"Starting {mode} scan…",
                    "done": 0,
                    "total": 0,
                    "account": "",
                }

                def _on_progress(prog: dict[str, Any]) -> None:
                    self.scan_progress = dict(prog)

                try:
                    snap = await asyncio.to_thread(
                        run_scan,
                        self.config,
                        self.store,
                        self.bridge,
                        mode=mode,
                        bypass_cache=bypass_cache,
                        on_progress=_on_progress,
                    )
                finally:
                    self.scan_progress = {
                        "active": False,
                        "mode": mode,
                        "phase": "idle",
                        "message": "",
                        "done": 0,
                        "total": 0,
                        "account": "",
                    }
                self.snapshots[mode] = snap
            else:
                snap = self.snapshots[mode]
                snap.missed = self.store.list_missed_unsubscribes(include_resolved=False)
                snap.stats = self.store.get_stats()
            snap.tray = self._enrich_tray(snap if mode == "inbox" else self.snapshots.get("inbox"))
            return snap


def create_app(
    config: TriageConfig,
    store: TriageStore,
    bridge: AppleMailBridge,
    token: Optional[str] = None,
    preload: bool = False,
    allow_private_urls: bool = False,
    open_browser_url: Optional[str] = None,
) -> tuple[FastAPI, str]:
    auth_token = token or secrets.token_urlsafe(24)
    executor = BatchExecutor(
        config=config,
        store=store,
        bridge=bridge,
        allow_private_urls=allow_private_urls,
    )
    runtime = AppRuntime(
        config=config,
        store=store,
        bridge=bridge,
        executor=executor,
        token=auth_token,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if preload:
            await runtime.get_snapshot(mode="inbox", refresh=True)
        if open_browser_url:
            subprocess.Popen(["open", open_browser_url])
        try:
            yield
        finally:
            for bg_task in list(runtime.background_tasks):
                bg_task.cancel()
            if runtime.background_tasks:
                await asyncio.gather(*runtime.background_tasks, return_exceptions=True)

    app = FastAPI(title="Nollpost Review Board", lifespan=lifespan)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        key_param = request.query_params.get("key") or ""
        header_key = request.headers.get("X-Triage-Key") or ""
        cookie_val = request.cookies.get("triage_key") or ""
        candidate = key_param or header_key or cookie_val
        if not hmac.compare_digest(candidate, auth_token):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        if key_param and hmac.compare_digest(key_param, auth_token) and not hmac.compare_digest(cookie_val, auth_token):
            response.set_cookie("triage_key", auth_token, httponly=True, samesite="strict")
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/state")
    async def api_state(mode: str = "inbox", refresh: bool = False) -> dict[str, Any]:
        snap = await runtime.get_snapshot(mode=mode, refresh=refresh, bypass_cache=refresh)
        return snap.to_dict()

    @app.post("/api/scan")
    async def api_scan(mode: str = "inbox") -> dict[str, Any]:
        snap = await runtime.get_snapshot(mode=mode, refresh=True, bypass_cache=True)
        return snap.to_dict()

    @app.get("/api/scan/status")
    async def api_scan_status() -> dict[str, Any]:
        return dict(runtime.scan_progress)

    @app.post("/api/preferences")
    async def api_preferences(req: PreferenceRequest) -> dict[str, Any]:
        if req.preference == "clear":
            runtime.store.record_sender_preference(
                sender_email=req.sender_email,
                sender_domain=req.sender_domain,
                display_name=req.display_name,
                preferred_action=None,
                keep_subscribed=False,
            )
        elif req.preference in ("always_keep", "archive", "mark_read"):
            runtime.store.record_sender_preference(
                sender_email=req.sender_email,
                sender_domain=req.sender_domain,
                display_name=req.display_name,
                preferred_action=req.preference,
                keep_subscribed=True,
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid preference")
        async with runtime.scan_lock:
            runtime.snapshots.clear()
        return {"ok": True}

    def _resolve_pairs(snap: ScanSnapshot, items: list[ActionSelection]) -> list[tuple[ClassifiedItem, str]]:
        by_uid = {item.uid: item for item in snap.items}
        deduped: dict[str, str] = {}
        for sel in items:
            deduped[sel.uid] = sel.action
        pairs: list[tuple[ClassifiedItem, str]] = []
        for uid, action in deduped.items():
            item = by_uid.get(uid)
            if not item:
                continue
            if action not in item.allowed_actions and action != "no_action":
                raise HTTPException(status_code=400, detail=f"Action {action} not allowed for {uid}")
            pairs.append((item, action))
        return pairs

    @app.post("/api/execute/preview")
    async def api_execute_preview(req: ExecuteRequest) -> dict[str, Any]:
        snap = await runtime.get_snapshot(mode=req.mode, refresh=False)
        pairs = _resolve_pairs(snap, req.items)
        acct_addrs = {a.name: a.addresses for a in snap.accounts}
        return runtime.executor.build_confirmation_preview(pairs, acct_addrs)

    @app.post("/api/execute/run")
    async def api_execute_run(req: ExecuteRequest) -> dict[str, Any]:
        snap = await runtime.get_snapshot(mode=req.mode, refresh=False)
        pairs = _resolve_pairs(snap, req.items)
        acct_addrs = {a.name: a.addresses for a in snap.accounts}
        job_id = uuid.uuid4().hex[:12]
        runtime.jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "done": 0,
            "total": sum(1 for _, act in pairs if act != "no_action"),
            "message": "Starting batch...",
            "results": [],
            "scan_warning": "",
        }

        def on_prog(prog: dict[str, Any]) -> None:
            runtime.jobs[job_id]["done"] = prog["done"]
            runtime.jobs[job_id]["total"] = prog["total"]
            runtime.jobs[job_id]["message"] = prog["message"]

        async def runner() -> None:
            try:
                results = await runtime.executor.execute_batch(
                    pairs,
                    acct_addrs,
                    headless=req.headless,
                    on_progress=on_prog,
                )
                runtime.jobs[job_id]["results"] = [r.to_dict() for r in results]
            except Exception as exc:
                runtime.jobs[job_id]["status"] = "error"
                runtime.jobs[job_id]["message"] = str(exc)
                return

            try:
                runtime.jobs[job_id]["message"] = "Step 3/3 · Refreshing Apple Mail inbox view..."
                await runtime.get_snapshot(mode=req.mode, refresh=True, bypass_cache=False)
            except Exception as scan_exc:
                runtime.jobs[job_id]["scan_warning"] = f"Actions completed, but inbox refresh failed: {scan_exc}"
            runtime.jobs[job_id]["done"] = runtime.jobs[job_id]["total"]
            runtime.jobs[job_id]["message"] = "Completed"
            runtime.jobs[job_id]["status"] = "completed"

        task = asyncio.create_task(runner())
        runtime.background_tasks.add(task)
        task.add_done_callback(runtime.background_tasks.discard)
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    async def api_job(job_id: str) -> dict[str, Any]:
        job = runtime.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        out = dict(job)
        if job["status"] == "completed":
            out["stats"] = runtime.store.get_stats()
            out["tray"] = runtime._enrich_tray()
            out["missed"] = runtime.store.list_missed_unsubscribes(include_resolved=False)
        return out

    @app.post("/api/tray/sweep/preview")
    async def api_tray_preview(req: TrayActionRequest) -> dict[str, Any]:
        snap = await runtime.get_snapshot(mode="inbox", refresh=False)
        archive_map = {a.name: a.archive_mailbox for a in snap.accounts}
        active = {row["message_uid"]: row for row in snap.tray}
        chosen = [active[u] for u in req.uids if u in active]
        targets: dict[str, dict[str, int]] = {}
        for row in chosen:
            acct = row["account"]
            dest = archive_map.get(acct, "Archive")
            targets.setdefault(acct, {})
            targets[acct][dest] = targets[acct].get(dest, 0) + 1
        return {
            "count": len(chosen),
            "deleted_count": 0,
            "archive_targets": targets,
            "items": [
                {
                    "uid": r["message_uid"],
                    "subject": r["subject"],
                    "sender": r["sender"],
                    "account": r["account"],
                    "opened_target": r.get("opened_target", ""),
                    "latest_pertinent_url": r.get("latest_pertinent_url") or r.get("opened_target", ""),
                    "latest_pertinent_label": r.get("latest_pertinent_label", ""),
                }
                for r in chosen
            ],
        }

    @app.post("/api/tray/sweep")
    async def api_tray_sweep(req: TrayActionRequest) -> dict[str, Any]:
        snap = await runtime.get_snapshot(mode="inbox", refresh=False)
        archive_map = {a.name: a.archive_mailbox for a in snap.accounts}
        res = await asyncio.to_thread(runtime.executor.sweep_opened_tray, req.uids, archive_map)
        await runtime.get_snapshot(mode="inbox", refresh=True)
        return {**res, "stats": runtime.store.get_stats(), "tray": runtime._enrich_tray()}

    @app.post("/api/tray/dismiss")
    async def api_tray_dismiss(req: TrayActionRequest) -> dict[str, Any]:
        runtime.store.dismiss_tray_items(req.uids)
        return {"ok": True, "tray": runtime.store.list_active_tray()}

    @app.post("/api/missed/enqueue")
    async def api_missed_enqueue(req: MissedEnqueueRequest) -> dict[str, Any]:
        snap = await runtime.get_snapshot(mode="inbox", refresh=False)
        item = next((i for i in snap.items if i.uid == req.uid), None)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        row_id = runtime.store.enqueue_missed_unsubscribe(
            account=item.header.account,
            mailbox=item.header.mailbox,
            internal_id=item.header.internal_id,
            rfc_message_id=item.header.rfc_message_id,
            sender=item.header.sender,
            sender_domain=item.header.sender_domain,
            subject=item.header.subject,
            stage="extract",
            failure_reason="Manually flagged for unsubscribe parser improvement",
            candidate_links=item.extracted.candidate_links,
            headers_snapshot=item.extracted.headers_snapshot,
            footer_html_snapshot=item.extracted.footer_html_snapshot,
        )
        return {"ok": True, "id": row_id, "missed": runtime.store.list_missed_unsubscribes(False)}

    async def _mark_succeeded_and_optional_archive(
        *,
        missed_id: Optional[int] = None,
        uid: str = "",
        account: str = "",
        mailbox: str = "",
        internal_id: str = "",
        sender: str = "",
        subject: str = "",
        archive: bool = True,
        note: str = "Marked as unsubscribed by user",
    ) -> dict[str, Any]:
        snap = await runtime.get_snapshot(mode="inbox", refresh=False)
        entry = runtime.store.get_missed_unsubscribe(missed_id) if missed_id else None
        if entry:
            account = account or entry.get("account", "")
            mailbox = mailbox or entry.get("mailbox", "")
            internal_id = internal_id or str(entry.get("internal_id", ""))
            sender = sender or entry.get("sender", "")
            subject = subject or entry.get("subject", "")

        target_item = next(
            (
                i
                for i in snap.items
                if (uid and i.uid == uid)
                or (
                    account
                    and internal_id
                    and i.header.account == account
                    and i.header.internal_id == str(internal_id)
                    and (not mailbox or i.header.mailbox.upper() == mailbox.upper())
                )
            ),
            None,
        )
        if target_item:
            account = account or target_item.header.account
            mailbox = mailbox or target_item.header.mailbox
            internal_id = internal_id or target_item.header.internal_id
            sender = sender or target_item.header.sender
            subject = subject or target_item.header.subject
        mailbox = mailbox or "INBOX"

        sender_email, sender_domain = parse_sender_parts(sender)
        if not sender_domain and entry:
            sender_domain = entry.get("sender_domain", "")
        if not sender_email and not sender_domain:
            raise HTTPException(status_code=400, detail="Could not determine sender to mark unsubscribed")

        runtime.store.record_unsubscribed(
            sender_email=sender_email,
            display_name=sender,
            sender_domain=sender_domain,
        )
        if missed_id:
            runtime.store.resolve_missed_unsubscribe(missed_id, note)
        runtime.store.resolve_missed_for_message(
            account=account,
            internal_id=internal_id,
            sender_domain=sender_domain,
            note=note,
        )

        archived_ok = False
        archive_map = {a.name: a.archive_mailbox for a in snap.accounts}
        arch_box = archive_map.get(account) or runtime.config.archive_map.get(account, "Archive")
        if archive and account and internal_id and mailbox.upper() == "INBOX":
            try:
                moved = await asyncio.to_thread(
                    runtime.bridge.mark_read_and_archive,
                    account,
                    mailbox,
                    arch_box,
                    [str(internal_id)],
                    mark_read=runtime.config.mark_read_on_archive,
                )
                if str(internal_id) in moved:
                    archived_ok = True
                    runtime.store.increment_stat("total_archived", 1)
            except Exception:
                archived_ok = False

            if archived_ok and "inbox" in runtime.snapshots:
                runtime.snapshots["inbox"].items = [
                    i
                    for i in runtime.snapshots["inbox"].items
                    if not (i.header.account == account and i.header.internal_id == str(internal_id))
                ]

        runtime.store.log_action(
            run_id="manual",
            account=account or "unknown",
            internal_id=str(internal_id or ""),
            sender=sender,
            subject=subject,
            action="mark_unsubscribed",
            status="ok",
            detail=f"{note}{' & archived to ' + arch_box if archived_ok else ''}",
        )
        return {
            "ok": True,
            "archived": archived_ok,
            "stats": runtime.store.get_stats(),
            "missed": runtime.store.list_missed_unsubscribes(False),
        }

    @app.post("/api/unsub/mark-succeeded")
    async def api_unsub_mark_succeeded(req: MarkUnsubscribedRequest) -> dict[str, Any]:
        return await _mark_succeeded_and_optional_archive(
            missed_id=req.missed_id,
            uid=req.uid,
            account=req.account,
            mailbox=req.mailbox,
            internal_id=req.internal_id,
            sender=req.sender,
            subject=req.subject,
            archive=req.archive,
            note=req.note or "Marked as unsubscribed by user",
        )

    @app.post("/api/missed/{missed_id}/resolve")
    async def api_missed_resolve(missed_id: int, req: MissedActionRequest) -> dict[str, Any]:
        runtime.store.resolve_missed_unsubscribe(missed_id, req.note or "Resolved by user")
        return {"ok": True, "missed": runtime.store.list_missed_unsubscribes(False)}

    @app.post("/api/missed/{missed_id}/retry")
    async def api_missed_retry(missed_id: int, req: MissedActionRequest) -> dict[str, Any]:
        entry = runtime.store.get_missed_unsubscribe(missed_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Missed entry not found")
        candidate_urls: list[str] = []
        if req.url:
            candidate_urls.append(req.url)
        else:
            if entry.get("final_url"):
                candidate_urls.append(entry["final_url"])
            for link in reversed(entry.get("candidate_links") or []):
                href = link.get("href", "")
                txt = link.get("text", "")
                if href and href not in candidate_urls and re.search(
                    r"unsub|opt[-_]?out|avregistrera|avprenumerera|preference|subscription",
                    f"{txt} {href}",
                    re.I,
                ):
                    candidate_urls.append(href)
            if not candidate_urls and entry.get("candidate_links"):
                candidate_urls.append(entry["candidate_links"][0].get("href", ""))

        valid_urls = [u for u in candidate_urls if is_public_http_url(u, allow_private=allow_private_urls)]
        if not valid_urls:
            raise HTTPException(status_code=400, detail="No valid public URL available to retry")

        outcome = None
        unsub_patterns = load_unsub_patterns(runtime.config.patterns_path)
        for target_url in valid_urls[:2]:
            outcome = await run_playwright_action(
                target_url,
                mode="unsubscribe",
                screenshots_dir=runtime.config.screenshots_dir,
                headless=req.headless,
                allow_private=allow_private_urls,
                browser_locale=runtime.config.browser_locale,
                accept_language=runtime.config.accept_language,
                patterns=unsub_patterns,
            )
            if outcome.status == "success":
                break
        assert outcome is not None
        if outcome.status == "success":
            await _mark_succeeded_and_optional_archive(
                missed_id=missed_id,
                account=entry.get("account", ""),
                mailbox=entry.get("mailbox", "INBOX"),
                internal_id=str(entry.get("internal_id", "")),
                sender=entry.get("sender", ""),
                subject=entry.get("subject", ""),
                archive=req.archive,
                note=f"Retry succeeded: {outcome.detail}",
            )
        return {
            "outcome": outcome.to_dict(),
            "stats": runtime.store.get_stats(),
            "missed": runtime.store.list_missed_unsubscribes(False),
        }

    @app.get("/api/screenshots/{filename}")
    async def api_screenshot(filename: str) -> FileResponse:
        if not re.fullmatch(r"[\w.\-]+\.png", filename):
            raise HTTPException(status_code=400, detail="Invalid filename")
        path = (runtime.config.screenshots_dir / filename).resolve()
        if runtime.config.screenshots_dir.resolve() not in path.parents or not path.exists():
            raise HTTPException(status_code=404, detail="Screenshot not found")
        return FileResponse(path, media_type="image/png")

    return app, auth_token
