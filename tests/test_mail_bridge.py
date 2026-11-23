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
            "Google|me@gmail.com|INBOX;[Gmail]/All e-post;[Gmail]/Skickat\n"
            "GoogleUK|uk@gmail.com|INBOX;[Google Mail]/All Mail;[Google Mail]/Sent Mail"
        )

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_osascript)
    accounts = AppleMailBridge().list_accounts()
    by_name = {a.name: a for a in accounts}
    assert by_name["iCloud"].archive_mailbox == "Archive"
    assert by_name["Google"].archive_mailbox == "[Gmail]/All e-post"
    assert by_name["Google"].sent_mailbox == "[Gmail]/Skickat"
    assert by_name["GoogleUK"].archive_mailbox == "[Google Mail]/All Mail"
    assert 'resolveMailbox(targetAccount, "[Gmail]/All e-post")' in mailbox_ref_applescript("[Gmail]/All e-post")
    assert is_gmail_all_mail_destination("[Gmail]/All e-post") is True
    assert is_gmail_all_mail_destination("[Gmail]/Arkiv") is True
    assert is_gmail_all_mail_destination("[Google Mail]/All Mail") is True
    assert is_gmail_all_mail_destination("[Google Mail]/Arkiv") is True
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
        assert "every mailbox of targetAccount" in script
        assert "papperskorgen" in script.lower()
        assert "if msgRfc is not missing value and msgRfc is not \"\"" in script
        assert "prevTrashIds" in script
        assert "move newMsg to archMailbox" in script
        assert "move newMsg to sourceMailbox" in script
        assert "__STRANDED__:" in script
        assert "System Events" not in script and "activate" not in script
        assert "delete " not in script.lower()
        return "20" if "{20}" in script else "10,11"

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_bg_osascript)
    moved = bridge.mark_read_and_archive("Google", "INBOX", "[Gmail]/All e-post", ["10", "11"])
    assert moved == ["10", "11"]
    assert len(scripts) == 1
    assert "set read status of targetMsg to true" in scripts[0]

    # mark_read=False omits read-status mutation
    scripts.clear()
    moved_unread = bridge.mark_read_and_archive("GoogleUK", "INBOX", "[Google Mail]/All Mail", ["20"], mark_read=False)
    assert moved_unread == ["20"]
    assert "set read status" not in scripts[0]

    # Non-Gmail destination still uses direct _move_to_mailbox under default background strategy
    scripts.clear()

    def fake_direct_osascript(script: str, timeout: int = 120) -> str:
        scripts.append(script)
        return "30"

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_direct_osascript)
    moved_icloud = bridge.mark_read_and_archive("iCloud", "INBOX", "Archive", ["30"])
    assert moved_icloud == ["30"]
    assert "trashBox" not in scripts[0] and "move targetMsg to destMailbox" in scripts[0]

    # Empty / non-digit IDs do not invoke osascript
    scripts.clear()
    assert bridge.mark_read_and_archive("Google", "INBOX", "[Gmail]/All e-post", ["", "abc"]) == []
    assert scripts == []

    # Forbidden destination raises even under default background strategy
    with pytest.raises(ValueError, match="prohibited"):
        bridge.mark_read_and_archive("Google", "INBOX", "Papperskorgen", ["10"])

    # Stranded response raises loudly rather than silently dropping the ID
    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", lambda s, timeout=120: "__STRANDED__:10|")
    with pytest.raises(RuntimeError, match="Trash"):
        bridge.mark_read_and_archive("Google", "INBOX", "[Gmail]/All e-post", ["10"])


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


def test_executor_skips_timeout_inbox_reconcile_in_background_gmail_mode(tmp_path, monkeypatch):
    import asyncio
    from nollpost.config import load_config
    from nollpost.executor import BatchExecutor
    from nollpost.models import ClassifiedItem, ExtractedEmailInfo, MailHeader
    from nollpost.store import TriageStore

    monkeypatch.setenv("NOLLPOST_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    cfg = load_config()
    store = TriageStore(cfg.db_path)

    class _TimeoutBgBridge:
        gmail_archive_strategy = "background"

        def mark_read_and_archive(self, *args, **kwargs):
            raise TimeoutError("osascript timed out")

        def list_mailbox_ids(self, account, mailbox="INBOX"):
            return set()  # Absent from INBOX (could be in Trash)

    item = ClassifiedItem(
        uid="Google:INBOX:10",
        header=MailHeader(
            account="Google",
            mailbox="INBOX",
            internal_id="10",
            rfc_message_id="m10@gmail.com",
            subject="News",
            sender="News <n@example.com>",
            sender_email="n@example.com",
            sender_domain="example.com",
            date_received="2026-11-22T10:00:00",
            is_read=False,
            is_flagged=False,
            attachment_count=0,
        ),
        extracted=ExtractedEmailInfo(),
        category="updates",
        subcategory="newsletter",
        default_action="archive",
        allowed_actions=["archive", "no_action"],
        reason="test",
        archive_mailbox="[Gmail]/All e-post",
    )
    executor = BatchExecutor(cfg, store, _TimeoutBgBridge())  # type: ignore[arg-type]
    results = asyncio.run(executor.execute_batch([(item, "archive")], {"Google": []}))
    assert results[0].status == "failed"
    assert "verified moved" not in results[0].detail
    store.close()


def test_check_for_new_mail_triggers_fetch_and_handles_timeout(monkeypatch):
    scripts: list[str] = []

    def fake_ok(script: str, timeout: int = 120) -> str:
        scripts.append(script)
        return ""

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_ok)
    bridge = AppleMailBridge()
    assert bridge.check_for_new_mail(timeout=5) is True
    assert len(scripts) == 1
    assert "check for new mail" in scripts[0]
    assert "with timeout of 5 seconds" in scripts[0]

    def fake_fail(script: str, timeout: int = 120) -> str:
        raise TimeoutError("timed out")

    monkeypatch.setattr("nollpost.mail_bridge.run_osascript", fake_fail)
    assert bridge.check_for_new_mail(timeout=5) is False
