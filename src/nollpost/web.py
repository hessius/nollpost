"""Local authenticated FastAPI web server for the Nollpost Review Board."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import time
import tomllib
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nollpost.config import VALID_GMAIL_ARCHIVE_STRATEGIES, TriageConfig, update_config_file
from nollpost.executor import BatchExecutor
from nollpost.mail_bridge import AppleMailBridge, parse_sender_parts, validate_archive_destination
from nollpost.missed_export import format_missed_bug_report, redact_missed_entry
from nollpost.models import ClassifiedItem
from nollpost.packs import BUILTIN_PACKS
from nollpost.patterns_loader import load_unsub_patterns
from nollpost.playwright_runner import is_public_http_url, run_playwright_action
from nollpost.scanner import ScanSnapshot, run_scan
from nollpost.store import TriageStore
from nollpost.updates import async_check_for_update

STATIC_DIR = Path(__file__).parent / "static"
LOCALES_DIR = STATIC_DIR / "locales"


def _read_locale_file(code: str, locales_dir: Path = LOCALES_DIR) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,4})?", code or "", re.IGNORECASE):
        return {}
    path = locales_dir / f"{code}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def list_available_ui_locales(locales_dir: Path = LOCALES_DIR) -> list[dict[str, str]]:
    """Return sorted UI locale metadata dicts ({code, name}) shipped in LOCALES_DIR."""
    if not locales_dir.is_dir():
        return []
    items: list[dict[str, str]] = []
    for path in sorted(locales_dir.glob("*.json")):
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,4})?", path.stem, re.IGNORECASE):
            continue
        raw = _read_locale_file(path.stem, locales_dir)
        meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
        code = str(meta.get("code") or path.stem).strip().lower()
        name = str(meta.get("name") or code).strip()
        items.append({"code": code, "name": name})
    items.sort(key=lambda it: (0 if it["code"] == "en" else 1, it["code"]))
    return items


def _match_locale_tag(tag: str, available_codes: set[str]) -> Optional[str]:
    norm = str(tag or "").strip().lower().replace("_", "-")
    if not norm or norm == "*":
        return None
    if norm in available_codes:
        return norm
    primary = norm.split("-", 1)[0]
    if primary in available_codes:
        return primary
    return None


def resolve_ui_language(
    configured_lang: str = "",
    accept_language: str = "",
    available_codes: Optional[set[str]] = None,
) -> str:
    """Resolve UI locale from explicit config, then Accept-Language header, else 'en'."""
    codes = set(available_codes) if available_codes is not None else {x["code"] for x in list_available_ui_locales()}
    fallback = "en" if "en" in codes else (sorted(codes)[0] if codes else "en")
    norm_config = str(configured_lang or "").strip()
    if norm_config and norm_config.lower() != "auto":
        return _match_locale_tag(norm_config, codes) or fallback

    weighted: list[tuple[float, int, str]] = []
    for idx, part in enumerate(str(accept_language or "").split(",")):
        piece = part.strip()
        if not piece:
            continue
        segments = [s.strip() for s in piece.split(";")]
        tag = segments[0]
        q = 1.0
        for param in segments[1:]:
            if param.lower().startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 0.0
        if q <= 0:
            continue
        weighted.append((-q, idx, tag))
    weighted.sort()
    for _, _, tag in weighted:
        matched = _match_locale_tag(tag, codes)
        if matched:
            return matched
    return fallback


def load_ui_locale_messages(language: str, locales_dir: Path = LOCALES_DIR) -> dict[str, str]:
    """Load locale strings for `language`, layered onto 'en' as fallback."""
    merged: dict[str, str] = {}
    for code in ("en", language):
        if not code:
            continue
        raw = _read_locale_file(code, locales_dir)
        for k, v in raw.items():
            if k == "_meta" or not isinstance(v, str):
                continue
            merged[k] = v
    return merged


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


class SettingsUpdateRequest(BaseModel):
    user: Optional[dict[str, Any]] = None
    accounts: Optional[dict[str, Any]] = None
    rules: Optional[dict[str, Any]] = None
    browser: Optional[dict[str, Any]] = None
    consulting: Optional[dict[str, Any]] = None
    profile: Optional[dict[str, Any]] = None


class SenderMemoryBulkRequest(BaseModel):
    sender_keys: list[str]
    action: str
    display_name: str = ""


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
    last_activity_at: float = field(default_factory=time.monotonic)
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

    def touch_activity(self) -> None:
        self.last_activity_at = time.monotonic()

    def has_active_work(self) -> bool:
        return bool(
            self.scan_lock.locked()
            or self.scan_progress.get("active")
            or any(j.get("status") == "running" for j in self.jobs.values())
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
        sync_mail: bool = True,
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
                        sync_mail=sync_mail,
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
                    self.touch_activity()
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
    idle_timeout_seconds: Optional[float] = None,
    idle_poll_interval: float = 15.0,
    on_idle_shutdown: Optional[Callable[[], None]] = None,
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
        lifespan_tasks: set[asyncio.Task[Any]] = set()
        current_loop = asyncio.get_running_loop()

        async def _preload_inbox() -> None:
            try:
                await runtime.get_snapshot(mode="inbox", refresh=True)
            except Exception:
                pass

        async def _open_browser_when_ready(target_url: str) -> None:
            parsed = urlsplit(target_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port
            if port:
                for _ in range(60):
                    try:
                        _, writer = await asyncio.wait_for(
                            asyncio.open_connection(host, port),
                            timeout=0.1,
                        )
                        writer.close()
                        await writer.wait_closed()
                        break
                    except asyncio.CancelledError:
                        return
                    except Exception:
                        await asyncio.sleep(0.05)
                else:
                    return
            else:
                await asyncio.sleep(0.25)
            subprocess.Popen(["open", target_url])

        async def _idle_watchdog() -> None:
            while True:
                await asyncio.sleep(max(0.02, float(idle_poll_interval)))
                if runtime.has_active_work():
                    runtime.touch_activity()
                    continue
                timeout_secs = (
                    float(idle_timeout_seconds)
                    if idle_timeout_seconds is not None
                    else float(runtime.config.idle_timeout_minutes) * 60.0
                )
                if timeout_secs <= 0:
                    continue
                if time.monotonic() - runtime.last_activity_at >= timeout_secs:
                    mins = max(1, round(timeout_secs / 60.0)) if timeout_secs >= 60 else 0
                    msg = (
                        f"Stopping Nollpost Review Board after {mins}m of inactivity (idle mode)."
                        if mins
                        else "Stopping Nollpost Review Board after inactivity timeout (idle mode)."
                    )
                    print(msg, flush=True)
                    if on_idle_shutdown is not None:
                        on_idle_shutdown()
                    else:
                        os.kill(os.getpid(), signal.SIGTERM)
                    return

        if preload:
            preload_task = asyncio.create_task(_preload_inbox())
            lifespan_tasks.add(preload_task)
            preload_task.add_done_callback(lifespan_tasks.discard)
            # Yield once so preload acquires scan_lock before browser requests arrive
            await asyncio.sleep(0)
        if open_browser_url:
            browser_task = asyncio.create_task(_open_browser_when_ready(open_browser_url))
            lifespan_tasks.add(browser_task)
            browser_task.add_done_callback(lifespan_tasks.discard)
        if idle_timeout_seconds is None or idle_timeout_seconds > 0:
            idle_task = asyncio.create_task(_idle_watchdog())
            lifespan_tasks.add(idle_task)
            idle_task.add_done_callback(lifespan_tasks.discard)
        try:
            yield
        finally:
            to_wait: list[asyncio.Task[Any]] = []
            for bg_task in list(lifespan_tasks) + list(runtime.background_tasks):
                if bg_task.get_loop() is current_loop:
                    bg_task.cancel()
                    to_wait.append(bg_task)
            if to_wait:
                await asyncio.gather(*to_wait, return_exceptions=True)

    app = FastAPI(title="Nollpost Review Board", lifespan=lifespan)
    app.state.runtime = runtime

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
        runtime.touch_activity()
        response = await call_next(request)
        if key_param and hmac.compare_digest(key_param, auth_token) and not hmac.compare_digest(cookie_val, auth_token):
            response.set_cookie("triage_key", auth_token, httponly=True, samesite="strict")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/i18n")
    async def api_i18n(request: Request, lang: Optional[str] = None) -> dict[str, Any]:
        available = list_available_ui_locales()
        codes = {entry["code"] for entry in available}
        chosen_override = lang.strip() if lang is not None else runtime.config.ui_language
        resolved = resolve_ui_language(
            configured_lang=chosen_override,
            accept_language=request.headers.get("accept-language", ""),
            available_codes=codes,
        )
        return {
            "language": resolved,
            "configured": runtime.config.ui_language,
            "fallback": "en",
            "available": available,
            "messages": load_ui_locale_messages(resolved),
        }

    @app.get("/api/updates")
    async def api_updates(refresh: bool = False) -> dict[str, Any]:
        return await async_check_for_update(
            runtime.config.cache_dir,
            enabled=runtime.config.check_updates,
            force_refresh=refresh,
        )

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

    def _settings_payload(cfg: TriageConfig) -> dict[str, Any]:
        raw_locale = ""
        raw_accept = ""
        if cfg.config_path.exists():
            try:
                parsed = tomllib.loads(cfg.config_path.read_text(encoding="utf-8"))
                bsec = parsed.get("browser", {}) if isinstance(parsed, dict) else {}
                raw_locale = str(bsec.get("locale", ""))
                raw_accept = str(bsec.get("accept_language", ""))
            except (OSError, tomllib.TOMLDecodeError):
                pass
        snap = runtime.snapshots.get("inbox") or runtime.snapshots.get("catchup")
        known_accts = [a.name for a in snap.accounts] if snap else list(cfg.archive_map.keys())
        return {
            "config_path": str(cfg.config_path),
            "user": {
                "display_name": cfg.display_name,
                "ui_language": cfg.ui_language,
                "available_ui_languages": list_available_ui_locales(),
                "check_updates": cfg.check_updates,
                "idle_timeout_minutes": cfg.idle_timeout_minutes,
                "mark_read_on_archive": cfg.mark_read_on_archive,
                "max_inbox_messages": cfg.max_inbox_messages,
                "catchup_scan_limit": cfg.catchup_scan_limit,
                "catchup_days_back": cfg.catchup_days_back,
            },
            "accounts": {
                "gmail_archive_strategy": cfg.gmail_archive_strategy,
                "archive_map": dict(cfg.archive_map),
                "known_accounts": known_accts,
                "valid_gmail_strategies": list(VALID_GMAIL_ARCHIVE_STRATEGIES),
            },
            "rules": {
                "enabled_packs": list(cfg.enabled_packs),
                "available_packs": [
                    {"name": p.name, "description": p.description}
                    for p in BUILTIN_PACKS.values()
                ],
            },
            "browser": {
                "locale": raw_locale,
                "accept_language": raw_accept,
                "effective_locale": cfg.browser_locale,
                "effective_accept_language": cfg.accept_language,
            },
            "consulting": {
                "enabled": cfg.consulting_enabled,
                "networks": list(cfg.consulting_networks),
                "match_keywords": list(cfg.consulting_match_keywords),
                "negative_keywords": list(cfg.consulting_negative_keywords),
                "decline_boilerplate": cfg.consulting_decline_boilerplate,
            },
            "profile": {
                "wanted_domains": list(cfg.wanted_domains),
                "bot_senders": list(cfg.bot_senders),
                "transactional_domains": list(cfg.transactional_domains),
                "bill_keywords": list(cfg.bill_keywords),
            },
        }

    @app.get("/api/settings")
    async def api_get_settings() -> dict[str, Any]:
        return _settings_payload(runtime.config)

    @app.put("/api/settings")
    async def api_put_settings(req: SettingsUpdateRequest) -> dict[str, Any]:
        updates = req.model_dump(exclude_none=True)
        async with runtime.scan_lock:
            try:
                new_cfg = update_config_file(
                    runtime.config.config_path,
                    updates,
                    archive_validator=validate_archive_destination,
                )
            except (ValueError, tomllib.TOMLDecodeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            runtime.config = new_cfg
            runtime.executor.config = new_cfg
            runtime.bridge.archive_overrides = dict(new_cfg.archive_map)
            runtime.bridge.gmail_archive_strategy = new_cfg.gmail_archive_strategy
            runtime.snapshots.clear()
        return {"ok": True, "settings": _settings_payload(new_cfg)}

    @app.get("/api/sender-memory")
    async def api_list_sender_memory(search: str = "", kind: str = "all") -> dict[str, Any]:
        return {
            "items": runtime.store.list_sender_memory(search=search, filter_kind=kind),
        }

    @app.post("/api/sender-memory/bulk")
    async def api_bulk_sender_memory(req: SenderMemoryBulkRequest) -> dict[str, Any]:
        if req.action not in ("always_keep", "archive", "mark_read", "clear", "delete"):
            raise HTTPException(status_code=400, detail="Invalid sender memory action")
        keys = [runtime.store.normalize_sender_key(k) for k in req.sender_keys if str(k).strip()]
        if not keys:
            raise HTTPException(status_code=400, detail="No sender_keys provided")
        updated = 0
        if req.action == "delete":
            updated = runtime.store.delete_sender_memory(keys)
        else:
            for k in keys:
                if req.action == "clear":
                    runtime.store.record_sender_preference(
                        sender_email=k,
                        display_name=req.display_name,
                        preferred_action=None,
                        keep_subscribed=False,
                    )
                else:
                    runtime.store.record_sender_preference(
                        sender_email=k,
                        display_name=req.display_name,
                        preferred_action=req.action,
                        keep_subscribed=True,
                    )
                updated += 1
        async with runtime.scan_lock:
            runtime.snapshots.clear()
        return {
            "ok": True,
            "updated": updated,
            "items": runtime.store.list_sender_memory(),
        }

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
                    await runtime.get_snapshot(mode=req.mode, refresh=True, bypass_cache=False, sync_mail=False)
                except Exception as scan_exc:
                    runtime.jobs[job_id]["scan_warning"] = f"Actions completed, but inbox refresh failed: {scan_exc}"
                runtime.jobs[job_id]["done"] = runtime.jobs[job_id]["total"]
                runtime.jobs[job_id]["message"] = "Completed"
                runtime.jobs[job_id]["status"] = "completed"
            finally:
                runtime.touch_activity()

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
        await runtime.get_snapshot(mode="inbox", refresh=True, sync_mail=False)
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

        rfc_msg_id = (
            (target_item.header.rfc_message_id if target_item else "")
            or (str(entry.get("rfc_message_id") or "") if entry else "")
        )
        runtime.store.log_action(
            run_id="manual",
            account=account or "unknown",
            internal_id=str(internal_id or ""),
            sender=sender,
            subject=subject,
            action="mark_unsubscribed",
            status="ok",
            detail=f"{note}{' & archived to ' + arch_box if archived_ok else ''}",
            archive_mailbox=arch_box if archived_ok else "",
            rfc_message_id=rfc_msg_id,
        )
        return {
            "ok": True,
            "archived": archived_ok,
            "stats": runtime.store.get_stats(),
            "missed": runtime.store.list_missed_unsubscribes(False),
        }

    @app.get("/api/missed/{missed_id}/export")
    async def api_missed_export(missed_id: int) -> dict[str, Any]:
        entry = runtime.store.get_missed_unsubscribe(missed_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Missed entry not found")
        acct_emails: list[str] = []
        snap = runtime.snapshots.get("inbox") or runtime.snapshots.get("catchup")
        if snap:
            acct_emails = [addr for a in snap.accounts for addr in a.addresses]
        else:
            try:
                acct_emails = [
                    addr
                    for a in await asyncio.to_thread(runtime.bridge.list_accounts)
                    for addr in a.addresses
                ]
            except Exception:
                acct_emails = []
        return {
            "id": missed_id,
            "markdown": format_missed_bug_report(entry, account_emails=acct_emails),
            "entry": redact_missed_entry(entry, account_emails=acct_emails),
        }

    @app.get("/api/audit-log")
    async def api_audit_log(
        status: str = "all",
        run_id: str = "",
        search: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        return runtime.store.list_action_log(
            status=status,
            run_id=run_id,
            search=search,
            limit=limit,
        )

    @app.post("/api/audit-log/{entry_id}/undo")
    async def api_undo_archive(entry_id: int) -> dict[str, Any]:
        async with runtime.scan_lock:
            entry = runtime.store.get_action_log_entry(entry_id)
            if not entry:
                raise HTTPException(status_code=404, detail="Audit log entry not found")
            if entry.get("undone_at"):
                raise HTTPException(status_code=400, detail="Archive action was already undone")
            if not entry.get("can_undo_archive"):
                raise HTTPException(status_code=400, detail="Only completed archive actions can be undone")

            account = str(entry.get("account") or "").strip()
            if not account:
                raise HTTPException(status_code=400, detail="Missing account for audit entry")

            snap = runtime.snapshots.get("inbox") or runtime.snapshots.get("catchup")
            accounts_list = snap.accounts if snap else []
            if not accounts_list:
                try:
                    accounts_list = await asyncio.to_thread(runtime.bridge.list_accounts)
                except Exception:
                    accounts_list = []
            acct_archive_map = {a.name: a.archive_mailbox for a in accounts_list}
            acct_inbox_map = {a.name: a.inbox_mailbox for a in accounts_list}

            from_mailbox = (
                str(entry.get("archive_mailbox") or "").strip()
                or acct_archive_map.get(account)
                or runtime.config.archive_map.get(account)
                or "Archive"
            )
            inbox_mailbox = acct_inbox_map.get(account, "INBOX")

            try:
                restored = await asyncio.to_thread(
                    runtime.bridge.restore_to_inbox,
                    account,
                    from_mailbox,
                    str(entry.get("internal_id") or ""),
                    inbox_mailbox=inbox_mailbox,
                    rfc_message_id=str(entry.get("rfc_message_id") or ""),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Mail restore failed: {exc}") from exc

            if not restored:
                raise HTTPException(
                    status_code=404,
                    detail=f"Could not find message #{entry.get('internal_id')} in {account}/{from_mailbox}",
                )

            runtime.store.mark_action_undone(entry_id, detail_note=f"Restored to {account}/{inbox_mailbox}")
            runtime.snapshots.clear()
            return {
                "ok": True,
                "entry_id": entry_id,
                "restored_to": f"{account}/{inbox_mailbox}",
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
