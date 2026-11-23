"""FastAPI Review Board auth, preview, preference learning, and tray sweep tests."""

import time
from email.message import EmailMessage
from pathlib import Path

from fastapi.testclient import TestClient

from nollpost.config import load_config
from nollpost.models import AccountInfo, MailHeader
from nollpost.store import TriageStore
from nollpost.web import create_app


class FakeBridge:
    def __init__(self):
        self.archived: list[tuple[str, str, list[str]]] = []
        self.marked_read: list[tuple[str, str, list[str]]] = []

    def list_accounts(self) -> list[AccountInfo]:
        return [
            AccountInfo("iCloud", ["user@example.com"], "INBOX", "Archive", "Sent Messages"),
            AccountInfo("Google", ["alt@example.org"], "INBOX", "All e-post", "Skickat"),
        ]

    def scan_mailbox_metadata(self, account: str, mailbox: str = "INBOX", limit: int = 500, include_attachments: bool = True, days_back: int = 0):
        if account == "iCloud":
            return [
                MailHeader("iCloud", "INBOX", "10", "m10", "Black Week Deals", "Shop <info@shop.example>", "info@shop.example", "shop.example", "2026-11-19T12:00:00", False, False, 0),
                MailHeader("iCloud", "INBOX", "11", "m11", "Mystery Promo", "Mystery <news@mystery.example>", "news@mystery.example", "mystery.example", "2026-11-19T12:01:00", False, False, 0),
            ]
        return [
            MailHeader("Google", "INBOX", "20", "m20", "Release Update", "Creator <hello@creator.example>", "hello@creator.example", "creator.example", "2026-11-19T12:05:00", False, False, 0),
        ]

    def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
        msg = EmailMessage()
        if internal_id == "10":
            msg["Subject"] = "Black Week Deals"
            msg["From"] = "Shop <info@shop.example>"
            msg["List-Unsubscribe"] = "<https://shop.example/unsub>"
            msg.set_content("Avprenumerera")
            msg.add_alternative("<a href='https://shop.example/unsub'>Avprenumerera</a>", subtype="html")
        elif internal_id == "11":
            msg["Subject"] = "Mystery Promo"
            msg["From"] = "Mystery <news@mystery.example>"
            msg.set_content("50% discount black week")
            msg.add_alternative("<p>50% discount black week</p><a href='https://mystery.example/shop'>Shop</a>", subtype="html")
        else:
            msg["Subject"] = "Release Update"
            msg["From"] = "Creator <hello@creator.example>"
            msg.set_content("Update")
            msg.add_alternative("<a href='https://creator.example/post'>Read update</a>", subtype="html")
        return msg.as_bytes()

    def mark_read(self, account: str, mailbox: str, internal_ids: list[str]) -> list[str]:
        self.marked_read.append((account, mailbox, list(internal_ids)))
        return list(internal_ids)

    def mark_read_and_archive(self, account: str, from_mailbox: str, archive_mailbox: str, internal_ids: list[str], mark_read: bool = True) -> list[str]:
        self.archived.append((account, archive_mailbox, list(internal_ids)))
        return list(internal_ids)


def _setup_test_env(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[profile]\nwanted_domains = ["creator.example"]\n', encoding="utf-8")
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))


def test_web_auth_preview_preference_learning_and_missed_enqueue(tmp_path: Path, monkeypatch):
    _setup_test_env(tmp_path, monkeypatch)

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    bridge = FakeBridge()
    app, token = create_app(cfg, store, bridge, token="test-secret-token")
    with TestClient(app) as client:
        # Auth required
        assert client.get("/api/state").status_code == 401
        resp = client.get(f"/api/state?key={token}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 3

        # Preview summary checks 0 deleted and per-account Archive destination
        prev = client.post(
            "/api/execute/preview",
            headers={"X-Triage-Key": token},
            json={
                "mode": "inbox",
                "items": [
                    {"uid": "iCloud:INBOX:10", "action": "archive"},
                    {"uid": "iCloud:INBOX:11", "action": "archive"},
                ],
            },
        )
        assert prev.status_code == 200
        pjson = prev.json()
        assert pjson["deleted_count"] == 0
        assert pjson["archive_targets"]["iCloud"]["Archive"] == 2

        # Execute archive on #10 (opted out of unsub -> learned!) and #11 (no unsub -> enqueued!)
        run = client.post(
            "/api/execute/run",
            headers={"X-Triage-Key": token},
            json={
                "mode": "inbox",
                "items": [
                    {"uid": "iCloud:INBOX:10", "action": "archive"},
                    {"uid": "iCloud:INBOX:11", "action": "archive"},
                ],
            },
        )
        assert run.status_code == 200
        job_id = run.json()["job_id"]
        for _ in range(100):
            j = client.get(f"/api/jobs/{job_id}", headers={"X-Triage-Key": token}).json()
            if j["status"] == "completed":
                break
            time.sleep(0.05)

        assert j["status"] == "completed"
        assert ("iCloud", "Archive", ["10", "11"]) in bridge.archived
        mem = store.get_sender_memory("info@shop.example")
        assert mem is not None and mem.keep_subscribed is True and mem.preferred_action == "archive"
        missed = store.list_missed_unsubscribes()
        assert len(missed) == 1
        assert missed[0]["sender_domain"] == "mystery.example"


def test_cookie_refresh_repeat_offender_guard_and_archive_failure_and_missed_retry(tmp_path: Path, monkeypatch):
    _setup_test_env(tmp_path, monkeypatch)

    cfg = load_config()
    store = TriageStore(cfg.db_path)

    # Pre-record Shop as recently unsubscribed (within 48h -> default_action becomes "archive", NOT "archive_and_unsub")
    store.record_unsubscribed("info@shop.example", "shop.example", "Shop")

    class FailingArchiveBridge(FakeBridge):
        def mark_read_and_archive(self, account: str, from_mailbox: str, archive_mailbox: str, internal_ids: list[str], mark_read: bool = True) -> list[str]:
            # Only move "11", simulate "10" failing to move
            kept = [mid for mid in internal_ids if mid != "10"]
            self.archived.append((account, archive_mailbox, kept))
            return kept

    bridge = FailingArchiveBridge()
    app, token = create_app(cfg, store, bridge, token="new-token-123", allow_private_urls=True)
    with TestClient(app) as client:
        # Stale cookie must NOT block authentication when valid ?key= is supplied, and must refresh the cookie
        client.cookies.set("triage_key", "stale-old-token", domain="testserver.local", path="/")
        resp = client.get(f"/?key={token}")
        assert resp.status_code == 200
        assert client.cookies.get("triage_key", domain="testserver.local", path="/") == token
        assert client.get("/api/state").status_code == 200

        # Executing "archive" on Shop ("10", default_action="archive" because recently unsubbed) must NOT set keep_subscribed=True,
        # and because FailingArchiveBridge did not return "10", result for iCloud:INBOX:10 must report status="failed".
        run = client.post(
            "/api/execute/run",
            headers={"X-Triage-Key": token},
            json={
                "mode": "inbox",
                "items": [
                    {"uid": "iCloud:INBOX:10", "action": "archive"},
                    {"uid": "iCloud:INBOX:11", "action": "archive"},
                ],
            },
        )
        assert run.status_code == 200
        job_id = run.json()["job_id"]
        for _ in range(100):
            j = client.get(f"/api/jobs/{job_id}", headers={"X-Triage-Key": token}).json()
            if j["status"] == "completed":
                break
            time.sleep(0.05)
        assert j["status"] == "completed"
        by_uid = {r["uid"]: r for r in j["results"]}
        assert by_uid["iCloud:INBOX:10"]["status"] == "failed"
        assert by_uid["iCloud:INBOX:11"]["status"] == "ok"
        shop_mem = store.get_sender_memory("info@shop.example")
        assert shop_mem is not None and shop_mem.keep_subscribed is False

        # Missed retry against localhost server records clean sender_email and resolves missed entry
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class OkHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                body = b"<html><body><h1>You have been unsubscribed</h1></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), OkHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            missed_id = store.list_missed_unsubscribes()[0]["id"]
            retry = client.post(
                f"/api/missed/{missed_id}/retry",
                headers={"X-Triage-Key": token},
                json={"url": f"http://127.0.0.1:{srv.server_address[1]}/unsub", "headless": True},
            )
            assert retry.status_code == 200
            assert retry.json()["outcome"]["status"] == "success"
            assert store.list_missed_unsubscribes(include_resolved=False) == []
            mystery_mem = store.get_sender_memory("news@mystery.example")
            assert mystery_mem is not None and mystery_mem.unsub_count >= 1
        finally:
            srv.shutdown()


def test_sweep_opened_tray_persists_partial_success_on_later_error(tmp_path: Path, monkeypatch):
    _setup_test_env(tmp_path, monkeypatch)

    import pytest
    from nollpost.executor import BatchExecutor

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    store.add_opened_tray_item(
        message_uid="iCloud:INBOX:10",
        account="iCloud",
        mailbox="INBOX",
        internal_id="10",
        rfc_message_id="m10",
        subject="S1",
        sender="A",
        opened_target="https://a.test",
    )
    store.add_opened_tray_item(
        message_uid="Google:INBOX:20",
        account="Google",
        mailbox="INBOX",
        internal_id="20",
        rfc_message_id="m20",
        subject="S2",
        sender="B",
        opened_target="https://b.test",
    )

    class PartialFailBridge(FakeBridge):
        def mark_read_and_archive(self, account: str, from_mailbox: str, archive_mailbox: str, internal_ids: list[str], mark_read: bool = True) -> list[str]:
            if account == "Google":
                raise RuntimeError("Gmail temporary error")
            return list(internal_ids)

    ex = BatchExecutor(cfg, store, PartialFailBridge())
    with pytest.raises(RuntimeError, match="Gmail temporary error"):
        ex.sweep_opened_tray(["iCloud:INBOX:10", "Google:INBOX:20"], {"iCloud": "Archive", "Google": "[Gmail]/All e-post"})

    remaining = store.list_active_tray()
    assert len(remaining) == 1
    assert remaining[0]["message_uid"] == "Google:INBOX:20"
    assert store.get_stats()["total_archived"] == 1


def test_chunked_archive_progress_partial_timeout_and_tray_reopen_enrichment(tmp_path: Path, monkeypatch):
    _setup_test_env(tmp_path, monkeypatch)

    import asyncio
    from nollpost.executor import BatchExecutor
    from nollpost.models import ClassifiedItem, ExtractedEmailInfo

    cfg = load_config()
    store = TriageStore(cfg.db_path)

    class ChunkedTimeoutBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.chunk_calls: list[list[str]] = []

        def mark_read_and_archive(self, account: str, from_mailbox: str, archive_mailbox: str, internal_ids: list[str], mark_read: bool = True) -> list[str]:
            self.chunk_calls.append(list(internal_ids))
            # Chunk 1 (1..8) succeeds; Chunk 2 (9..16) times out after 9..12 left INBOX; Chunk 3 (17..20) succeeds
            if len(self.chunk_calls) == 2:
                raise TimeoutError("osascript timed out")
            return list(internal_ids)

        def list_mailbox_ids(self, account: str, mailbox: str = "INBOX") -> set[str]:
            # Simulate 9..12 having left INBOX before timeout while 13..16 remained
            return {"13", "14", "15", "16"}

        def synchronize_account(self, account: str, timeout: int = 8) -> bool:
            return True

    bridge = ChunkedTimeoutBridge()
    ex = BatchExecutor(cfg, store, bridge)
    pairs = [
        (
            ClassifiedItem(
                uid=f"iCloud:INBOX:{i}",
                header=MailHeader("iCloud", "INBOX", str(i), f"m{i}", f"Sub {i}", f"S{i} <s{i}@example.se>", f"s{i}@example.se", "example.se", "2026-11-20T08:00:00", False, False, 0),
                extracted=ExtractedEmailInfo(),
                category="newsletters",
                subcategory="Newsletter",
                default_action="archive",
                allowed_actions=["no_action", "archive"],
                archive_mailbox="Archive",
            ),
            "archive",
        )
        for i in range(1, 21)
    ]
    progress_events: list[dict] = []
    results = asyncio.run(ex.execute_batch(pairs, {"iCloud": ["user@example.com"]}, on_progress=progress_events.append))
    assert [len(c) for c in bridge.chunk_calls] == [8, 8, 4]
    by_id = {r.internal_id: r for r in results}
    # Chunk 1 (1..8), reconciled (9..12), Chunk 3 (17..20) = 16 archived; 13..16 failed
    assert all(by_id[str(i)].status == "ok" for i in range(1, 13))
    assert all(by_id[str(i)].status == "failed" for i in range(13, 17))
    assert all(by_id[str(i)].status == "ok" for i in range(17, 21))
    assert store.get_stats()["total_archived"] == 16
    assert any("Step 1/3" in ev["message"] for ev in progress_events)
    assert any("Step 2/3" in ev["message"] and "chunk" in ev["message"] for ev in progress_events)

    # Verify Safety Tray preview enriches tray item with latest_pertinent_url from live inbox scan
    store.add_opened_tray_item(
        message_uid="Google:INBOX:20",
        account="Google",
        mailbox="INBOX",
        internal_id="20",
        rfc_message_id="m20",
        subject="Release Update",
        sender="Creator",
        opened_target="https://old-wrong-link.example/",
    )
    app, token = create_app(cfg, store, FakeBridge(), token="tray-tok")
    with TestClient(app) as client:
        st = client.get("/api/state", headers={"X-Triage-Key": token}).json()
        assert st["tray"][0]["latest_pertinent_url"] == "https://creator.example/post"
        prev = client.post("/api/tray/sweep/preview", headers={"X-Triage-Key": token}, json={"uids": ["Google:INBOX:20"]}).json()
        assert prev["items"][0]["opened_target"] == "https://old-wrong-link.example/"
        assert prev["items"][0]["latest_pertinent_url"] == "https://creator.example/post"

        # Verify manual "Mark Unsubscribed & Archive" records sender unsubscribed, resolves missed queue, and archives
        mid = store.enqueue_missed_unsubscribe(
            account="iCloud",
            mailbox="INBOX",
            internal_id="10",
            rfc_message_id="m10",
            sender="Shop <info@shop.example>",
            sender_domain="shop.example",
            subject="Black Week Deals",
            stage="playwright",
            failure_reason="No confirmation phrase found",
            candidate_links=[],
            headers_snapshot="",
            footer_html_snapshot="",
        )
        mark_resp = client.post(
            "/api/unsub/mark-succeeded",
            headers={"X-Triage-Key": token},
            json={"missed_id": mid, "archive": True},
        )
        assert mark_resp.status_code == 200
        mdata = mark_resp.json()
        assert mdata["ok"] is True
        assert mdata["archived"] is True
        assert mdata["missed"] == []
        mem = store.get_sender_memory("info@shop.example")
        assert mem is not None and mem.unsub_count >= 1
        assert mdata["stats"]["total_archived"] == 17


def test_ui_and_api_support_arbitrary_accounts_and_consulting_visibility(tmp_path: Path, monkeypatch):
    _setup_test_env(tmp_path, monkeypatch)

    class ThreeAccountBridge(FakeBridge):
        def list_accounts(self) -> list[AccountInfo]:
            return [
                AccountInfo("Fastmail", ["a@example.com"], "INBOX", "Archive", "Sent"),
                AccountInfo("Work", ["b@example.com"], "INBOX", "Archive", "Sent Items"),
                AccountInfo("Personal", ["c@example.com"], "INBOX", "All Mail", "Sent"),
            ]

        def scan_mailbox_metadata(self, account: str, mailbox: str = "INBOX", limit: int = 500, include_attachments: bool = True, days_back: int = 0):
            return [
                MailHeader(account, "INBOX", f"{account}-1", f"m-{account}", f"Hello {account}", "Sender <news@shop.example>", "news@shop.example", "shop.example", "2026-11-20T09:00:00", False, False, 0)
            ]

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    app, token = create_app(cfg, store, ThreeAccountBridge(), token="multi-tok")
    with TestClient(app) as client:
        st = client.get("/api/state", headers={"X-Triage-Key": token}).json()
        assert st["consulting_enabled"] is False
        assert sorted(a["name"] for a in st["accounts"]) == ["Fastmail", "Personal", "Work"]
        assert sorted(st["briefing"]["per_account"].keys()) == ["Fastmail", "Personal", "Work"]

        app_js = client.get("/static/app.js").text
        en_locale = client.get("/static/locales/en.json").text
        style_css = client.get("/static/style.css").text
        assert "Both (" not in app_js
        assert "Both (" not in en_locale
        assert "filter.all_accounts" in app_js
        assert "All Accounts (" in en_locale
        assert "accountToneClass" in app_js
        assert ".acct-c0" in style_css and ".acct-c5" in style_css
        assert ".acct.icloud" not in style_css

    (tmp_path / "config.toml").write_text("[consulting]\nenabled = true\n", encoding="utf-8")
    cfg_consult = load_config(ensure_files=False)
    app2, token2 = create_app(cfg_consult, store, ThreeAccountBridge(), token="multi-tok-2")
    with TestClient(app2) as client2:
        st2 = client2.get("/api/state", headers={"X-Triage-Key": token2}).json()
        assert st2["consulting_enabled"] is True


def test_scan_progress_and_ui_safety_controls(tmp_path: Path, monkeypatch):
    _setup_test_env(tmp_path, monkeypatch)

    from nollpost.scanner import run_scan

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    bridge = FakeBridge()

    events: list[dict] = []
    run_scan(cfg, store, bridge, mode="inbox", on_progress=events.append)
    phases = [e["phase"] for e in events]
    assert "accounts" in phases
    assert "mailbox" in phases
    assert "done" in phases

    catchup_events: list[dict] = []
    run_scan(cfg, store, bridge, mode="catchup", on_progress=catchup_events.append)
    cphases = [e["phase"] for e in catchup_events]
    assert "accounts" in cphases
    assert "mailbox" in cphases
    assert "done" in cphases

    store.add_opened_tray_item(
        message_uid="iCloud:INBOX:10",
        account="iCloud",
        mailbox="INBOX",
        internal_id="10",
        rfc_message_id="m10",
        subject="Deal 1",
        sender="Shop",
        opened_target="https://shop.example/1",
    )
    store.add_opened_tray_item(
        message_uid="iCloud:INBOX:11",
        account="iCloud",
        mailbox="INBOX",
        internal_id="11",
        rfc_message_id="m11",
        subject="Deal 2",
        sender="Mystery",
        opened_target="https://mystery.example/2",
    )

    app, token = create_app(cfg, store, bridge, token="ui-tok")
    with TestClient(app) as client:
        # Unauthenticated access to /api/scan/status blocked
        assert client.get("/api/scan/status").status_code == 401
        status = client.get("/api/scan/status", headers={"X-Triage-Key": token})
        assert status.status_code == 200
        assert status.json()["active"] is False

        # Per-item tray dismiss leaves the other item intact
        resp = client.post(
            "/api/tray/dismiss",
            headers={"X-Triage-Key": token},
            json={"uids": ["iCloud:INBOX:10"]},
        )
        assert resp.status_code == 200
        remaining = resp.json()["tray"]
        assert [t["message_uid"] for t in remaining] == ["iCloud:INBOX:11"]

        html = client.get(f"/?key={token}").text
        favicon_resp = client.get("/static/favicon.svg")
        assert favicon_resp.status_code == 200
        assert "Snabel-noll" in favicon_resp.text
        assert 'id="outer-swirl"' in favicon_resp.text
        assert 'id="inner-zero"' in favicon_resp.text
        assert 'id="zero-slash"' in favicon_resp.text
        assert 'href="/static/favicon.svg"' in html
        assert 'class="brand-mark"' in html
        app_js = client.get("/static/app.js").text
        en_locale = client.get("/static/locales/en.json").text
        assert 'id="help-btn"' in html
        assert 'id="btn-reset-actions"' in html
        assert "btn-reset-filters" in app_js
        assert "confirm.tray_dismiss_title" in app_js
        assert "Confirm Dismiss Safety Tray" in en_locale
        assert "data-tray-dismiss-uid" in app_js
        assert "confirm.warn_outbound" in app_js
        assert "Outbound Email Warning" in en_locale
        assert "System Settings" in en_locale
        assert "/api/scan/status" in app_js
        assert "\u2014" not in html
        assert "\u2014" not in app_js
        assert "\u2014" not in en_locale


def test_serve_startup_nonblocking_preload_and_terminal_guidance(tmp_path: Path, monkeypatch, capsys):
    _setup_test_env(tmp_path, monkeypatch)

    from nollpost import cli

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    bridge = FakeBridge()
    app, token = create_app(cfg, store, bridge, token="preload-tok", preload=True)
    with TestClient(app) as client:
        st = client.get("/api/state", headers={"X-Triage-Key": token})
        assert st.status_code == 200
        assert len(st.json()["items"]) == 3

    monkeypatch.setattr(cli, "AppleMailBridge", lambda **_kw: FakeBridge())
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **kw: None)
    rc = cli.main(["serve", "--port", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "automatically" in out
    assert "Review Board URL:" in out

    # Unbound port + immediate shutdown cancels browser task cleanly without invoking Popen
    import socket
    from nollpost import web as web_mod

    opened_cmds: list[list[str]] = []
    monkeypatch.setattr(web_mod.subprocess, "Popen", lambda cmd, *a, **kw: opened_cmds.append(list(cmd)))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]

    app_cancel, _ = create_app(
        cfg,
        store,
        bridge,
        token="cancel-tok",
        open_browser_url=f"http://127.0.0.1:{closed_port}/?key=cancel-tok",
    )
    with TestClient(app_cancel):
        pass
    assert opened_cmds == []


def test_preload_syncs_new_mail_before_scan_and_disables_api_http_cache(tmp_path: Path, monkeypatch):
    _setup_test_env(tmp_path, monkeypatch)

    monkeypatch.setattr("nollpost.scanner.time.sleep", lambda _s: None)

    class WakeSyncBridge(FakeBridge):
        def __init__(self):
            super().__init__()
            self.sync_calls = 0
            self.synced = False

        def check_for_new_mail(self, timeout: int = 8) -> bool:
            self.sync_calls += 1
            self.synced = True
            return True

        def scan_mailbox_metadata(self, account: str, mailbox: str = "INBOX", limit: int = 500, include_attachments: bool = True, days_back: int = 0):
            base = super().scan_mailbox_metadata(account, mailbox, limit, include_attachments, days_back)
            if account == "iCloud" and self.synced:
                base.append(
                    MailHeader(
                        "iCloud",
                        "INBOX",
                        "12",
                        "m12",
                        "Brand New Mail",
                        "New <new@example.com>",
                        "new@example.com",
                        "example.com",
                        "2026-11-23T09:00:00",
                        False,
                        False,
                        0,
                    )
                )
            return base

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    bridge = WakeSyncBridge()
    app, token = create_app(cfg, store, bridge, token="wake-tok", preload=True)
    with TestClient(app) as client:
        st = client.get("/api/state", headers={"X-Triage-Key": token})
        assert st.status_code == 200
        assert st.headers.get("cache-control") == "no-store"
        uids = {item["uid"] for item in st.json()["items"]}
        assert "iCloud:INBOX:12" in uids
        assert bridge.sync_calls == 1

        # Post-execute rescan skips redundant check_for_new_mail
        run = client.post(
            "/api/execute/run",
            headers={"X-Triage-Key": token},
            json={"mode": "inbox", "items": [{"uid": "iCloud:INBOX:10", "action": "archive"}]},
        )
        assert run.status_code == 200
        job_id = run.json()["job_id"]
        for _ in range(100):
            j = client.get(f"/api/jobs/{job_id}", headers={"X-Triage-Key": token}).json()
            if j["status"] == "completed":
                break
            time.sleep(0.05)
        assert j["status"] == "completed"
        assert bridge.sync_calls == 1

        app_js = client.get("/static/app.js").text
        assert 'cache: "no-store"' in app_js
        assert 'params.get("refresh") === "1"' in app_js
    store.close()


def test_same_sender_action_offer_in_chromium(tmp_path: Path, monkeypatch):
    import asyncio
    import socket
    import threading
    import uvicorn
    from playwright.async_api import async_playwright

    _setup_test_env(tmp_path, monkeypatch)

    class MultiSenderBridge(FakeBridge):
        def scan_mailbox_metadata(self, account: str, mailbox: str = "INBOX", limit: int = 500, include_attachments: bool = True, days_back: int = 0):
            if account == "iCloud":
                return [
                    MailHeader("iCloud", "INBOX", "10", "m10", "Deal One", "Shop <info@shop.example>", "info@shop.example", "shop.example", "2026-11-19T12:00:00", False, False, 0),
                    MailHeader("iCloud", "INBOX", "11", "m11", "Deal Two", "Shop <info@shop.example>", "info@shop.example", "shop.example", "2026-11-19T12:01:00", False, False, 0),
                    MailHeader("iCloud", "INBOX", "12", "m12", "Deal Three", "Shop <info@shop.example>", "info@shop.example", "shop.example", "2026-11-19T12:02:00", False, False, 0),
                ]
            return super().scan_mailbox_metadata(account, mailbox, limit, include_attachments, days_back)

        def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
            if internal_id in ("10", "11", "12"):
                msg = EmailMessage()
                msg["Subject"] = f"Deal {internal_id}"
                msg["From"] = "Shop <info@shop.example>"
                msg["List-Unsubscribe"] = "<https://shop.example/unsub>"
                msg.set_content("Avprenumerera")
                msg.add_alternative("<a href='https://shop.example/unsub'>Avprenumerera</a>", subtype="html")
                return msg.as_bytes()
            return super().get_raw_source(account, mailbox, internal_id)

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    app, token = create_app(cfg, store, MultiSenderBridge(), token="sender-offer-tok")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    async def _run():
        try:
            deadline = time.monotonic() + 10.0
            while not server.started and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert server.started

            page_errors: list[str] = []
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(locale="en-US")
                page = await context.new_page()
                page.on("pageerror", lambda err: page_errors.append(str(err)))

                await page.goto(f"http://127.0.0.1:{port}/?key={token}&refresh=1", wait_until="domcontentloaded")
                await page.locator("text=Deal One").wait_for(timeout=8000)
                # ?refresh=1 stripped cleanly after initial load
                assert "refresh=1" not in page.url

                # All 3 Shop emails default to archive_and_unsub
                for uid in ("iCloud:INBOX:10", "iCloud:INBOX:11", "iCloud:INBOX:12"):
                    btn = page.locator(f'button[data-uid="{uid}"][data-act="archive_and_unsub"]')
                    assert "active" in (await btn.get_attribute("class") or "")

                # Switch #10 from archive_and_unsub -> archive; offer appears for the 2 others
                await page.locator('button[data-uid="iCloud:INBOX:10"][data-act="archive"]').click()
                offer = page.locator(".sender-apply-offer")
                await offer.wait_for(state="visible", timeout=3000)
                offer_text = await offer.inner_text()
                assert "2 other messages" in offer_text
                assert "Archive" in offer_text

                # Applying switches #11 and #12 to archive, clears offer, leaves Google:INBOX:20 untouched
                await page.locator("[data-apply-sender-offer]").click()
                assert await offer.count() == 0
                for uid in ("iCloud:INBOX:10", "iCloud:INBOX:11", "iCloud:INBOX:12"):
                    btn = page.locator(f'button[data-uid="{uid}"][data-act="archive"]')
                    assert "active" in (await btn.get_attribute("class") or "")
                other_btn = page.locator('button[data-uid="Google:INBOX:20"][data-act="archive"]')
                assert "active" not in (await other_btn.get_attribute("class") or "")

                # Switching #10 to mark_read shows offer again; dismissing leaves #11/#12 on archive
                await page.locator('button[data-uid="iCloud:INBOX:10"][data-act="mark_read"]').click()
                await offer.wait_for(state="visible", timeout=3000)
                await page.locator("[data-dismiss-sender-offer]").click()
                assert await offer.count() == 0
                for uid in ("iCloud:INBOX:11", "iCloud:INBOX:12"):
                    btn = page.locator(f'button[data-uid="{uid}"][data-act="archive"]')
                    assert "active" in (await btn.get_attribute("class") or "")

                assert page_errors == []
                await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            store.close()

    asyncio.run(_run())


def test_sort_and_group_by_controls_in_chromium(tmp_path: Path, monkeypatch):
    import asyncio
    import socket
    import threading
    import uvicorn
    from playwright.async_api import async_playwright

    _setup_test_env(tmp_path, monkeypatch)

    class SortGroupBridge(FakeBridge):
        def scan_mailbox_metadata(self, account: str, mailbox: str = "INBOX", limit: int = 500, include_attachments: bool = True, days_back: int = 0):
            if account == "iCloud":
                return [
                    MailHeader("iCloud", "INBOX", "10", "m10", "Alpha Deal", "Shop <info@shop.example>", "info@shop.example", "shop.example", "2026-11-19T10:00:00", True, False, 0),
                    MailHeader("iCloud", "INBOX", "11", "m11", "Zebra Deal", "Shop <info@shop.example>", "info@shop.example", "shop.example", "2026-11-19T12:00:00", False, False, 0),
                ]
            return [
                MailHeader("Google", "INBOX", "20", "m20", "Beta Release", "Creator <hello@creator.example>", "hello@creator.example", "creator.example", "2026-11-19T11:00:00", True, False, 0),
            ]

        def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
            if internal_id in ("10", "11"):
                msg = EmailMessage()
                msg["Subject"] = "Alpha Deal" if internal_id == "10" else "Zebra Deal"
                msg["From"] = "Shop <info@shop.example>"
                msg["List-Unsubscribe"] = "<https://shop.example/unsub>"
                msg.set_content("Avprenumerera")
                msg.add_alternative("<a href='https://shop.example/unsub'>Avprenumerera</a>", subtype="html")
                return msg.as_bytes()
            return super().get_raw_source(account, mailbox, internal_id)

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    app, token = create_app(cfg, store, SortGroupBridge(), token="sort-group-tok")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    async def _run():
        try:
            deadline = time.monotonic() + 10.0
            while not server.started and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert server.started

            page_errors: list[str] = []
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(locale="en-US")
                page = await context.new_page()
                page.on("pageerror", lambda err: page_errors.append(str(err)))

                await page.goto(f"http://127.0.0.1:{port}/?key={token}", wait_until="domcontentloaded")
                await page.locator("text=Zebra Deal").wait_for(timeout=8000)

                # Default: grouped by sender, sorted newest first
                sender_btn = page.locator('#group-by-toggle button[data-group="sender"]')
                assert "active" in (await sender_btn.get_attribute("class") or "")
                assert await page.locator("tr.group-header-row").count() == 1
                assert "2 messages" in (await page.locator("tr.group-header-row").first.inner_text())

                subjects = await page.locator("tbody tr:not(.group-header-row) td:nth-child(4) strong").all_inner_texts()
                assert subjects == ["Zebra Deal", "Alpha Deal", "Beta Release"]

                # Group checkbox selects all rows in Shop group (2 items; 1 has non-noop default action)
                await page.locator("input.group-cb").first.check()
                assert await page.locator("input.row-cb:checked").count() == 2
                assert "Execute Selected (1)" in (await page.locator("#btn-exec-selected").inner_text())

                # Switch Group to Category: 2 category groups (Newsletters & Bots, Updates & Comments)
                await page.locator('#group-by-toggle button[data-group="category"]').click()
                assert await page.locator("tr.group-header-row").count() == 2

                # Switch Group to None and sort Subject (A-Z)
                await page.locator('#group-by-toggle button[data-group="none"]').click()
                assert await page.locator("tr.group-header-row").count() == 0
                await page.locator("#sort-by-select").select_option("subject_asc")
                subjects_az = await page.locator("tbody tr td:nth-child(4) strong").all_inner_texts()
                assert subjects_az == ["Alpha Deal", "Beta Release", "Zebra Deal"]

                # Sort Oldest first
                await page.locator("#sort-by-select").select_option("date_asc")
                subjects_oldest = await page.locator("tbody tr td:nth-child(4) strong").all_inner_texts()
                assert subjects_oldest == ["Alpha Deal", "Beta Release", "Zebra Deal"]

                # Sort Unread first (Zebra Deal is unread, others are read)
                await page.locator("#sort-by-select").select_option("unread_first")
                subjects_unread = await page.locator("tbody tr td:nth-child(4) strong").all_inner_texts()
                assert subjects_unread[0] == "Zebra Deal"

                assert page_errors == []
                await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            store.close()

    asyncio.run(_run())


def test_auto_preferences_and_settings_endpoints(tmp_path: Path, monkeypatch):
    _setup_test_env(tmp_path, monkeypatch)
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    app, token = create_app(cfg, store, FakeBridge(), token="auto-pref-tok")
    headers = {"X-Triage-Key": token}

    with TestClient(app) as client:
        pref = client.post(
            "/api/preferences",
            headers=headers,
            json={
                "sender_email": "alerts@bank.example",
                "sender_domain": "bank.example",
                "display_name": "MyBank",
                "preference": "auto_archive",
            },
        )
        assert pref.status_code == 200
        rules = client.get("/api/sender-memory?kind=auto_archive", headers=headers).json()
        assert [r["sender_key"] for r in rules["items"]] == ["alerts@bank.example"]

        bulk = client.post(
            "/api/sender-memory/bulk",
            headers=headers,
            json={"sender_keys": ["other@bank.example"], "action": "auto_archive"},
        )
        assert bulk.status_code == 200
        assert len(client.get("/api/sender-memory?kind=auto_archive", headers=headers).json()["items"]) == 2

        settings_before = client.get("/api/settings", headers=headers).json()
        assert settings_before["automation"]["interval_minutes"] == 60
        assert settings_before["automation"]["auto_decline_consulting"] is False

        put_res = client.put(
            "/api/settings",
            headers=headers,
            json={
                "automation": {
                    "interval_minutes": 30,
                    "auto_decline_consulting": True,
                    "auto_archive_transactional": True,
                }
            },
        )
        assert put_res.status_code == 200
        assert put_res.json()["settings"]["automation"]["interval_minutes"] == 30
        assert put_res.json()["settings"]["automation"]["auto_decline_consulting"] is True


def test_auto_run_digest_and_ack_endpoints(tmp_path: Path, monkeypatch):
    import json as _json

    _setup_test_env(tmp_path, monkeypatch)

    class AutoWebBridge(FakeBridge):
        def check_for_new_mail(self) -> bool:
            return False

        def scan_mailbox_metadata(self, account: str, mailbox: str = "INBOX", limit: int = 500, include_attachments: bool = True, days_back: int = 0):
            if account == "iCloud":
                return [
                    MailHeader("iCloud", "INBOX", "10", "m10", "Card notice", "MyBank <alerts@bank.example>", "alerts@bank.example", "bank.example", "2026-11-23T09:00:00", False, False, 0),
                ]
            return []

        def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
            msg = EmailMessage()
            msg["Subject"] = "Card notice"
            msg["From"] = "MyBank <alerts@bank.example>"
            msg.set_content("Transaction notice")
            return msg.as_bytes()

        def mark_read_and_archive(self, account: str, mailbox: str, archive_mailbox: str, internal_ids: list[str], *, mark_read: bool = True) -> list[str]:
            return list(internal_ids)

        def synchronize_account(self, account: str, timeout: int = 8) -> None:
            return None

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    store.record_sender_preference("alerts@bank.example", "MyBank", "auto_archive", keep_subscribed=True)
    app, token = create_app(cfg, store, AutoWebBridge(), token="auto-run-tok")
    headers = {"X-Triage-Key": token}

    with TestClient(app) as client:
        run_res = client.post("/api/auto/run", headers=headers)
        assert run_res.status_code == 200
        run_data = run_res.json()
        assert run_data["status"] == "ok"
        assert run_data["summary"] == {"archived": 1, "declined": 0, "failed": 0}
        assert run_data["unseen_count"] == 1

        digest = client.get("/api/auto/digest", headers=headers).json()
        assert digest["unseen_count"] == 1
        assert [it["subject"] for it in digest["items"]] == ["Card notice"]

        auto_logs = client.get("/api/audit-log?source=auto", headers=headers).json()
        assert len(auto_logs["items"]) == 1
        assert auto_logs["items"][0]["source"] == "auto"

        ack = client.post("/api/auto/ack", headers=headers, json={"max_log_id": digest["max_log_id"]})
        assert ack.status_code == 200
        assert ack.json()["unseen_count"] == 0

        digest_after = client.get("/api/auto/digest", headers=headers).json()
        assert digest_after["unseen_count"] == 0
        assert digest_after["items"] == []

        marker = _json.loads((cfg.data_dir / "auto_status.json").read_text(encoding="utf-8"))
        assert marker["unseen_count"] == 0
