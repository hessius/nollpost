"""Tests for UI localization (i18n) bundles, config persistence, API resolution, and browser rendering."""

import asyncio
import json
import re
import socket
import threading
import time
from email.message import EmailMessage
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from nollpost.config import load_config
from nollpost.models import AccountInfo, MailHeader
from nollpost.store import TriageStore
from nollpost.web import LOCALES_DIR, STATIC_DIR, create_app, load_ui_locale_messages, resolve_ui_language


class _I18nFakeBridge:
    def list_accounts(self) -> list[AccountInfo]:
        return [
            AccountInfo("iCloud", ["user@example.com"], "INBOX", "Archive", "Sent Messages"),
            AccountInfo("Google", ["alt@example.org"], "INBOX", "[Gmail]/All Mail", "[Gmail]/Sent Mail"),
        ]

    def scan_mailbox_metadata(
        self,
        account: str,
        mailbox: str = "INBOX",
        limit: int = 500,
        include_attachments: bool = True,
        days_back: int = 0,
    ):
        if account == "iCloud":
            return [
                MailHeader(
                    "iCloud",
                    "INBOX",
                    "10",
                    "m10",
                    "Weekly Dispatch",
                    "News <news@shop.example>",
                    "news@shop.example",
                    "shop.example",
                    "2026-11-22T09:00:00",
                    False,
                    False,
                    0,
                )
            ]
        return [
            MailHeader(
                "Google",
                "INBOX",
                "20",
                "m20",
                "Build Passed",
                "GitHub <notifications@github.com>",
                "notifications@github.com",
                "github.com",
                "2026-11-22T09:05:00",
                False,
                False,
                0,
            )
        ]

    def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
        msg = EmailMessage()
        if internal_id == "10":
            msg["Subject"] = "Weekly Dispatch"
            msg["From"] = "News <news@shop.example>"
            msg["List-Unsubscribe"] = "<https://shop.example/unsub>"
            msg.set_content("Unsubscribe here")
            msg.add_alternative("<a href='https://shop.example/unsub'>Unsubscribe</a>", subtype="html")
        else:
            msg["Subject"] = "Build Passed"
            msg["From"] = "GitHub <notifications@github.com>"
            msg.set_content("Workflow green")
        return msg.as_bytes()


def _setup_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NOLLPOST_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NOLLPOST_BROWSER_LOCALE", "en-US")


def _extract_placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{([a-zA-Z0-9_]+)\}", text))


def test_locale_bundles_complete_clean_and_referenced_keys_exist():
    en_raw = json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8"))
    sv_raw = json.loads((LOCALES_DIR / "sv.json").read_text(encoding="utf-8"))

    assert en_raw["_meta"] == {"code": "en", "name": "English"}
    assert sv_raw["_meta"] == {"code": "sv", "name": "Svenska"}

    en_keys = {k for k in en_raw if k != "_meta"}
    sv_keys = {k for k in sv_raw if k != "_meta"}
    assert en_keys == sv_keys

    for key in en_keys:
        assert isinstance(en_raw[key], str) and en_raw[key].strip()
        assert isinstance(sv_raw[key], str) and sv_raw[key].strip()
        assert "\u2014" not in en_raw[key]
        assert "\u2014" not in sv_raw[key]
        assert _extract_placeholders(en_raw[key]) == _extract_placeholders(sv_raw[key]), (
            f"Placeholder mismatch for key {key!r}"
        )

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    html_keys = set(re.findall(r'data-i18n(?:-(?:title|placeholder))?="([^"]+)"', html))
    js_keys = set(re.findall(r'\bt(?:Html)?\(\s*"([^"]+)"', app_js))
    for key in html_keys | js_keys:
        assert key in en_keys, f"UI references missing i18n key {key!r}"

    for cat in ("all", "attention", "updates", "consulting", "newsletters", "missed"):
        assert f"cat.{cat}" in en_keys
    for act in (
        "no_action",
        "mark_read",
        "read_open_link",
        "read_show_invoice",
        "decline_and_archive",
        "draft_decline",
        "send_decline_and_archive",
        "archive",
        "archive_and_unsub",
        "unsub_only",
        "sweep_archive",
        "undo_archive",
        "mark_unsubscribed",
    ):
        assert f"action.{act}" in en_keys


def test_i18n_api_resolution_config_and_fallback(tmp_path: Path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    cfg = load_config()
    assert cfg.ui_language == ""

    store = TriageStore(cfg.db_path)
    app, token = create_app(cfg, store, _I18nFakeBridge(), token="i18n-tok")

    with TestClient(app) as client:
        assert client.get("/api/i18n").status_code == 401

        auto_sv = client.get(
            "/api/i18n",
            headers={"X-Triage-Key": token, "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"},
        )
        assert auto_sv.status_code == 200
        sv_data = auto_sv.json()
        assert sv_data["language"] == "sv"
        assert sv_data["messages"]["mode.inbox"] == "Daglig Inkorg"
        assert sv_data["messages"]["action.archive_and_unsub"] == "Arkivera & avreg."

        fallback_en = client.get(
            "/api/i18n",
            headers={"X-Triage-Key": token, "Accept-Language": "fr-FR,de;q=0.8"},
        )
        assert fallback_en.status_code == 200
        assert fallback_en.json()["language"] == "en"
        assert fallback_en.json()["messages"]["mode.inbox"] == "Daily Inbox"

        bad = client.put(
            "/api/settings",
            headers={"X-Triage-Key": token},
            json={"user": {"ui_language": "invalid locale!"}},
        )
        assert bad.status_code == 400

        saved = client.put(
            "/api/settings",
            headers={"X-Triage-Key": token},
            json={"user": {"ui_language": "sv"}},
        )
        assert saved.status_code == 200
        settings_user = saved.json()["settings"]["user"]
        assert settings_user["ui_language"] == "sv"
        assert [loc["code"] for loc in settings_user["available_ui_languages"]] == ["en", "sv"]

        # Config override wins over Accept-Language: en-US
        forced = client.get(
            "/api/i18n",
            headers={"X-Triage-Key": token, "Accept-Language": "en-US,en;q=0.9"},
        )
        assert forced.status_code == 200
        assert forced.json()["language"] == "sv"
        assert "ui_language = \"sv\"" in (tmp_path / "config.toml").read_text(encoding="utf-8")

    # Partial locale falls back cleanly to English on missing keys
    partial_dir = tmp_path / "locales"
    partial_dir.mkdir()
    (partial_dir / "en.json").write_text(
        json.dumps({"_meta": {"code": "en", "name": "English"}, "a": "One", "b": "Two"}),
        encoding="utf-8",
    )
    (partial_dir / "nb.json").write_text(
        json.dumps({"_meta": {"code": "nb", "name": "Norsk"}, "a": "En"}),
        encoding="utf-8",
    )
    assert resolve_ui_language("fr", "fr-FR", {"en", "nb"}) == "en"
    merged = load_ui_locale_messages("nb", locales_dir=partial_dir)
    assert merged == {"a": "En", "b": "Two"}


@pytest.mark.asyncio
async def test_ui_renders_english_and_live_switches_to_swedish_in_chromium(tmp_path: Path, monkeypatch):
    from playwright.async_api import async_playwright

    _setup_env(tmp_path, monkeypatch)
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    app, token = create_app(cfg, store, _I18nFakeBridge(), token="pw-i18n-tok")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

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
            await page.locator("text=Weekly Dispatch").wait_for(timeout=8000)

            assert await page.evaluate("document.documentElement.lang") == "en"
            assert (await page.locator("#mode-inbox").inner_text()).strip() == "Daily Inbox"
            assert "All Accounts (2)" in (await page.locator("#account-tabs").inner_text())

            # Switch UI language to Swedish via Settings modal and verify live reload without refresh
            await page.locator("#settings-btn").click()
            await page.locator("#cfg-ui-language").select_option("sv")
            await page.locator("#btn-save-settings").click()

            await page.wait_for_function(
                "() => document.documentElement.lang === 'sv' && document.querySelector('#mode-inbox')?.textContent?.trim() === 'Daglig Inkorg'",
                timeout=8000,
            )
            assert "Alla konton (2)" in (await page.locator("#account-tabs").inner_text())
            assert "Arkivera & avreg." in (await page.locator("#queue-container").inner_text())
            assert page_errors == []
            await browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
