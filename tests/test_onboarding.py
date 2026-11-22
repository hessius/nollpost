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
    cfg_path = _setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "AppleMailBridge", lambda **_kw: _StubBridge(error=RuntimeError("denied (-1743)")))
    monkeypatch.setattr("nollpost.onboarding._check_chromium_installed", lambda: (False, "missing"))
    rc = cli.main(["doctor", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any(c["status"] == "fail" for c in payload["checks"])
    # doctor must not create config.toml or state.db
    assert not cfg_path.exists()
    assert not (tmp_path / "data" / "state.db").exists()

    # Invalid config.toml must report a structured 'config' fail check rather than crashing
    cfg_path.write_text("[accounts]\ngmail_archive_strategy = 'bad'\n", encoding="utf-8")
    rc_bad = cli.main(["doctor", "--json"])
    assert rc_bad == 1
    bad_payload = json.loads(capsys.readouterr().out)
    assert any(c["name"] == "config" and c["status"] == "fail" for c in bad_payload["checks"])


def test_setup_sanitizes_trash_archive_map_migrates_legacy_consulting_and_handles_eof(tmp_path: Path, monkeypatch):
    import tomllib
    from nollpost.onboarding import _toml_str

    escaped = _toml_str('line1\nline2\t"q"\x00\x1b\\')
    assert tomllib.loads(f"v = {escaped}")["v"] == 'line1\nline2\t"q"\x00\x1b\\'

    cfg_path = _setup_env(tmp_path, monkeypatch)
    cfg_path.write_text(
        """
[user]
consulting_decline_boilerplate = "Custom legacy boilerplate"

[accounts.archive_map]
Google = "Trash"
Stale = "Deleted Messages"

[profile]
consulting_networks = ["guidepointglobal.com"]
consulting_match_keywords = ["medtech"]
""",
        encoding="utf-8",
    )
    cfg = load_config(ensure_files=False)
    bridge = _StubBridge([AccountInfo("Google", ["b@example.com"], "INBOX", "[Gmail]/All Mail")])

    # EOF mid-prompt must abort cleanly without overwriting config
    rc_eof = run_setup(
        cfg,
        bridge,
        skip_browser=True,
        input_fn=lambda _p: (_ for _ in ()).throw(EOFError()),
        output_fn=lambda _s: None,
    )
    assert rc_eof == 1
    assert "Trash" in cfg_path.read_text(encoding="utf-8")

    # Non-interactive --yes strips forbidden archive_map entries and migrates legacy consulting keys
    out: list[str] = []
    rc_yes = run_setup(
        cfg,
        bridge,
        yes=True,
        skip_browser=True,
        output_fn=out.append,
    )
    assert rc_yes == 0
    reloaded = load_config(ensure_files=False)
    assert reloaded.consulting_enabled is True
    assert reloaded.consulting_networks == ["guidepointglobal.com"]
    assert reloaded.consulting_decline_boilerplate == "Custom legacy boilerplate"
    assert reloaded.archive_map.get("Google") == "[Gmail]/All Mail"
    assert "Stale" not in reloaded.archive_map
    assert "Trash" not in cfg_path.read_text(encoding="utf-8")
