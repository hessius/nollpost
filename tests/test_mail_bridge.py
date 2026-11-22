"""Tests for mail_bridge non-deletion guardrails and parsing helpers."""

import pytest

from nollpost.mail_bridge import (
    AppleMailBridge,
    decode_field,
    is_gmail_all_mail_destination,
    mailbox_ref_applescript,
    parse_sender_parts,
    resolve_lock_path,
    validate_archive_destination,
)


@pytest.mark.parametrize(
    "bad_box",
    ["Trash", "Deleted Messages", "Papperskorgen", "Skräppost", "Junk", "Spam", "Folder/Trash", "Trash/Subfolder"],
)
def test_forbidden_trash_destinations_raise(bad_box: str):
    with pytest.raises(ValueError, match="prohibited"):
        validate_archive_destination(bad_box)


@pytest.mark.parametrize("good_box", ["Archive", "All e-post", "[Gmail]/All Mail", "Arkiv"])
def test_allowed_archive_destinations_pass(good_box: str):
    assert validate_archive_destination(good_box) == good_box


def test_parse_sender_and_decode_field():
    email, domain = parse_sender_parts('"Coop" <Info@e.coop.se>')
    assert email == "info@e.coop.se"
    assert domain == "e.coop.se"
    assert decode_field("100%25 valid %7C test") == "100% valid | test"


def test_list_accounts_resolves_gmail_container_archive(monkeypatch):
    def fake_osascript(script: str, timeout: int = 35) -> str:
        return (
            "iCloud|me@icloud.com|INBOX;Archive;Sent Messages\n"
            "Google|me@gmail.com|INBOX;[Gmail]/All e-post;[Gmail]/Skickat"
        )

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_osascript)
    accounts = AppleMailBridge().list_accounts()
    by_name = {a.name: a for a in accounts}
    assert by_name["iCloud"].archive_mailbox == "Archive"
    assert by_name["Google"].archive_mailbox == "[Gmail]/All e-post"
    assert by_name["Google"].sent_mailbox == "[Gmail]/Skickat"
    assert 'resolveMailbox(targetAccount, "[Gmail]/All e-post")' in mailbox_ref_applescript("[Gmail]/All e-post")
    assert is_gmail_all_mail_destination("[Gmail]/All e-post") is True
    assert is_gmail_all_mail_destination("[Gmail]/Arkiv") is True
    assert is_gmail_all_mail_destination("[Google Mail]/All Mail") is True
    assert is_gmail_all_mail_destination("Archive") is False


def test_gmail_tag_archive_primary_with_ui_fallback(monkeypatch):
    bridge = AppleMailBridge(gmail_archive_strategy="tag_staging")
    monkeypatch.setattr("nollpost.mail_bridge.time.sleep", lambda s: None)
    scripts: list[str] = []

    # 1. Primary path: _ArchiveTmp tag succeeds for all ids -> UI fallback is NOT invoked
    def fake_tag_ok(script: str, timeout: int = 120) -> str:
        scripts.append(script)
        assert "_ArchiveTmp" in script and "synchronize with targetAccount" in script
        assert "front window" not in script
        return "10,11"

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_tag_ok)
    moved = bridge.mark_read_and_archive("Google", "INBOX", "[Gmail]/All e-post", ["10", "11"])
    assert moved == ["10", "11"]
    assert len(scripts) == 1
    assert all("delete " not in s.lower() and "Papperskorgen" not in s for s in scripts)

    # 2. Fallback path: _ArchiveTmp tag only tags "10" (or fails for "11"), so UI archive handles "11"
    scripts.clear()
    inbox = {"11", "12"}

    def fake_ids(account, mailbox="INBOX"):
        return set(inbox)

    def fake_partial_tag_then_ui(script: str, timeout: int = 120) -> str:
        scripts.append(script)
        if "_ArchiveTmp" in script:
            return "10"
        if "front window" in script:
            assert "whose id is 11)" in script
            inbox.discard("11")
            return "ok"
        return ""

    monkeypatch.setattr(bridge, "list_mailbox_ids", fake_ids)
    monkeypatch.setattr(bridge, "mark_read", lambda a, m, ids: list(ids))
    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_partial_tag_then_ui)
    moved_fb = bridge.mark_read_and_archive("Google", "INBOX", "[Gmail]/All e-post", ["10", "11"])
    assert moved_fb == ["10", "11"]
    assert any("_ArchiveTmp" in s for s in scripts)
    assert any("front window" in s for s in scripts)


def test_gmail_background_archive_default_and_safety_guards(monkeypatch):
    bridge = AppleMailBridge()
    assert bridge.gmail_archive_strategy == "background"
    scripts: list[str] = []

    def fake_bg_osascript(script: str, timeout: int = 120) -> str:
        scripts.append(script)
        assert '"[Gmail]"' in script and '"[Google Mail]"' in script
        assert "papperskorgen" in script.lower()
        assert "if msgRfc is not missing value and msgRfc is not \"\"" in script
        assert "prevTrashIds" in script
        assert "move newMsg to archMailbox" in script
        assert "move newMsg to sourceMailbox" in script
        assert "System Events" not in script and "activate" not in script
        assert "delete " not in script.lower()
        return "10,11"

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_bg_osascript)
    moved = bridge.mark_read_and_archive("Google", "INBOX", "[Gmail]/All e-post", ["10", "11"])
    assert moved == ["10", "11"]
    assert len(scripts) == 1


def test_gmail_ui_archive_verifies_target_and_stops_on_surprise(monkeypatch):
    bridge = AppleMailBridge(gmail_archive_strategy="ui_menu")
    assert bridge.gmail_archive_strategy == "ui_menu"
    monkeypatch.setattr("nollpost.mail_bridge.time.sleep", lambda s: None)
    scripts: list[str] = []

    # Scenario A: each click removes exactly the targeted id from INBOX -> both reported moved
    inbox = {"10", "11", "12"}
    order: list[str] = []

    def fake_ids(account, mailbox="INBOX"):
        return set(inbox)

    def fake_osascript_ok(script: str, timeout: int = 45) -> str:
        scripts.append(script)
        assert "_ArchiveTmp" not in script
        if "front window" in script:
            mid = next(m for m in ("10", "11") if f"whose id is {m})" in script)
            inbox.discard(mid)
            order.append(mid)
            return "ok"
        return ""

    monkeypatch.setattr(bridge, "list_mailbox_ids", fake_ids)
    monkeypatch.setattr(bridge, "mark_read", lambda a, m, ids: list(ids))
    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_osascript_ok)
    moved = bridge.mark_read_and_archive("Google", "INBOX", "[Gmail]/All e-post", ["10", "11"])
    assert moved == ["10", "11"] and order == ["10", "11"]
    assert all("delete " not in s.lower() and "Papperskorgen" not in s for s in scripts)
    assert all('if subj is "" then return "ABORT:' in s and "close front window" in s for s in scripts)
    ui_scripts = [s for s in scripts if "front window" in s]
    assert ui_scripts and all("toolbar 1 of targetWin" in s and "AXFocusedWindow" in s and "click mbi" in s for s in ui_scripts)

    # Scenario B: clicking archives a DIFFERENT message ("12") -> target not reported, batch stops
    inbox.clear()
    inbox.update({"10", "11", "12"})
    calls: list[str] = []

    def fake_osascript_wrong(script: str, timeout: int = 45) -> str:
        if "front window" in script:
            calls.append("click")
            inbox.discard("12")
            return "ok"
        return ""

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_osascript_wrong)
    moved_b = bridge.mark_read_and_archive("Google", "INBOX", "[Gmail]/All e-post", ["10", "11"])
    assert moved_b == [] and len(calls) == 1


def test_gmail_two_hop_archive_and_list_mailbox_ids(monkeypatch):
    scripts: list[str] = []

    def fake_osascript(script: str, timeout: int = 120) -> str:
        scripts.append(script)
        if "_ArchiveTmp" in script and "sourceMailbox" in script:
            return "10|msg10@gmail.com\n11|msg11@gmail.com"
        if "_ArchiveTmp" in script and "destMailbox" in script:
            return "msg10@gmail.com\nmsg11@gmail.com"
        if "__EMPTY__" in script:
            return "10,11"
        return ""

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_osascript)
    bridge = AppleMailBridge()
    moved = bridge._archive_gmail_two_hop("Google", "INBOX", "[Gmail]/All e-post", ["10", "11"], mark_read=True)
    assert moved == ["10", "11"]
    assert len(scripts) == 2
    assert "_ArchiveTmp" in scripts[0] and "synchronize with targetAccount" in scripts[0]
    assert "destMailbox" in scripts[1] and "_ArchiveTmp" in scripts[1]
    assert all("delete " not in s.lower() for s in scripts)
    assert bridge.list_mailbox_ids("Google", "INBOX") == {"10", "11"}

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", lambda s, timeout=30: "__TOO_LARGE__")
    assert bridge.list_mailbox_ids("Google", "INBOX") is None
    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", lambda s, timeout=30: "__EMPTY__")
    assert bridge.list_mailbox_ids("Google", "INBOX") == set()


def test_resolve_lock_path_respects_override_and_default(tmp_path, monkeypatch):
    custom = tmp_path / "custom.lock"
    monkeypatch.setenv("NOLLPOST_LOCK_PATH", str(custom))
    assert resolve_lock_path() == custom

    monkeypatch.delenv("NOLLPOST_LOCK_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_lock_path() == tmp_path / "Library/Application Support/nollpost/mail_automation.lock"

    mcp_dir = tmp_path / "Library/Application Support/apple-mail-mcp"
    mcp_dir.mkdir(parents=True)
    assert resolve_lock_path() == mcp_dir / "mail_automation.lock"
