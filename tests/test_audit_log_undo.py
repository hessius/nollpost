"""Tests for Issue #10: Audit Log viewer, batch filtering, and one-click Undo Archive."""

from __future__ import annotations

import sqlite3
import time
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nollpost.config import load_config
from nollpost.mail_bridge import AppleMailBridge
from nollpost.models import AccountInfo, MailHeader
from nollpost.store import TriageStore
from nollpost.web import create_app


class _AuditFakeBridge:
    def __init__(self):
        self.inbox_ids = {"10", "11"}
        self.archived_ids: set[str] = set()
        self.restored: list[tuple[str, str, str, str]] = []

    def list_accounts(self) -> list[AccountInfo]:
        return [
            AccountInfo("iCloud", ["user@example.com"], "INBOX", "Archive", "Sent Messages"),
        ]

    def scan_mailbox_metadata(
        self,
        account: str,
        mailbox: str = "INBOX",
        limit: int = 500,
        include_attachments: bool = True,
        days_back: int = 0,
    ):
        headers = []
        if "10" in self.inbox_ids:
            headers.append(
                MailHeader(
                    "iCloud",
                    "INBOX",
                    "10",
                    "<m10@shop.example>",
                    "Weekly Deals",
                    "Shop <news@shop.example>",
                    "news@shop.example",
                    "shop.example",
                    "2026-11-22T08:00:00",
                    False,
                    False,
                    0,
                )
            )
        if "11" in self.inbox_ids:
            headers.append(
                MailHeader(
                    "iCloud",
                    "INBOX",
                    "11",
                    "<m11@creator.example>",
                    "Creator Update",
                    "Creator <hello@creator.example>",
                    "hello@creator.example",
                    "creator.example",
                    "2026-11-22T08:05:00",
                    False,
                    False,
                    0,
                )
            )
        return headers

    def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
        msg = EmailMessage()
        if internal_id == "10":
            msg["Subject"] = "Weekly Deals"
            msg["From"] = "Shop <news@shop.example>"
            msg.set_content("Discount deals")
        else:
            msg["Subject"] = "Creator Update"
            msg["From"] = "Creator <hello@creator.example>"
            msg.set_content("Read new post")
            msg.add_alternative("<a href='https://creator.example/p/1'>Read post</a>", subtype="html")
        return msg.as_bytes()

    def mark_read(self, account: str, mailbox: str, internal_ids: list[str]) -> list[str]:
        return [mid for mid in internal_ids if mid in self.inbox_ids]

    def mark_read_and_archive(
        self,
        account: str,
        from_mailbox: str,
        archive_mailbox: str,
        internal_ids: list[str],
        mark_read: bool = True,
    ) -> list[str]:
        moved = [mid for mid in internal_ids if mid in self.inbox_ids]
        for mid in moved:
            self.inbox_ids.discard(mid)
            self.archived_ids.add(mid)
        return moved

    def restore_to_inbox(
        self,
        account: str,
        from_mailbox: str,
        internal_id: str,
        *,
        inbox_mailbox: str = "INBOX",
        rfc_message_id: str = "",
    ) -> bool:
        self.restored.append((account, from_mailbox, internal_id, inbox_mailbox))
        if internal_id in self.archived_ids:
            self.archived_ids.discard(internal_id)
            self.inbox_ids.add(internal_id)
            return True
        return False


def test_store_action_log_migration_filtering_and_undo(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            account TEXT NOT NULL,
            internal_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            subject TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        INSERT INTO action_log (run_id, account, internal_id, sender, subject, action, status, detail, created_at)
        VALUES ('old-run', 'iCloud', '55', 'Old <old@example.com>', 'Old Promo', 'archive', 'ok', 'Archived', '2026-11-20T10:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    store = TriageStore(db_path)
    store.log_action(
        run_id="run-a",
        account="iCloud",
        internal_id="56",
        sender="Fail <f@example.com>",
        subject="Timeout",
        action="archive",
        status="failed",
        detail="Timed out",
    )
    store.log_action(
        run_id="run-a",
        account="Google",
        internal_id="57",
        sender="Captcha <c@example.com>",
        subject="Promo",
        action="archive_and_unsub",
        status="blocked_captcha",
        detail="CAPTCHA hit",
    )
    store.log_action(
        run_id="run-a",
        account="iCloud",
        internal_id="58",
        sender="Draft <d@example.com>",
        subject="Consult",
        action="draft_decline",
        status="drafted",
        detail="Opened draft",
    )

    all_res = store.list_action_log()
    assert len(all_res["items"]) == 4
    assert len(all_res["runs"]) == 2
    by_run = {r["run_id"]: r for r in all_res["runs"]}
    assert by_run["run-a"]["failed"] == 1
    assert by_run["run-a"]["blocked_captcha"] == 1
    assert by_run["run-a"]["drafted"] == 1

    # Legacy migrated row can be undone
    old_entry = store.get_action_log_entry(1)
    assert old_entry is not None
    assert old_entry["can_undo_archive"] is True

    failed_only = store.list_action_log(status="failed")
    assert [i["internal_id"] for i in failed_only["items"]] == ["56"]

    store.mark_action_undone(1, detail_note="Restored to iCloud/INBOX")
    assert store.mark_action_undone(1, detail_note="Duplicate") is False
    updated = store.get_action_log_entry(1)
    assert updated is not None
    assert updated["can_undo_archive"] is False
    assert updated["undone_at"] is not None
    assert "Restored to iCloud/INBOX" in updated["detail"]
    assert "Duplicate" not in updated["detail"]
    # An undo_archive audit row is appended once
    undo_rows = store.list_action_log(search="undo_archive")["items"]
    assert len(undo_rows) == 1
    assert undo_rows[0]["action"] == "undo_archive"

    # Insert >1000 filler rows and verify older entries remain searchable and filterable in SQL
    for idx in range(1050):
        store.log_action(
            run_id="bulk-run",
            account="iCloud",
            internal_id=str(1000 + idx),
            sender="Bulk <b@example.com>",
            subject=f"Filler {idx}",
            action="mark_read",
            status="ok",
        )
    old_found = store.list_action_log(run_id="old-run", search="Old Promo")
    assert len(old_found["items"]) == 1
    assert old_found["items"][0]["id"] == 1
    bulk_summary = next(r for r in old_found["runs"] if r["run_id"] == "bulk-run")
    assert bulk_summary["total"] == 1050


def test_mail_bridge_restore_to_inbox_guards_trash_and_builds_applescript(monkeypatch):
    scripts: list[str] = []

    def fake_osascript(script: str, timeout: int = 70) -> str:
        scripts.append(script)
        return "moved"

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_osascript)
    bridge = AppleMailBridge()

    with pytest.raises(ValueError, match="prohibited"):
        bridge.restore_to_inbox("iCloud", "Trash", "10", inbox_mailbox="INBOX")
    with pytest.raises(ValueError, match="prohibited"):
        bridge.restore_to_inbox("iCloud", "Archive", "10", inbox_mailbox="Papperskorgen")
    with pytest.raises(ValueError, match="digits only"):
        bridge.restore_to_inbox("iCloud", "Archive", "bad-id")

    ok = bridge.restore_to_inbox(
        "iCloud",
        "Archive",
        "123",
        inbox_mailbox="INBOX",
        rfc_message_id="<m123@example.com>",
    )
    assert ok is True
    assert len(scripts) == 1
    assert "123" in scripts[0]
    assert "<m123@example.com>" in scripts[0]
    assert "move targetMsg to destMailbox" in scripts[0]


def test_web_audit_log_and_one_click_undo_archive(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    bridge = _AuditFakeBridge()
    app, token = create_app(cfg, store, bridge, token="audit-tok")

    with TestClient(app) as client:
        assert client.get("/api/audit-log").status_code == 401
        assert client.post("/api/audit-log/1/undo").status_code == 401

        st0 = client.get("/api/state", headers={"X-Triage-Key": token}).json()
        assert len(st0["items"]) == 2

        # Archive #10 and mark_read #11
        run = client.post(
            "/api/execute/run",
            headers={"X-Triage-Key": token},
            json={
                "mode": "inbox",
                "items": [
                    {"uid": "iCloud:INBOX:10", "action": "archive"},
                    {"uid": "iCloud:INBOX:11", "action": "mark_read"},
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

        assert "10" in bridge.archived_ids

        audit = client.get("/api/audit-log", headers={"X-Triage-Key": token}).json()
        assert len(audit["items"]) == 2
        arch_row = next(r for r in audit["items"] if r["internal_id"] == "10")
        read_row = next(r for r in audit["items"] if r["internal_id"] == "11")
        assert arch_row["can_undo_archive"] is True
        assert arch_row["archive_mailbox"] == "Archive"
        assert read_row["can_undo_archive"] is False

        # Non-archive row rejected with 400
        bad_undo = client.post(
            f"/api/audit-log/{read_row['id']}/undo",
            headers={"X-Triage-Key": token},
        )
        assert bad_undo.status_code == 400

        # Valid one-click undo restores #10 to INBOX and refreshes Inbox view
        undo = client.post(
            f"/api/audit-log/{arch_row['id']}/undo",
            headers={"X-Triage-Key": token},
        )
        assert undo.status_code == 200
        assert undo.json()["restored_to"] == "iCloud/INBOX"
        assert "10" in bridge.inbox_ids
        assert ("iCloud", "Archive", "10", "INBOX") in bridge.restored

        # Second undo is blocked with 400
        repeat_undo = client.post(
            f"/api/audit-log/{arch_row['id']}/undo",
            headers={"X-Triage-Key": token},
        )
        assert repeat_undo.status_code == 400

        # Subsequent /api/state sees #10 back in Inbox
        st1 = client.get("/api/state", headers={"X-Triage-Key": token}).json()
        assert any(i["header"]["internal_id"] == "10" for i in st1["items"])

        # mark-succeeded records rfc_message_id so manual archive + undo also preserves Message-ID
        mark_resp = client.post(
            "/api/unsub/mark-succeeded",
            headers={"X-Triage-Key": token},
            json={"uid": "iCloud:INBOX:11", "archive": True},
        )
        assert mark_resp.status_code == 200
        manual_audit = client.get("/api/audit-log?run_id=manual", headers={"X-Triage-Key": token}).json()
        assert manual_audit["items"][0]["rfc_message_id"] == "<m11@creator.example>"
        assert manual_audit["items"][0]["can_undo_archive"] is True

        html = client.get(f"/?key={token}").text
        app_js = client.get("/static/app.js").text
        assert 'id="audit-log-btn"' in html
        assert "openAuditLogModal" in app_js
        assert "data-audit-undo" in app_js
