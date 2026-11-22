"""Tests for privacy-redacted Missed-Unsubscribe export (unit, CLI, and Web API)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from nollpost.cli import main as cli_main
from nollpost.config import load_config
from nollpost.missed_export import format_missed_bug_report, redact_missed_entry, redact_url
from nollpost.store import TriageStore
from nollpost.web import create_app


def _seed_realistic_missed(store: TriageStore) -> int:
    return store.enqueue_missed_unsubscribe(
        account="iCloud",
        mailbox="INBOX",
        internal_id="9042",
        rfc_message_id="<msg-secret-id@mail.acme.example>",
        sender="Acme Marketing <news@acme.example>",
        sender_domain="acme.example",
        subject="Special offer for Alice Svensson!",
        stage="playwright",
        failure_reason="Could not verify confirmation at https://user:pw@click.acme.example/u?token=abc998877&email=alice.svensson@icloud.com",
        candidate_links=[
            {
                "text": "Avprenumerera här",
                "href": "https://track.sendgrid.net/ls/click?upn=SecretEncryptedBlob123456&e=alice.svensson%40icloud.com&lang=sv",
            },
            {
                "text": "Manage Preferences",
                "href": "https://acme.example/unsubscribe/eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6ImFsaWNlQGljbG91ZC5jb20ifQ.sig12345?sid=9988776655",
            },
        ],
        headers_snapshot=(
            "From: Acme Marketing <news@acme.example>\n"
            "To: Alice Svensson <alice.svensson@icloud.com>\n"
            "Delivered-To: alice.svensson@icloud.com\n"
            "Return-Path: <bounces+1234-alice.svensson=icloud.com@sendgrid.net>\n"
            "List-Unsubscribe: <mailto:unsub+u=alice.svensson@mg.acme.example?subject=sub-999>, <https://mg.acme.example/unsub?token=TopSecret999&action=unsubscribe>\n"
            "List-Unsubscribe-Post: List-Unsubscribe=One-Click\n"
            "X-Mailer: KlaviyoMailer 2.0\n"
            "X-KMail-Campaign: fall-promo"
        ),
        footer_html_snapshot=(
            '<div class="footer"><p>Sent to Alice Svensson (alice.svensson@icloud.com) by Acme.</p>'
            '<a class="btn-unsub" href="https://mg.acme.example/unsub?token=TopSecret999&e=alice.svensson@icloud.com&lang=sv">Avprenumerera</a></div>'
        ),
        final_url="https://mg.acme.example/unsub?token=TopSecret999&sig=deadbeef123456",
        screenshot_path="/Users/example/private/shot-9042.png",
    )


def test_redact_url_strips_tokens_emails_userinfo_and_jwt_segments():
    u1 = redact_url("https://alice:hunter2@esp.example/preferences-center/eyJhbGciOiJIUzI1NiJ9.payload.sig?token=secret123&lang=sv")
    assert "alice:hunter2" not in u1
    assert "eyJhbGciOiJIUzI1NiJ9" not in u1
    assert "secret123" not in u1
    assert "/preferences-center/<redacted-token>" in u1
    assert "token=<redacted>" in u1
    assert "lang=sv" in u1

    # URL-encoded base64 path segment & mailto trailing punctuation & hidden_domains
    u2 = redact_url("https://esp.example/u/YWxpY2VAbWUuc2U%3D/confirm")
    assert "YWxpY2VAbWUuc2U" not in u2
    assert "/<redacted-token>/" in u2

    m1 = redact_url("mailto:unsub+alice=me.example@mg.acme.example?subject=stop")
    assert m1 == "mailto:<redacted>@mg.acme.example"
    assert "alice" not in m1
    assert "subject=" not in m1

    m2 = redact_url("mailto:alice@hessius.example.", hidden_domains={"hessius.example"})
    assert m2 == "mailto:<redacted>@<redacted>."


def test_real_extractor_headers_without_to_and_sender_brand_preservation(tmp_path: Path):
    entry = {
        "id": 42,
        "account": "Personal",
        "mailbox": "INBOX",
        "internal_id": "42",
        "sender": "Acme <news@acme.example>",
        "sender_domain": "acme.example",
        "subject": "Weekly deal for Alice Svensson",
        "stage": "playwright",
        "failure_reason": "Failed reading /Users/alice/tmp/debug.html on https://mg.acme.example/u(1)?token=SecretToken123",
        "candidate_links": [],
        "headers_snapshot": (
            "From: Acme <news@acme.example>\n"
            "Subject: Weekly deal for Alice Svensson\n"
            "List-Unsubscribe: <https://mg.acme.example/unsub?token=TopSecret999>\n"
            "X-Mailer: SendGrid"
        ),
        "footer_html_snapshot": (
            '<div class="acme"><p>Sent to Alice Svensson (alice.svensson@hessius.example).</p>'
            '<a href="mailto:alice.svensson@hessius.example">Contact</a>'
            '<a href="https://mg.acme.example/unsub(v2)?token=Secret999&lang=sv">Avprenumerera</a></div>'
        ),
        "final_url": "",
    }
    md = format_missed_bug_report(entry, account_emails=["alice.svensson@hessius.example"])
    for secret in [
        "Alice",
        "Svensson",
        "hessius.example",
        "SecretToken123",
        "TopSecret999",
        "Secret999",
        "/Users/alice",
    ]:
        assert secret not in md

    # Sender brand and domain in headers/DOM MUST stay intact
    assert "https://mg.acme.example/unsub" in md
    assert '<div class="acme">' in md
    assert "/Users/<redacted>/tmp/debug.html" in md


def test_missed_export_redacts_pii_while_preserving_esp_headers_and_dom(tmp_path: Path):
    store = TriageStore(tmp_path / "state.db")
    mid = _seed_realistic_missed(store)
    entry = store.get_missed_unsubscribe(mid)
    assert entry is not None

    red = redact_missed_entry(entry)
    md = format_missed_bug_report(entry)

    # Personal email, personal name, tokens, userinfo, and local file paths must never appear
    for secret in [
        "alice.svensson",
        "Alice",
        "Svensson",
        "icloud.com",
        "TopSecret999",
        "SecretEncryptedBlob123456",
        "9988776655",
        "deadbeef123456",
        "eyJhbGci",
        "user:pw",
        "shot-9042.png",
        "msg-secret-id",
    ]:
        assert secret not in md
        assert secret not in json.dumps(red)

    # Useful ESP/DOM diagnostics MUST be preserved
    assert "acme.example" in md
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in md
    assert "X-Mailer: KlaviyoMailer 2.0" in md
    assert "X-KMail-Campaign: fall-promo" in md
    assert "sendgrid.net" in md
    assert '<div class="footer">' in md
    assert '<a class="btn-unsub" href="https://mg.acme.example/unsub?' in md
    assert ">Avprenumerera</a>" in md
    assert "lang=sv" in md


def test_cli_and_web_missed_export_endpoints(tmp_path: Path, monkeypatch, capsys):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))

    cfg = load_config()
    store = TriageStore(cfg.db_path)
    mid = _seed_realistic_missed(store)

    # CLI list still works
    assert cli_main(["missed"]) == 0
    list_out = capsys.readouterr().out
    assert f"#{mid}" in list_out

    # CLI export Markdown & JSON
    assert cli_main(["missed", "export", str(mid)]) == 0
    md_out = capsys.readouterr().out
    assert "Redacted Missed-Unsubscribe Report" in md_out
    assert "Alice" not in md_out
    assert "TopSecret999" not in md_out
    assert "X-KMail-Campaign: fall-promo" in md_out

    assert cli_main(["missed", "export", str(mid), "--json"]) == 0
    json_out = json.loads(capsys.readouterr().out)
    assert json_out["sender_domain"] == "acme.example"
    assert "Alice" not in json.dumps(json_out)

    # Nonexistent ID returns exit 1
    assert cli_main(["missed", "export", "99999"]) == 1
    capsys.readouterr()

    # Web API export + static UI button check
    class DummyBridge:
        def list_accounts(self):
            return []

    app, token = create_app(cfg, store, DummyBridge(), token="export-tok")
    with TestClient(app) as client:
        assert client.get(f"/api/missed/{mid}/export").status_code == 401
        resp = client.get(f"/api/missed/{mid}/export", headers={"X-Triage-Key": token})
        assert resp.status_code == 200
        payload = resp.json()
        assert "Redacted Missed-Unsubscribe Report" in payload["markdown"]
        assert "alice.svensson" not in payload["markdown"]
        assert "TopSecret999" not in payload["markdown"]
        assert client.get("/api/missed/99999/export", headers={"X-Triage-Key": token}).status_code == 404

        app_js = client.get("/static/app.js").text
        en_locale = client.get("/static/locales/en.json").json()
        assert "data-missed-copy-report" in app_js
        assert "missed_snap.copy_report" in app_js
        assert en_locale["missed_snap.copy_report"] == "Copy Redacted Bug Report"
