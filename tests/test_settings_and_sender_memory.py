"""Tests for Issue #9: in-browser settings editor and sender_memory rule manager."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nollpost.config import load_config, update_config_file
from nollpost.mail_bridge import validate_archive_destination
from nollpost.models import AccountInfo, MailHeader
from nollpost.store import TriageStore
from nollpost.web import create_app


class _SettingsFakeBridge:
    def __init__(self, archive_overrides=None, gmail_archive_strategy="ui_menu"):
        self.archive_overrides = dict(archive_overrides or {})
        self.gmail_archive_strategy = gmail_archive_strategy

    def list_accounts(self) -> list[AccountInfo]:
        return [
            AccountInfo(
                "iCloud",
                ["user@example.com"],
                "INBOX",
                self.archive_overrides.get("iCloud", "Archive"),
                "Sent Messages",
            ),
        ]

    def scan_mailbox_metadata(
        self,
        account: str,
        mailbox: str = "INBOX",
        limit: int = 500,
        include_attachments: bool = True,
        days_back: int = 0,
    ):
        return [
            MailHeader(
                "iCloud",
                "INBOX",
                "100",
                "m100",
                "Project Dispatch",
                "Dispatch <updates@dispatch.example>",
                "updates@dispatch.example",
                "dispatch.example",
                "2026-11-22T09:00:00",
                False,
                False,
                0,
            ),
            MailHeader(
                "iCloud",
                "INBOX",
                "101",
                "m101",
                "Consultation Request on MedTech",
                "ExpertNet <invite@expertnet.example>",
                "invite@expertnet.example",
                "expertnet.example",
                "2026-11-22T09:05:00",
                False,
                False,
                0,
            ),
        ]

    def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
        msg = EmailMessage()
        if internal_id == "100":
            msg["Subject"] = "Project Dispatch"
            msg["From"] = "Dispatch <updates@dispatch.example>"
            msg.set_content("New update ready")
            msg.add_alternative("<a href='https://dispatch.example/post/1'>Read update</a>", subtype="html")
        else:
            msg["Subject"] = "Consultation Request on MedTech"
            msg["From"] = "ExpertNet <invite@expertnet.example>"
            msg.set_content("Are you available for a paid medical devices consultation?")
            msg.add_alternative(
                "<a href='https://expertnet.example/accept'>Accept</a> "
                "<a href='https://expertnet.example/decline'>Not a fit</a>",
                subtype="html",
            )
        return msg.as_bytes()


def test_update_config_file_validation_and_persistence(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))

    updated = update_config_file(
        cfg_path,
        {
            "user": {"max_inbox_messages": 250, "mark_read_on_archive": False},
            "accounts": {"gmail_archive_strategy": "tag_staging", "archive_map": {"Google": "[Gmail]/All e-post"}},
            "rules": {"enabled_packs": ["se"]},
            "browser": {"locale": "sv-SE"},
            "consulting": {"enabled": True, "networks": ["ExpertNet"], "match_keywords": ["medtech"]},
            "profile": {"wanted_domains": ["Dispatch.Example"], "bill_keywords": ["invoice", "receipt"]},
        },
        archive_validator=validate_archive_destination,
    )
    assert updated.max_inbox_messages == 250
    assert updated.mark_read_on_archive is False
    assert updated.gmail_archive_strategy == "tag_staging"
    assert updated.archive_map == {"Google": "[Gmail]/All e-post"}
    assert updated.enabled_packs == ["se"]
    assert updated.browser_locale == "sv-SE"
    assert updated.consulting_enabled is True
    assert updated.wanted_domains == ["dispatch.example"]

    # Reject Trash, out-of-range integer, bad strategy, and bad locale
    with pytest.raises(ValueError, match="prohibited"):
        update_config_file(
            cfg_path,
            {"accounts": {"archive_map": {"iCloud": "Trash"}}},
            archive_validator=validate_archive_destination,
        )
    with pytest.raises(ValueError):
        update_config_file(cfg_path, {"user": {"max_inbox_messages": 99999}})
    with pytest.raises(ValueError):
        update_config_file(cfg_path, {"accounts": {"gmail_archive_strategy": "bad_mode"}})
    with pytest.raises(ValueError):
        update_config_file(cfg_path, {"browser": {"locale": "not_a_valid_locale_string"}})


def test_store_sender_memory_list_filter_and_delete(tmp_path: Path):
    store = TriageStore(tmp_path / "state.db")
    store.record_sender_preference("keep@creator.example", "Creator Co", "always_keep", True)
    store.record_sender_preference("arch@noise.example", "Noise Letter", "archive", True)
    store.record_unsubscribed("unsub@promo.example", "Promo Blast")

    assert len(store.list_sender_memory()) == 3
    assert [r["sender_key"] for r in store.list_sender_memory(filter_kind="always_keep")] == ["keep@creator.example"]
    assert [r["sender_key"] for r in store.list_sender_memory(filter_kind="unsubscribed")] == ["unsub@promo.example"]
    assert [r["sender_key"] for r in store.list_sender_memory(search="Noise")] == ["arch@noise.example"]

    deleted = store.delete_sender_memory(["arch@noise.example"])
    assert deleted == 1
    assert len(store.list_sender_memory()) == 2


def test_web_settings_live_reload_and_sender_memory_bulk_api(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    bridge = _SettingsFakeBridge()
    app, token = create_app(cfg, store, bridge, token="settings-tok")

    with TestClient(app) as client:
        # Auth required
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/sender-memory").status_code == 401

        # Initial scan: wanted_domains=[], consulting disabled
        s0 = client.get("/api/state", headers={"X-Triage-Key": token}).json()
        by_id0 = {i["header"]["internal_id"]: i for i in s0["items"]}
        assert by_id0["100"]["category"] != "updates"
        assert s0["consulting_enabled"] is False

        # Forbidden Trash target rejected with 400
        bad_put = client.put(
            "/api/settings",
            headers={"X-Triage-Key": token},
            json={"accounts": {"archive_map": {"iCloud": "Deleted Messages"}}},
        )
        assert bad_put.status_code == 400

        # Valid update enables consulting + adds dispatch.example to wanted_domains live
        ok_put = client.put(
            "/api/settings",
            headers={"X-Triage-Key": token},
            json={
                "user": {"max_inbox_messages": 120},
                "consulting": {
                    "enabled": True,
                    "networks": ["expertnet.example"],
                    "match_keywords": ["medtech"],
                },
                "profile": {"wanted_domains": ["dispatch.example"]},
            },
        )
        assert ok_put.status_code == 200
        assert ok_put.json()["settings"]["user"]["max_inbox_messages"] == 120
        assert load_config(ensure_files=False).consulting_enabled is True

        # Next /api/state reflects updated config immediately without server restart
        s1 = client.get("/api/state", headers={"X-Triage-Key": token}).json()
        assert s1["consulting_enabled"] is True
        by_id1 = {i["header"]["internal_id"]: i for i in s1["items"]}
        assert by_id1["100"]["category"] == "updates"
        assert by_id1["101"]["category"] == "consulting"

        # Seed sender_memory and test search/filter/bulk edit/delete
        store.record_sender_preference("updates@dispatch.example", "Dispatch", "archive", True)
        store.record_sender_preference("promo@other.example", "Promo", "mark_read", True)

        mem_resp = client.get("/api/sender-memory?search=dispatch", headers={"X-Triage-Key": token})
        assert mem_resp.status_code == 200
        assert len(mem_resp.json()["items"]) == 1

        bulk_upd = client.post(
            "/api/sender-memory/bulk",
            headers={"X-Triage-Key": token},
            json={"sender_keys": ["updates@dispatch.example"], "action": "always_keep"},
        )
        assert bulk_upd.status_code == 200
        assert store.get_sender_memory("updates@dispatch.example").preferred_action == "always_keep"

        bulk_del = client.post(
            "/api/sender-memory/bulk",
            headers={"X-Triage-Key": token},
            json={"sender_keys": ["promo@other.example"], "action": "delete"},
        )
        assert bulk_del.status_code == 200
        assert store.get_sender_memory("promo@other.example") is None

        html = client.get(f"/?key={token}").text
        app_js = client.get("/static/app.js").text
        assert 'id="settings-btn"' in html
        assert 'id="sender-rules-btn"' in html
        assert "openSettingsModal" in app_js
        assert "openSenderRulesModal" in app_js

        # Non-list payload for list fields must return HTTP 400, not 500
        bad_list = client.put(
            "/api/settings",
            headers={"X-Triage-Key": token},
            json={"rules": {"enabled_packs": "se"}},
        )
        assert bad_list.status_code == 400


def test_legacy_consulting_preserved_and_chunked_sender_delete(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[profile]\nconsulting_networks = ["expertnet.example"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))

    # Updating unrelated [user] setting must keep legacy consulting_enabled=True
    reloaded = update_config_file(cfg_path, {"user": {"max_inbox_messages": 300}})
    assert reloaded.consulting_enabled is True
    assert reloaded.consulting_networks == ["expertnet.example"]

    # Non-table TOML section raises ValueError cleanly
    bad_path = tmp_path / "bad.toml"
    bad_path.write_text('user = "oops"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="must be a table"):
        load_config(ensure_files=False, config_path=bad_path)

    # Chunked delete handles >1000 keys without SQLite variable-limit error
    store = TriageStore(tmp_path / "chunk.db")
    keys = [f"s{i}@bulk.example" for i in range(1100)]
    for k in keys[:5]:
        store.record_sender_preference(k, "", "archive", True)
    deleted = store.delete_sender_memory(keys)
    assert deleted == 5


def test_automation_config_defaults_and_roundtrip(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[user]\ndisplay_name = \"Ada\"\n", encoding="utf-8")
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))

    cfg = load_config(ensure_files=False, config_path=cfg_path)
    assert cfg.auto_enabled is True
    assert cfg.auto_interval_minutes == 60
    assert cfg.auto_archive_sender_rules is True
    assert cfg.auto_decline_consulting is False
    assert cfg.auto_archive_transactional is False
    assert cfg.auto_archive_bot_noise is False
    assert cfg.auto_archive_always_archive is False
    assert cfg.auto_archive_recent_unsubs is False
    assert cfg.auto_skip_security_alerts is True
    assert cfg.auto_skip_pdf_invoices is True

    updated = update_config_file(
        cfg_path,
        {
            "automation": {
                "enabled": False,
                "interval_minutes": 30,
                "auto_decline_consulting": True,
                "auto_archive_transactional": True,
                "auto_archive_bot_noise": True,
                "auto_archive_always_archive": True,
                "auto_archive_recent_unsubs": True,
                "skip_security_alerts": True,
                "skip_pdf_invoices": False,
            }
        },
    )
    assert updated.auto_enabled is False
    assert updated.auto_interval_minutes == 30
    assert updated.auto_decline_consulting is True
    assert updated.auto_archive_transactional is True
    assert updated.auto_archive_bot_noise is True
    assert updated.auto_archive_always_archive is True
    assert updated.auto_archive_recent_unsubs is True
    assert updated.auto_skip_pdf_invoices is False

    reloaded = load_config(ensure_files=False, config_path=cfg_path)
    assert reloaded.auto_interval_minutes == 30
    assert reloaded.auto_decline_consulting is True
    assert reloaded.auto_skip_pdf_invoices is False


def test_automation_config_rejects_invalid_interval(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))

    with pytest.raises(ValueError, match="interval_minutes"):
        update_config_file(cfg_path, {"automation": {"interval_minutes": 0}})
    with pytest.raises(ValueError, match="interval_minutes"):
        update_config_file(cfg_path, {"automation": {"interval_minutes": True}})
    with pytest.raises(ValueError, match="interval_minutes"):
        update_config_file(cfg_path, {"automation": {"interval_minutes": 2000}})
    bad_toml = tmp_path / "bad_auto.toml"
    bad_toml.write_text("[automation]\ninterval_minutes = 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="interval_minutes"):
        load_config(ensure_files=False, config_path=bad_toml)
