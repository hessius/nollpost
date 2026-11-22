"""Integration tests for RFC 8058 POST and real Playwright Chromium against local HTTP fixtures."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nollpost.playwright_runner import is_public_http_url, run_playwright_action, try_rfc8058_one_click


class _FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(length) if length else b""
        if self.path == "/oneclick-ok":
            content_type = self.headers.get("Content-Type", "")
            if "application/x-www-form-urlencoded" not in content_type or raw_body != b"List-Unsubscribe=One-Click":
                self.send_response(400)
                self.end_headers()
                return
            body = b"Unsubscribed"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/oneclick-form":
            body = b"<form><button type='submit'>Confirm Unsubscribe</button></form>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if self.path == "/sv-unsub":
            self.wfile.write(
                """<html><body>
                <h2>Hantera dina utskick</h2>
                <form action="/sv-done" method="get">
                  <label><input type="checkbox" checked /> Veckobrev</label>
                  <button type="submit">Avprenumerera från allt</button>
                </form>
                </body></html>""".encode("utf-8")
            )
        elif self.path.startswith("/sv-done"):
            self.wfile.write("<html><body><p>Du är nu avregistrerad från alla utskick.</p></body></html>".encode("utf-8"))
        elif self.path == "/consulting-decline":
            self.wfile.write(
                """<html><body>
                <form action="/consulting-done" method="get">
                  <label><input type="radio" name="reason" value="not_area" /> Not my area of expertise</label>
                  <button type="submit">Decline</button>
                </form>
                </body></html>""".encode("utf-8")
            )
        elif self.path.startswith("/consulting-done"):
            self.wfile.write("<html><body><p>Thank you for your response. Project declined.</p></body></html>".encode("utf-8"))
        elif self.path == "/captcha":
            self.wfile.write(
                """<html><body>
                <div class="cf-turnstile">Verify you are human</div>
                <button>Unsubscribe</button>
                </body></html>""".encode("utf-8")
            )
        elif self.path == "/sats-pref":
            import base64
            inner = """<!doctype html><html><body>
            <h2>Här kan du välja vilka typer av e-postmeddelanden du vill ta emot</h2>
            <label><input type="checkbox" checked /> Nyheter</label>
            <label><input type="checkbox" checked /> Månadsstatistik</label>
            <button type="button" onclick="document.body.innerHTML='<p>Still subscribed to partial lists</p>'">Spara val</button>
            <button type="button" onclick="document.body.innerHTML='<p>Dina preferenser har uppdaterats.</p>'">Avregistrera alla e-postutskick</button>
            </body></html>"""
            b64 = base64.b64encode(inner.encode("utf-8")).decode("ascii")
            self.wfile.write(
                f'<html><body><iframe sandbox="allow-forms allow-scripts" src="data:text/html;base64,{b64}"></iframe></body></html>'.encode(
                    "utf-8"
                )
            )
        elif self.path.startswith("/wrong-save"):
            self.wfile.write("<html><body><p>Still subscribed to partial lists</p></body></html>".encode("utf-8"))
        elif self.path == "/weeffect-sub":
            self.wfile.write(
                """<html><body>
                <a href="#unsub-all">Avbryt alla prenumerationer</a>
                <form action="/wrong-save" method="get">
                  <input type="checkbox" name="pub1" /> 1%-kampanj
                  <input type="submit" value="Uppdatera" />
                </form>
                <form id="unsub-all" action="/weeffect-done" method="get">
                  <table><tr>
                    <td><input type="checkbox" name="optout" required /></td>
                    <td>Jag vill inte längre ta emot några publikationer.</td>
                  </tr></table>
                  <input type="submit" value="Uppdatera" />
                </form>
                </body></html>""".encode("utf-8")
            )
        elif self.path.startswith("/weeffect-done"):
            self.wfile.write(
                "<html><body><div>Din prenumeration har uppdaterats.</div></body></html>".encode("utf-8")
            )
        elif self.path.startswith("/viking-done"):
            self.wfile.write(
                "<html><body><h2>Vi beklagar att du avslutar prenumerationen.</h2><p>Din avregistrering är genomförd.</p></body></html>".encode("utf-8")
            )
        elif self.path == "/wolt-done":
            self.wfile.write(
                "<html><body><p>You've successfully been unsubscribed from Email Marketing Channel messages for Customer.</p><button>+ RESUBSCRIBE</button></body></html>".encode("utf-8")
            )
        else:
            self.wfile.write("<html><body><p>Hello world</p></body></html>".encode("utf-8"))


@pytest.fixture(scope="module")
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.mark.asyncio
async def test_rfc8058_ok_and_form_escalation(fixture_server: str):
    ok = await try_rfc8058_one_click(f"{fixture_server}/oneclick-ok", allow_private=True)
    assert ok.status == "success"

    esc = await try_rfc8058_one_click(f"{fixture_server}/oneclick-form", allow_private=True)
    assert esc.status == "failed"


@pytest.mark.asyncio
async def test_playwright_swedish_unsub_and_consulting_and_captcha_and_fail(fixture_server: str, tmp_path: Path):
    sv_res = await run_playwright_action(
        f"{fixture_server}/sv-unsub",
        mode="unsubscribe",
        screenshots_dir=tmp_path,
        headless=True,
        allow_private=True,
    )
    assert sv_res.status == "success"
    assert sv_res.screenshot_path and Path(sv_res.screenshot_path).exists()

    dec_res = await run_playwright_action(
        f"{fixture_server}/consulting-decline",
        mode="consulting_decline",
        screenshots_dir=tmp_path,
        headless=True,
        allow_private=True,
    )
    assert dec_res.status == "success"

    cap_res = await run_playwright_action(
        f"{fixture_server}/captcha",
        mode="unsubscribe",
        screenshots_dir=tmp_path,
        headless=True,
        allow_private=True,
    )
    assert cap_res.status == "blocked_captcha"

    fail_res = await run_playwright_action(
        f"{fixture_server}/unknown",
        mode="unsubscribe",
        screenshots_dir=tmp_path,
        headless=True,
        allow_private=True,
    )
    assert fail_res.status == "failed"

    sats_res = await run_playwright_action(
        f"{fixture_server}/sats-pref",
        mode="unsubscribe",
        screenshots_dir=tmp_path,
        headless=True,
        allow_private=True,
    )
    assert sats_res.status == "success"
    assert "Avregistrera alla e-postutskick" in sats_res.detail

    we_res = await run_playwright_action(
        f"{fixture_server}/weeffect-sub",
        mode="unsubscribe",
        screenshots_dir=tmp_path,
        headless=True,
        allow_private=True,
    )
    assert we_res.status == "success"
    assert "Uppdatera" in we_res.detail

    wolt_res = await run_playwright_action(
        f"{fixture_server}/wolt-done",
        mode="unsubscribe",
        screenshots_dir=tmp_path,
        headless=True,
        allow_private=True,
    )
    assert wolt_res.status == "success"


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_private_and_non_http_and_failed_goto_preserves_url(fixture_server: str, tmp_path: Path):
    assert is_public_http_url(f"{fixture_server}/oneclick-ok", allow_private=False) is False
    assert is_public_http_url("http://169.254.169.254/latest/meta-data/", allow_private=False) is False
    assert is_public_http_url("http://192.168.1.1/unsub", allow_private=False) is False
    assert is_public_http_url("http://100.64.0.1/unsub", allow_private=False) is False
    assert is_public_http_url("http://198.18.0.1/unsub", allow_private=False) is False
    assert is_public_http_url("http://0177.0.0.1/unsub", allow_private=False) is False
    assert is_public_http_url("http://0x7f000001/unsub", allow_private=False) is False
    assert is_public_http_url("http://2130706433/unsub", allow_private=False) is False
    assert is_public_http_url("http://[::ffff:127.0.0.1]/unsub", allow_private=False) is False
    assert is_public_http_url("mailto:unsub@example.com", allow_private=False) is False
    assert is_public_http_url("javascript:alert(1)", allow_private=False) is False
    assert is_public_http_url("https://93.184.216.34/unsub", allow_private=False) is True

    blocked_post = await try_rfc8058_one_click(f"{fixture_server}/oneclick-ok", allow_private=False)
    assert blocked_post.status == "failed"
    assert "Blocked" in blocked_post.detail

    blocked_pw = await run_playwright_action(
        f"{fixture_server}/sv-unsub",
        mode="unsubscribe",
        screenshots_dir=tmp_path,
        headless=True,
        allow_private=False,
    )
    assert blocked_pw.status == "failed"
    assert "Blocked" in blocked_pw.detail

    # Reserve then immediately close a port so Playwright hits ECONNREFUSED and never navigates off about:blank
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        unused_port = sock.getsockname()[1]
    unreachable_url = f"http://127.0.0.1:{unused_port}/unsub"
    goto_fail = await run_playwright_action(
        unreachable_url,
        mode="unsubscribe",
        screenshots_dir=tmp_path,
        headless=True,
        allow_private=True,
    )
    assert goto_fail.status == "failed"
    assert goto_fail.final_url != "about:blank"
    assert goto_fail.final_url == unreachable_url
