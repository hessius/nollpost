"""Tests for nollpost `doctor` preflight checks and `setup` onboarding wizard."""

import json
from pathlib import Path

from nollpost import cli
from nollpost.config import load_config
from nollpost.models import AccountInfo
from nollpost.onboarding import format_doctor_report, run_doctor, run_setup


class _StubBridge:
    def __init__(self, accounts=None, error=None):
        self._accounts = accounts if accounts is not None else []
        self._error = error

    def list_accounts(self) -> list[AccountInfo]:
        if self._error is not None:
            raise self._error
        return list(self._accounts)


def _setup_env(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    return cfg_path


def test_doctor_passes_and_detects_missing_automation_and_chromium(tmp_path: Path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    cfg = load_config(ensure_files=False)

    bridge_ok = _StubBridge(
        [
            AccountInfo("iCloud", ["a@example.com"], "INBOX", "Archive"),
            AccountInfo("Google", ["b@example.com"], "INBOX", "[Gmail]/All Mail"),
        ]
    )
    ok, checks = run_doctor(
        cfg,
        bridge_ok,
        platform_name="Darwin",
        chromium_checker=lambda: (True, "/tmp/chromium"),
        accessibility_checker=lambda: (True, "ok"),
        mail_running_checker=lambda: True,
    )
    assert ok is True
    assert all(c.status == "ok" for c in checks)
    assert "Nollpost Preflight Check" in format_doctor_report(checks)

    # Automation -1743 + missing Chromium -> fail
    bridge_denied = _StubBridge(error=RuntimeError("Not authorized to send Apple events to Mail (-1743)"))
    ok_fail, checks_fail = run_doctor(
        cfg,
        bridge_denied,
        platform_name="Darwin",
        chromium_checker=lambda: (False, "missing"),
        accessibility_checker=lambda: (False, "disabled"),
        mail_running_checker=lambda: False,
    )
    assert ok_fail is False
    by_name = {c.name: c for c in checks_fail}
    assert by_name["mail_automation"].status == "fail"
    assert "Automation" in by_name["mail_automation"].remediation
    assert by_name["chromium"].status == "fail"
    assert by_name["mail_app"].status == "warn"

    # Accessibility disabled with Gmail ui_menu -> warn (not fail)
    ok_ax, checks_ax = run_doctor(
        cfg,
        bridge_ok,
        platform_name="Darwin",
        chromium_checker=lambda: (True, "/tmp/chromium"),
        accessibility_checker=lambda: (False, "disabled"),
        mail_running_checker=lambda: True,
    )
    assert ok_ax is True
    assert {c.name: c for c in checks_ax}["accessibility"].status == "warn"


def test_setup_wizard_rejects_trash_and_writes_valid_config(tmp_path: Path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    cfg = load_config(ensure_files=False)
    bridge = _StubBridge(
        [
            AccountInfo("iCloud", ["a@example.com"], "INBOX", "Archive"),
            AccountInfo("Google", ["b@example.com"], "INBOX", "[Gmail]/All e-post"),
        ]
    )

    answers = iter(
        [
            "y",                 # install chromium
            "",                  # accept iCloud -> Archive
            "Trash",             # rejected!
            "[Gmail]/All e-post",# valid override
            "3",                 # choose tag_staging
            "y",                 # enable "se" pack
            "n",                 # keep consulting disabled
            "kickstarter.com, patreon.com",
        ]
    )
    out_lines: list[str] = []
    installed_calls: list[bool] = []

    def fake_install() -> int:
        installed_calls.append(True)
        return 0

    rc = run_setup(
        cfg,
        bridge,
        input_fn=lambda _p: next(answers),
        output_fn=out_lines.append,
        chromium_checker=lambda: (False, "not installed"),
        browser_installer=fake_install,
    )
    assert rc == 0
    assert installed_calls == [True]
    combined_out = "\n".join(out_lines)
    assert "prohibited" in combined_out
    assert "archiveLabeledEmails" in combined_out

    reloaded = load_config(ensure_files=False)
    assert reloaded.gmail_archive_strategy == "tag_staging"
    assert reloaded.enabled_packs == ["se"]
    assert reloaded.consulting_enabled is False
    assert reloaded.wanted_domains == ["kickstarter.com", "patreon.com"]
    assert reloaded.archive_map["Google"] == "[Gmail]/All e-post"


def test_cli_doctor_json_exit_code(tmp_path: Path, monkeypatch, capsys):
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "AppleMailBridge", lambda **_kw: _StubBridge(error=RuntimeError("denied (-1743)")))
    monkeypatch.setattr("nollpost.onboarding._check_chromium_installed", lambda: (False, "missing"))
    rc = cli.main(["doctor", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any(c["status"] == "fail" for c in payload["checks"])
