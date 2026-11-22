"""Tests for Homebrew tap update checking, Settings persistence, and Review Board/menu-bar notice."""

import asyncio
import socket
import threading
import time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from nollpost.config import load_config
from nollpost.models import AccountInfo, MailHeader
from nollpost.store import TriageStore
from nollpost.updates import (
    UPGRADE_COMMAND,
    async_check_for_update,
    extract_version_from_formula,
    is_newer_version,
    parse_version_tuple,
)
from nollpost.web import create_app


class _UpdateFakeBridge:
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
                "10",
                "m10",
                "Hello World",
                "Sender <news@shop.example>",
                "news@shop.example",
                "shop.example",
                "2026-11-22T10:00:00",
                False,
                False,
                0,
            )
        ]

    def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
        msg = EmailMessage()
        msg["Subject"] = "Hello World"
        msg["From"] = "Sender <news@shop.example>"
        msg.set_content("Hi")
        return msg.as_bytes()


def _setup_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NOLLPOST_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("NOLLPOST_BROWSER_LOCALE", "en-US")


def test_version_parsing_comparison_and_formula_extraction():
    assert parse_version_tuple("v0.2.10") == (0, 2, 10)
    assert is_newer_version("0.2.0", "0.1.0") is True
    assert is_newer_version("0.1.10", "0.1.2") is True
    assert is_newer_version("0.1.0", "0.1.0") is False
    assert is_newer_version("0.1.0", "0.2.0") is False

    rb = '''
    class Nollpost < Formula
      url "https://github.com/Hessius/nollpost/archive/refs/tags/v0.4.2.tar.gz"
    end
    '''
    assert extract_version_from_formula(rb) == "0.4.2"
    assert extract_version_from_formula('version "1.2.3"') == "1.2.3"
    assert extract_version_from_formula("no version here") is None


@pytest.mark.asyncio
async def test_async_check_for_update_cache_errors_and_opt_out(tmp_path: Path, monkeypatch):
    calls = 0

    async def fake_fetch(_url: str) -> str:
        nonlocal calls
        calls += 1
        return 'url "https://github.com/Hessius/nollpost/archive/refs/tags/v0.5.0.tar.gz"'

    res1 = await async_check_for_update(
        tmp_path,
        current_version="0.1.0",
        fetcher=fake_fetch,
    )
    assert res1["update_available"] is True
    assert res1["latest_version"] == "0.5.0"
    assert res1["upgrade_command"] == UPGRADE_COMMAND
    assert calls == 1

    # Second call within TTL hits disk cache cleanly
    res2 = await async_check_for_update(tmp_path, current_version="0.1.0", fetcher=fake_fetch)
    assert res2["latest_version"] == "0.5.0"
    assert calls == 1

    # force_refresh bypasses cache
    await async_check_for_update(tmp_path, current_version="0.1.0", force_refresh=True, fetcher=fake_fetch)
    assert calls == 2

    # Disabled via flag or env
    disabled = await async_check_for_update(tmp_path, enabled=False, current_version="0.1.0", fetcher=fake_fetch)
    assert disabled["update_available"] is False
    assert disabled["disabled"] is True

    monkeypatch.setenv("NOLLPOST_NO_UPDATE_CHECK", "1")
    env_disabled = await async_check_for_update(tmp_path, current_version="0.1.0", fetcher=fake_fetch)
    assert env_disabled["update_available"] is False
    assert env_disabled["disabled"] is True
    monkeypatch.delenv("NOLLPOST_NO_UPDATE_CHECK", raising=False)

    async def broken_fetch(_url: str) -> str:
        raise RuntimeError("offline")

    err_res = await async_check_for_update(
        tmp_path / "nocache",
        current_version="0.1.0",
        fetcher=broken_fetch,
    )
    assert err_res["update_available"] is False
    assert "offline" in err_res["error"]


@pytest.mark.asyncio
async def test_api_updates_and_webview_banner_in_chromium(tmp_path: Path, monkeypatch):
    from playwright.async_api import async_playwright

    _setup_env(tmp_path, monkeypatch)

    class FormulaHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'url "https://github.com/Hessius/nollpost/archive/refs/tags/v9.9.9.tar.gz"\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    tap_server = ThreadingHTTPServer(("127.0.0.1", 0), FormulaHandler)
    tap_port = tap_server.server_address[1]
    tap_thread = threading.Thread(target=tap_server.serve_forever, daemon=True)
    tap_thread.start()

    monkeypatch.setenv("NOLLPOST_UPDATE_URL", f"http://127.0.0.1:{tap_port}/Formula/nollpost.rb")

    cfg = load_config()
    assert cfg.check_updates is True
    store = TriageStore(cfg.db_path)
    app, token = create_app(cfg, store, _UpdateFakeBridge(), token="upd-tok")

    with TestClient(app) as client:
        assert client.get("/api/updates").status_code == 401
        r = client.get("/api/updates", headers={"X-Triage-Key": token})
        assert r.status_code == 200
        payload = r.json()
        assert payload["update_available"] is True
        assert payload["latest_version"] == "9.9.9"
        assert payload["upgrade_command"] == UPGRADE_COMMAND

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

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(locale="en-US")
            page = await context.new_page()

            await page.goto(f"http://127.0.0.1:{port}/?key={token}", wait_until="domcontentloaded")
            await page.locator("#update-banner").wait_for(state="visible", timeout=8000)
            banner_text = await page.locator("#update-banner").inner_text()
            assert "v9.9.9" in banner_text
            assert UPGRADE_COMMAND in banner_text

            # Switching language to Swedish re-renders the update banner live
            await page.locator("#settings-btn").click()
            await page.locator("#cfg-ui-language").select_option("sv")
            await page.locator("#btn-save-settings").click()
            await page.wait_for_function(
                "() => document.querySelector('#update-banner')?.textContent?.includes('Uppdatering tillgänglig')",
                timeout=8000,
            )

            # Dismissing persists across reload
            await page.locator("#btn-dismiss-update").click()
            await page.locator("#update-banner").wait_for(state="hidden", timeout=4000)
            await page.reload(wait_until="domcontentloaded")
            await page.locator("text=Hello World").wait_for(timeout=8000)
            assert await page.locator("#update-banner").is_hidden()

            await browser.close()

        # Disabling via Settings persists and stops /api/updates
        with TestClient(app) as client:
            put = client.put(
                "/api/settings",
                headers={"X-Triage-Key": token},
                json={"user": {"check_updates": False}},
            )
            assert put.status_code == 200
            assert put.json()["settings"]["user"]["check_updates"] is False
            dis = client.get("/api/updates", headers={"X-Triage-Key": token}).json()
            assert dis["disabled"] is True
            assert dis["update_available"] is False
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        tap_server.shutdown()
        tap_server.server_close()
