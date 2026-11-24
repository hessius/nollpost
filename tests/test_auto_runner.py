"""Tests for headless auto-triage selection, run lock, execution, and CLI `nollpost auto`."""

from __future__ import annotations

import json
from email.message import EmailMessage
from pathlib import Path

from nollpost import cli
from nollpost.auto_runner import acquire_run_lock, run_auto_triage, select_auto_actions
from nollpost.classifier import classify_email
from nollpost.config import load_config
from nollpost.models import AccountInfo, ConsultingLinks, ExtractedEmailInfo, MailHeader, PdfAttachment
from nollpost.store import TriageStore


class _AutoFakeBridge:
    def __init__(self, *, include_gmail: bool = False):
        self.include_gmail = include_gmail
        self.archived: list[tuple[str, str, list[str]]] = []
        self.sent_replies: list[tuple[str, str]] = []
        self.gmail_archive_strategy = "background"
        self.archive_overrides: dict[str, str] = {}

    def check_for_new_mail(self) -> bool:
        return False

    def list_accounts(self) -> list[AccountInfo]:
        accts = [AccountInfo("iCloud", ["me@icloud.com"], "INBOX", "Archive", "Sent Messages")]
        if self.include_gmail:
            accts.append(AccountInfo("Google", ["me@gmail.com"], "INBOX", "[Gmail]/All Mail", "[Gmail]/Sent Mail"))
        return accts

    def scan_mailbox_metadata(
        self,
        account: str,
        mailbox: str = "INBOX",
        limit: int = 500,
        include_attachments: bool = True,
        days_back: int = 0,
    ) -> list[MailHeader]:
        if account == "Google":
            return [
                MailHeader(
                    "Google",
                    "INBOX",
                    "201",
                    "g-201",
                    "Package shipped",
                    "PostNord <noreply@postnord.se>",
                    "noreply@postnord.se",
                    "postnord.se",
                    "2026-11-23T09:00:00",
                    False,
                    False,
                    0,
                )
            ]
        return [
            MailHeader(
                "iCloud",
                "INBOX",
                "101",
                "m-101",
                "Card payment receipt",
                "MyBank <alerts@mybank.example>",
                "alerts@mybank.example",
                "mybank.example",
                "2026-11-23T09:00:00",
                False,
                False,
                0,
            ),
            MailHeader(
                "iCloud",
                "INBOX",
                "102",
                "m-102",
                "Security Alert: New device",
                "MyBank <alerts@mybank.example>",
                "alerts@mybank.example",
                "mybank.example",
                "2026-11-23T09:01:00",
                False,
                False,
                0,
            ),
            MailHeader(
                "iCloud",
                "INBOX",
                "103",
                "m-103",
                "Oracle ERP Consultation",
                "ExpertNet <rm@expertnet.example>",
                "rm@expertnet.example",
                "expertnet.example",
                "2026-11-23T09:02:00",
                False,
                False,
                0,
            ),
            MailHeader(
                "iCloud",
                "INBOX",
                "104",
                "m-104",
                "MedTech Diagnostics Consultation",
                "ExpertNet <rm@expertnet.example>",
                "rm@expertnet.example",
                "expertnet.example",
                "2026-11-23T09:03:00",
                False,
                False,
                0,
            ),
        ]

    def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
        msg = EmailMessage()
        if internal_id == "101":
            msg["Subject"] = "Card payment receipt"
            msg["From"] = "MyBank <alerts@mybank.example>"
            msg.set_content("You spent 120 SEK.")
        elif internal_id == "102":
            msg["Subject"] = "Security Alert: New device"
            msg["From"] = "MyBank <alerts@mybank.example>"
            msg.set_content("Verification code: 999111")
        elif internal_id == "103":
            msg["Subject"] = "Oracle ERP Consultation"
            msg["From"] = "ExpertNet <rm@expertnet.example>"
            msg.set_content("Need an Oracle ERP specialist for a short call.")
        elif internal_id == "104":
            msg["Subject"] = "MedTech Diagnostics Consultation"
            msg["From"] = "ExpertNet <rm@expertnet.example>"
            msg.set_content("Need a MedTech specialist.")
        else:
            msg["Subject"] = "Package shipped"
            msg["From"] = "PostNord <noreply@postnord.se>"
            msg.set_content("Your package is on the way.")
        return msg.as_bytes()

    def create_or_send_reply(
        self,
        account: str,
        mailbox: str,
        internal_id: str,
        body: str,
        *,
        send_now: bool = False,
        open_window: bool = False,
    ) -> None:
        assert send_now is True
        assert open_window is False
        self.sent_replies.append((internal_id, body))

    def mark_read_and_archive(
        self,
        account: str,
        mailbox: str,
        archive_mailbox: str,
        internal_ids: list[str],
        *,
        mark_read: bool = True,
    ) -> list[str]:
        self.archived.append((account, archive_mailbox, list(internal_ids)))
        return list(internal_ids)

    def synchronize_account(self, account: str, timeout: int = 8) -> None:
        return None


def _setup_cfg(tmp_path: Path, monkeypatch, extra_toml: str = "") -> tuple[Path, Path]:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(extra_toml, encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(data_dir))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    return cfg_path, data_dir


def test_select_auto_actions_rules_guardrails_and_failure_suppression(tmp_path: Path, monkeypatch):
    _setup_cfg(
        tmp_path,
        monkeypatch,
        """
[consulting]
enabled = true
networks = ["expertnet.example"]
match_keywords = ["medtech"]
negative_keywords = ["oracle"]

[automation]
enabled = true
auto_decline_consulting = true
auto_archive_transactional = true
auto_archive_bot_noise = true
""",
    )
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    store.record_sender_preference("alerts@mybank.example", "MyBank", "auto_archive", keep_subscribed=True)

    bank_ok = classify_email(
        MailHeader("iCloud", "INBOX", "1", "r1", "Card notice", "B <alerts@mybank.example>", "alerts@mybank.example", "mybank.example", "2026-11-23T09:00:00", False, False, 0),
        ExtractedEmailInfo(),
        cfg,
        "Archive",
        sender_mem=store.get_sender_memory("alerts@mybank.example"),
    )
    bank_pdf = classify_email(
        MailHeader("iCloud", "INBOX", "2", "r2", "Statement", "B <alerts@mybank.example>", "alerts@mybank.example", "mybank.example", "2026-11-23T09:00:00", False, False, 1),
        ExtractedEmailInfo(pdf_attachments=[PdfAttachment("stmt.pdf", 1, 1024)]),
        cfg,
        "Archive",
        sender_mem=store.get_sender_memory("alerts@mybank.example"),
    )
    consult_link = classify_email(
        MailHeader("iCloud", "INBOX", "3", "r3", "Oracle ERP", "E <rm@expertnet.example>", "rm@expertnet.example", "expertnet.example", "2026-11-23T09:00:00", False, False, 0),
        ExtractedEmailInfo(consulting_links=ConsultingLinks(decline_url="https://expertnet.example/decline")),
        cfg,
        "Archive",
    )
    consult_reply = classify_email(
        MailHeader("iCloud", "INBOX", "4", "r4", "Oracle ERP 2", "E <rm@expertnet.example>", "rm@expertnet.example", "expertnet.example", "2026-11-23T09:00:00", False, False, 0),
        ExtractedEmailInfo(),
        cfg,
        "Archive",
    )
    consult_match = classify_email(
        MailHeader("iCloud", "INBOX", "5", "r5", "MedTech role", "E <rm@expertnet.example>", "rm@expertnet.example", "expertnet.example", "2026-11-23T09:00:00", False, False, 0),
        ExtractedEmailInfo(),
        cfg,
        "Archive",
    )
    tx_item = classify_email(
        MailHeader("iCloud", "INBOX", "6", "r6", "Order shipped", "Shop <shop@store.example>", "shop@store.example", "store.example", "2026-11-23T09:00:00", False, False, 0),
        ExtractedEmailInfo(plain_excerpt="Your order has shipped! Track your package."),
        cfg,
        "Archive",
    )
    bot_item = classify_email(
        MailHeader("iCloud", "INBOX", "7", "r7", "Dependabot PR", "dependabot[bot] <notifications@github.com>", "notifications@github.com", "github.com", "2026-11-23T09:00:00", False, False, 0),
        ExtractedEmailInfo(),
        cfg,
        "Archive",
    )

    # Prior failure on consult_link must suppress repeat auto-decline
    store.log_action(
        run_id="auto-prev",
        account="iCloud",
        internal_id="3",
        sender="E <rm@expertnet.example>",
        subject="Oracle ERP",
        action="decline_and_archive",
        status="failed",
        rfc_message_id="r3",
        source="auto",
    )

    selected, warnings = select_auto_actions(
        [bank_ok, bank_pdf, consult_link, consult_reply, consult_match, tx_item, bot_item],
        cfg,
        store,
    )
    assert warnings == []
    chosen = {it.header.internal_id: act for it, act in selected}
    assert chosen == {
        "1": "archive",
        "4": "send_decline_and_archive",
        "6": "archive",
        "7": "archive",
    }


def test_select_auto_actions_skips_gmail_when_ui_menu(tmp_path: Path, monkeypatch):
    _setup_cfg(
        tmp_path,
        monkeypatch,
        """
[accounts]
gmail_archive_strategy = "ui_menu"

[rules]
enabled_packs = ["se"]

[automation]
auto_archive_transactional = true
""",
    )
    cfg = load_config()
    store = TriageStore(cfg.db_path)

    gmail_tx = classify_email(
        MailHeader("Google", "INBOX", "201", "g1", "Ditt paket", "PostNord <noreply@postnord.se>", "noreply@postnord.se", "postnord.se", "2026-11-23T09:00:00", False, False, 0),
        ExtractedEmailInfo(plain_excerpt="Din leverans är på väg"),
        cfg,
        "[Gmail]/All Mail",
    )
    icloud_tx = classify_email(
        MailHeader("iCloud", "INBOX", "202", "i1", "Ditt paket", "PostNord <noreply@postnord.se>", "noreply@postnord.se", "postnord.se", "2026-11-23T09:00:00", False, False, 0),
        ExtractedEmailInfo(plain_excerpt="Din leverans är på väg"),
        cfg,
        "Archive",
    )
    selected, warnings = select_auto_actions([gmail_tx, icloud_tx], cfg, store)
    assert [it.header.internal_id for it, _ in selected] == ["202"]
    assert any("ui_menu" in w for w in warnings)


def test_select_auto_actions_accepts_localized_inbox_names(tmp_path: Path, monkeypatch):
    _setup_cfg(
        tmp_path,
        monkeypatch,
        """
[automation]
auto_archive_sender_rules = true
""",
    )
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    store.record_sender_preference("alerts@mybank.example", "MyBank", "auto_archive", keep_subscribed=True)

    swedish_inbox_item = classify_email(
        MailHeader("iCloud", "Inkorgen", "301", "s1", "Kontohändelse", "MyBank <alerts@mybank.example>", "alerts@mybank.example", "mybank.example", "2026-11-23T09:00:00", False, False, 0),
        ExtractedEmailInfo(),
        cfg,
        "Arkiv",
        sender_mem=store.get_sender_memory("alerts@mybank.example"),
    )
    selected, warnings = select_auto_actions(
        [swedish_inbox_item],
        cfg,
        store,
        inbox_mailboxes={"iCloud": "Inkorgen"},
    )
    assert warnings == []
    assert [(it.header.internal_id, act) for it, act in selected] == [("301", "archive")]


def test_run_auto_triage_dry_run_execute_and_lock(tmp_path: Path, monkeypatch):
    _setup_cfg(
        tmp_path,
        monkeypatch,
        """
[consulting]
enabled = true
networks = ["expertnet.example"]
match_keywords = ["medtech"]
negative_keywords = ["oracle"]

[automation]
enabled = true
auto_decline_consulting = true
""",
    )
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    store.record_sender_preference("alerts@mybank.example", "MyBank", "auto_archive", keep_subscribed=True)
    bridge = _AutoFakeBridge()

    # Mail not running -> clean skip
    skipped_mail = run_auto_triage(cfg, store, bridge, mail_running_checker=lambda: False)
    assert skipped_mail["status"] == "skipped"
    assert skipped_mail["reason"] == "mail_not_running"

    # Busy lock -> clean skip
    with acquire_run_lock(cfg.data_dir) as acquired:
        assert acquired is True
        skipped_busy = run_auto_triage(cfg, store, bridge, mail_running_checker=lambda: True)
        assert skipped_busy["status"] == "skipped"
        assert skipped_busy["reason"] == "busy"

    # Dry run -> 0 mutations, planned actions listed
    dry = run_auto_triage(cfg, store, bridge, dry_run=True, mail_running_checker=lambda: True)
    assert dry["status"] == "dry_run"
    assert len(dry["planned"]) == 2
    assert bridge.archived == []
    assert bridge.sent_replies == []

    # Real run -> archives bank alert + sends boilerplate decline & archives mismatch (never touches Security Alert or MedTech match)
    res = run_auto_triage(cfg, store, bridge, dry_run=False, mail_running_checker=lambda: True)
    assert res["status"] == "ok"
    assert res["summary"] == {"archived": 1, "declined": 1, "failed": 0}
    assert res["unseen_count"] == 2
    assert len(bridge.sent_replies) == 1
    assert bridge.sent_replies[0][0] == "103"
    archived_ids = [mid for _, _, ids in bridge.archived for mid in ids]
    assert set(archived_ids) == {"101", "103"}
    assert store.list_missed_unsubscribes() == []

    status_file = cfg.data_dir / "auto_status.json"
    assert status_file.exists()
    marker = json.loads(status_file.read_text(encoding="utf-8"))
    assert marker["unseen_count"] == 2
    assert marker["last_status"] == "ok"


def test_cli_auto_json(tmp_path: Path, monkeypatch, capsys):
    _setup_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr("nollpost.auto_runner._is_mail_running", lambda: False)
    rc = cli.main(["auto", "--dry-run", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "skipped"
    assert payload["reason"] == "mail_not_running"


def test_auto_force_flag_and_scan_warnings_preserved(tmp_path: Path, monkeypatch):
    _setup_cfg(
        tmp_path,
        monkeypatch,
        """
[automation]
enabled = false
""",
    )
    cfg = load_config()
    store = TriageStore(cfg.db_path)
    store.record_sender_preference("alerts@mybank.example", "MyBank", "auto_archive", keep_subscribed=True)

    class PartialWarnBridge(_AutoFakeBridge):
        def list_accounts(self):
            return [
                AccountInfo("iCloud", ["me@icloud.com"], "INBOX", "Archive", "Sent Messages"),
                AccountInfo("BrokenAcct", ["b@example.com"], "INBOX", "Archive", "Sent"),
            ]

        def scan_mailbox_metadata(self, account: str, mailbox: str = "INBOX", limit: int = 500, include_attachments: bool = True, days_back: int = 0):
            if account == "BrokenAcct":
                raise RuntimeError("BrokenAcct offline")
            return super().scan_mailbox_metadata(account, mailbox, limit, include_attachments, days_back)

    bridge = PartialWarnBridge()
    skipped = run_auto_triage(cfg, store, bridge, mail_running_checker=lambda: True)
    assert skipped["status"] == "skipped"
    assert skipped["reason"] == "disabled"

    forced = run_auto_triage(cfg, store, bridge, force=True, mail_running_checker=lambda: True)
    assert forced["status"] == "ok"
    assert forced["summary"]["archived"] == 1
    assert forced["active_rules"]["sender_auto_archive"] == 1
    assert forced["active_rules"]["auto_decline_consulting"] is False
    assert any("BrokenAcct offline" in w for w in forced["warnings"])
    digest = store.get_auto_digest()
    assert any("BrokenAcct offline" in w for w in digest["last_warnings"])
