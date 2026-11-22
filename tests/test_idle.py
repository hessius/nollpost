"""Tests for auto-idle shutdown (45-minute default, activity tracking, config/UI settings, CLI flags)."""

from __future__ import annotations

import asyncio
import threading
import time
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nollpost import cli
from nollpost.config import TriageConfig, load_config, update_config_file
from nollpost.models import AccountInfo, MailHeader
from nollpost.store import TriageStore
from nollpost.web import AppRuntime, create_app


class _IdleFakeBridge:
    def list_accounts(self) -> list[AccountInfo]:
        return [AccountInfo("iCloud", ["user@example.com"], "INBOX", "Archive", "Sent Messages")]

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
                "1",
                "m1",
                "Test Mail",
                "Sender <news@example.com>",
                "news@example.com",
                "example.com",
                "2026-11-22T10:00:00",
                False,
                False,
                0,
            )
        ]

    def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
        msg = EmailMessage()
        msg["Subject"] = "Test Mail"
        msg["From"] = "Sender <news@example.com>"
        msg.set_content("Body")
        return msg.as_bytes()


def _setup_env(tmp_path: Path, monkeypatch) -> Path:
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NOLLPOST_BROWSER_LOCALE", "en-US")
    monkeypatch.setenv("NOLLPOST_NO_UPDATE_CHECK", "1")
    return cfg_path


def test_default_idle_timeout_is_45_minutes(tmp_path: Path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    assert TriageConfig(tmp_path, tmp_path, tmp_path, tmp_path, tmp_path, tmp_path).idle_timeout_minutes == 45
    cfg = load_config()
    assert cfg.idle_timeout_minutes == 45
    rendered = cfg.config_path.read_text(encoding="utf-8")
    assert "idle_timeout_minutes = 45" in rendered


def test_config_validation_and_settings_api_roundtrip(tmp_path: Path, monkeypatch):
    cfg_path = _setup_env(tmp_path, monkeypatch)
    cfg = load_config()
    store = TriageStore(cfg.db_path)

    update_config_file(cfg_path, {"user": {"idle_timeout_minutes": 0}})
    assert load_config().idle_timeout_minutes == 0

    with pytest.raises(ValueError, match="idle_timeout_minutes"):
        update_config_file(cfg_path, {"user": {"idle_timeout_minutes": -1}})
    with pytest.raises(ValueError, match="idle_timeout_minutes"):
        update_config_file(cfg_path, {"user": {"idle_timeout_minutes": 1441}})

    app, _ = create_app(
        load_config(),
        store,
        _IdleFakeBridge(),
        token="tok",
        preload=False,
        idle_timeout_seconds=0,
    )
    headers = {"X-Triage-Key": "tok"}
    with TestClient(app) as client:
        res = client.get("/api/settings", headers=headers)
        assert res.status_code == 200
        assert res.json()["user"]["idle_timeout_minutes"] == 0

        res_put = client.put(
            "/api/settings",
            headers=headers,
            json={"user": {"idle_timeout_minutes": 30}, "rescan": False},
        )
        assert res_put.status_code == 200
        assert res_put.json()["settings"]["user"]["idle_timeout_minutes"] == 30
        assert load_config().idle_timeout_minutes == 30

        bad_neg = client.put(
            "/api/settings",
            headers=headers,
            json={"user": {"idle_timeout_minutes": -5}, "rescan": False},
        )
        assert bad_neg.status_code == 400

        bad_high = client.put(
            "/api/settings",
            headers=headers,
            json={"user": {"idle_timeout_minutes": 2000}, "rescan": False},
        )
        assert bad_high.status_code == 400
    store.close()


def test_idle_watchdog_triggers_shutdown_after_inactivity(tmp_path: Path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    fired = threading.Event()

    app, _ = create_app(
        cfg,
        store,
        _IdleFakeBridge(),
        token="tok",
        preload=False,
        idle_timeout_seconds=0.15,
        idle_poll_interval=0.03,
        on_idle_shutdown=fired.set,
    )
    with TestClient(app):
        assert fired.wait(timeout=2.0) is True
    store.close()


def test_idle_watchdog_resets_timer_on_authenticated_requests(tmp_path: Path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    fired = threading.Event()

    app, _ = create_app(
        cfg,
        store,
        _IdleFakeBridge(),
        token="tok",
        preload=False,
        idle_timeout_seconds=0.8,
        idle_poll_interval=0.05,
        on_idle_shutdown=fired.set,
    )
    headers = {"X-Triage-Key": "tok"}
    with TestClient(app) as client:
        for _ in range(4):
            time.sleep(0.25)
            assert client.get("/api/updates", headers=headers).status_code == 200
            assert not fired.is_set()
        assert client.get("/api/state", headers=headers).status_code == 200
        assert not fired.is_set()
        assert fired.wait(timeout=3.0) is True
    store.close()


def test_idle_watchdog_respects_zero_timeout(tmp_path: Path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    fired = threading.Event()

    app, _ = create_app(
        cfg,
        store,
        _IdleFakeBridge(),
        token="tok",
        preload=False,
        idle_timeout_seconds=0,
        idle_poll_interval=0.02,
        on_idle_shutdown=fired.set,
    )
    with TestClient(app):
        assert fired.wait(timeout=0.25) is False
    store.close()


def test_runtime_has_active_work_gates_shutdown(tmp_path: Path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    cfg = load_config()
    store = TriageStore(cfg.db_path)

    async def _run():
        rt = AppRuntime(
            config=cfg,
            store=store,
            bridge=_IdleFakeBridge(),
            executor=None,  # type: ignore[arg-type]
            token="tok",
        )
        assert rt.has_active_work() is False

        rt.jobs["j1"] = {"status": "running"}
        assert rt.has_active_work() is True
        rt.jobs["j1"] = {"status": "completed"}
        assert rt.has_active_work() is False

        rt.scan_progress["active"] = True
        assert rt.has_active_work() is True
        rt.scan_progress["active"] = False

        async with rt.scan_lock:
            assert rt.has_active_work() is True
        assert rt.has_active_work() is False

    asyncio.run(_run())
    store.close()


def test_idle_watchdog_does_not_fire_during_running_job(tmp_path: Path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    fired = threading.Event()

    app, _ = create_app(
        cfg,
        store,
        _IdleFakeBridge(),
        token="tok",
        preload=False,
        idle_timeout_seconds=0.12,
        idle_poll_interval=0.02,
        on_idle_shutdown=fired.set,
    )
    with TestClient(app):
        rt: AppRuntime = app.state.runtime
        rt.jobs["job-active"] = {"status": "running"}
        assert fired.wait(timeout=0.25) is False
        rt.jobs["job-active"]["status"] = "completed"
        rt.touch_activity()
        assert fired.wait(timeout=2.0) is True
    store.close()


def test_cli_serve_rejects_negative_idle_timeout():
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--idle-timeout", "-1"])
    assert exc.value.code == 2
