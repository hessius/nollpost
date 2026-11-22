"""Tests for personalized classifier rules, defaults, and preference memory."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nollpost.classifier import classify_email
from nollpost.config import load_config
from nollpost.models import ConsultingLinks, ExtractedEmailInfo, MailHeader, PdfAttachment, UnsubTarget
from nollpost.store import SenderMemoryRecord


def _hdr(
    subject: str,
    sender: str,
    sender_email: str,
    sender_domain: str,
    *,
    account: str = "iCloud",
    is_read: bool = False,
) -> MailHeader:
    return MailHeader(
        account=account,
        mailbox="INBOX",
        internal_id="101",
        rfc_message_id="msg-101",
        subject=subject,
        sender=sender,
        sender_email=sender_email,
        sender_domain=sender_domain,
        date_received="2026-11-19T12:00:00",
        is_read=is_read,
        is_flagged=False,
        attachment_count=0,
    )


def test_medtech_consulting_match_vs_oracle_mismatch(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[profile]
consulting_networks = ["guidepointglobal.com", "colemanrg.com", "glgroup.com"]
consulting_match_keywords = ["medtech", "medical device", "physician"]
consulting_negative_keywords = ["oracle", "erp", "semiconductor"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    cfg = load_config()

    match_item = classify_email(
        _hdr("Consultation: MedTech Diagnostics", "Guidepoint <rm@guidepointglobal.com>", "rm@guidepointglobal.com", "guidepointglobal.com"),
        ExtractedEmailInfo(
            consulting_links=ConsultingLinks(review_url="https://gp.test/rev", decline_url="https://gp.test/dec"),
            pertinent_url="https://gp.test/rev",
            plain_excerpt="Seeking a physician experienced in medical device workflows.",
        ),
        cfg,
        "Archive",
    )
    assert match_item.category == "consulting"
    assert match_item.subcategory == "Consulting Match"
    assert match_item.default_action == "read_open_link"

    mismatch_btn = classify_email(
        _hdr("Oracle ERP & Database", "Coleman <jz@colemanrg.com>", "jz@colemanrg.com", "colemanrg.com"),
        ExtractedEmailInfo(
            consulting_links=ConsultingLinks(review_url="https://col.test/rev", decline_url="https://col.test/dec"),
            plain_excerpt="Oracle ERP HCM CRM senior sales professionals.",
        ),
        cfg,
        "Archive",
    )
    assert mismatch_btn.subcategory == "Consulting Mismatch"
    assert mismatch_btn.default_action == "decline_and_archive"

    mismatch_no_btn = classify_email(
        _hdr("Semiconductor Supply Chain", "GLG <rm@glgroup.com>", "rm@glgroup.com", "glgroup.com"),
        ExtractedEmailInfo(plain_excerpt="Please reply if interested."),
        cfg,
        "Archive",
    )
    assert mismatch_no_btn.default_action == "draft_decline"
    assert "send_decline_and_archive" in mismatch_no_btn.allowed_actions


def test_github_human_vs_bot(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOLLPOST_CONFIG", str(tmp_path / "config.toml"))
    cfg = load_config(ensure_files=False)

    bot = classify_email(
        _hdr("Re: PR #302", '"vercel[bot]" <notifications@github.com>', "notifications@github.com", "github.com"),
        ExtractedEmailInfo(pertinent_url="https://github.com/org/repo/pull/302"),
        cfg,
        "Archive",
    )
    assert bot.category == "newsletters"
    assert bot.default_action == "archive"

    human = classify_email(
        _hdr("Re: PR #302 comment", "Jane Doe <notifications@github.com>", "notifications@github.com", "github.com"),
        ExtractedEmailInfo(pertinent_url="https://github.com/org/repo/pull/302#issuecomment-1"),
        cfg,
        "Archive",
    )
    assert human.category == "updates"
    assert human.default_action == "read_open_link"


def test_bill_invoice_and_kivra_and_creators(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[profile]\nwanted_domains = ["creator.example"]\n', encoding="utf-8")
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    cfg = load_config(ensure_files=False)
    assert cfg.gmail_archive_strategy == "ui_menu"

    inv = classify_email(
        _hdr("Faktura 5819", "Acme Billing <faktura-no-reply@billing.example.se>", "faktura-no-reply@billing.example.se", "billing.example.se"),
        ExtractedEmailInfo(pdf_attachments=[PdfAttachment("Faktura.pdf", 1, 5000)]),
        cfg,
        "Archive",
    )
    assert inv.category == "attention"
    assert inv.default_action == "read_show_invoice"

    met = classify_email(
        _hdr("Release Update", "Creator <hello@creator.example>", "hello@creator.example", "creator.example"),
        ExtractedEmailInfo(pertinent_url="https://creator.example/update"),
        cfg,
        "All e-post",
    )
    assert met.category == "updates"
    assert met.default_action == "read_open_link"
    assert met.archive_mailbox == "All e-post"


def test_unrecognized_noreply_falls_back_safely_to_no_action(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOLLPOST_CONFIG", str(tmp_path / "config.toml"))
    cfg = load_config(ensure_files=False)

    item = classify_email(
        _hdr("Confirmation #9941", "Booking <no-reply@example-clinic.se>", "no-reply@example-clinic.se", "example-clinic.se"),
        ExtractedEmailInfo(plain_excerpt="Your appointment is scheduled for Friday."),
        cfg,
        "Archive",
    )
    assert item.category == "attention"
    assert item.default_action == "no_action"


def test_newsletter_unsub_gate_learned_preference_and_repeat_offender(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOLLPOST_CONFIG", str(tmp_path / "config.toml"))
    cfg = load_config(ensure_files=False)

    # No unsub target -> disabled
    no_unsub = classify_email(
        _hdr("Black Week Deal", "Brand <news@brand.se>", "news@brand.se", "brand.se"),
        ExtractedEmailInfo(newsletter_signals=["text:promo_or_unsub"]),
        cfg,
        "Archive",
    )
    assert no_unsub.default_action == "archive"
    assert no_unsub.no_unsub_found is True
    assert "archive_and_unsub" not in no_unsub.allowed_actions

    # Opted not to unsub -> learned preference
    mem_keep = SenderMemoryRecord(
        sender_key="news@brand.se",
        display_name="Brand",
        preferred_action="archive",
        keep_subscribed=True,
        unsubscribed_at=None,
        unsub_count=0,
        last_seen_at="2026-11-18T10:00:00+00:00",
    )
    learned = classify_email(
        _hdr("Weekly Digest", "Brand <news@brand.se>", "news@brand.se", "brand.se"),
        ExtractedEmailInfo(
            unsub_target=UnsubTarget("https_link", "https://brand.se/unsub", "body", "Unsubscribe", 0.9),
            newsletter_signals=["List-Unsubscribe"],
        ),
        cfg,
        "Archive",
        sender_mem=mem_keep,
    )
    assert learned.default_action == "archive"
    assert "archive_and_unsub" in learned.allowed_actions
    assert "Learned: archive" in learned.badges

    # Sent >48h after unsub -> repeat offender escalation
    old_unsub = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    mem_repeat = SenderMemoryRecord(
        sender_key="news@brand.se",
        display_name="Brand",
        preferred_action=None,
        keep_subscribed=False,
        unsubscribed_at=old_unsub,
        unsub_count=1,
        last_seen_at=old_unsub,
    )
    rep = classify_email(
        _hdr("Still emailing you", "Brand <news@brand.se>", "news@brand.se", "brand.se"),
        ExtractedEmailInfo(
            unsub_target=UnsubTarget("rfc8058_one_click", "https://brand.se/unsub", "header", "List-Unsubscribe-Post", 1.0),
            newsletter_signals=["List-Unsubscribe"],
        ),
        cfg,
        "Archive",
        sender_mem=mem_repeat,
    )
    assert rep.repeat_offender_days is not None and rep.repeat_offender_days >= 4
    assert rep.default_action == "archive_and_unsub"


def test_word_boundary_bill_protection_and_catchup_safety(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[rules]\nenabled_packs = ["se"]\n', encoding="utf-8")
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    cfg = load_config(ensure_files=False)

    # "democracy" must NOT substring-match "ocr" (consulting keyword) -> treated cleanly via plain noreply fallback
    democracy = classify_email(
        _hdr("Notes on Democracy Today", "Forum <no-reply@forum.org>", "no-reply@forum.org", "forum.org"),
        ExtractedEmailInfo(plain_excerpt="Discussion of democracy and civic participation."),
        cfg,
        "Archive",
    )
    assert democracy.category != "consulting"

    # Bill reminder that happens to include a List-Unsubscribe header must remain protected in attention
    bill_with_unsub = classify_email(
        _hdr("Faktura förfallodatum 2026-11-30", "Elbolag <no-reply@elbolag.se>", "no-reply@elbolag.se", "elbolag.se"),
        ExtractedEmailInfo(
            unsub_target=UnsubTarget("https_link", "https://elbolag.se/unsub", "header", "List-Unsubscribe", 0.9),
            newsletter_signals=["List-Unsubscribe"],
            plain_excerpt="Att betala: 649 kr. Bankgiro 123-4567. OCR 99112233.",
        ),
        cfg,
        "Archive",
    )
    assert bill_with_unsub.category == "attention"
    assert bill_with_unsub.default_action in ("read_open_link", "no_action")

    # Catch-Up mode must never auto-select unsub for Kivra/Fortnox or human GitHub messages
    kivra_catchup = classify_email(
        _hdr("Du har ett nytt brev i Kivra", "Kivra <no-reply@kivra.com>", "no-reply@kivra.com", "kivra.com", is_read=True),
        ExtractedEmailInfo(
            unsub_target=UnsubTarget("https_link", "https://kivra.com/settings", "header", "List-Unsubscribe", 0.9),
            newsletter_signals=["List-Unsubscribe"],
        ),
        cfg,
        "Archive",
        catchup_mode=True,
    )
    assert kivra_catchup.category == "attention"
    assert kivra_catchup.default_action == "no_action"

    gh_human_catchup = classify_email(
        _hdr("Re: Fix crash", "Alice <notifications@github.com>", "notifications@github.com", "github.com", is_read=True),
        ExtractedEmailInfo(
            unsub_target=UnsubTarget("https_link", "https://github.com/unsub", "header", "List-Unsubscribe", 0.9),
            newsletter_signals=["List-Unsubscribe"],
        ),
        cfg,
        "Archive",
        catchup_mode=True,
    )
    assert gh_human_catchup.category == "updates"
    assert gh_human_catchup.default_action == "no_action"

    # Explicit keep_subscribed + preferred_action must take precedence even if unsubscribed_at is still populated
    mem_explicit_over_unsub = SenderMemoryRecord(
        sender_key="news@brand.se",
        display_name="Brand",
        preferred_action="mark_read",
        keep_subscribed=True,
        unsubscribed_at=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
        unsub_count=1,
        last_seen_at=datetime.now(timezone.utc).isoformat(),
    )
    overridden = classify_email(
        _hdr("Weekly Digest", "Brand <news@brand.se>", "news@brand.se", "brand.se"),
        ExtractedEmailInfo(
            unsub_target=UnsubTarget("https_link", "https://brand.se/unsub", "header", "List-Unsubscribe", 0.9),
            newsletter_signals=["List-Unsubscribe"],
        ),
        cfg,
        "Archive",
        sender_mem=mem_explicit_over_unsub,
    )
    assert overridden.default_action == "mark_read"
    assert "Learned: mark_read" in overridden.badges


def test_default_config_is_neutral_and_validates_gmail_strategy(tmp_path: Path, monkeypatch):
    import pytest

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    cfg = load_config(ensure_files=True)
    assert cfg.display_name == ""
    assert cfg.gmail_archive_strategy == "ui_menu"
    assert cfg.consulting_enabled is False
    assert cfg.enabled_packs == []
    assert cfg.wanted_domains == []
    assert cfg.consulting_networks == []

    # With consulting disabled by default, consulting emails do not route to consulting category
    non_consult = classify_email(
        _hdr("Consultation Request", "RM <rm@guidepointglobal.com>", "rm@guidepointglobal.com", "guidepointglobal.com"),
        ExtractedEmailInfo(consulting_links=ConsultingLinks(review_url="https://gp.test/rev", decline_url="https://gp.test/dec")),
        cfg,
        "Archive",
    )
    assert non_consult.category != "consulting"

    # Without the "se" pack, Swedish-only delivery phrases do not trigger Swedish transactional rule
    swedish_tx = classify_email(
        _hdr("Ditt paket", "Shop <hello@shop.example>", "hello@shop.example", "shop.example"),
        ExtractedEmailInfo(plain_excerpt="Din beställning är packad och skickas nu."),
        cfg,
        "Archive",
    )
    assert swedish_tx.subcategory != "Transactional"

    # Enabling "se" pack activates Swedish delivery and Kivra rules
    cfg_path.write_text('[rules]\nenabled_packs = ["se"]\n', encoding="utf-8")
    se_cfg = load_config(ensure_files=False)
    assert se_cfg.enabled_packs == ["se"]
    se_tx = classify_email(
        _hdr("Ditt paket", "Shop <hello@shop.example>", "hello@shop.example", "shop.example"),
        ExtractedEmailInfo(plain_excerpt="Din beställning är packad och skickas nu."),
        se_cfg,
        "Archive",
    )
    assert se_tx.subcategory == "Transactional"

    cfg_path.write_text('[rules]\nenabled_packs = ["nonexistent"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown rule pack"):
        load_config(ensure_files=False)

    cfg_path.write_text('[accounts]\ngmail_archive_strategy = "invalid_mode"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="gmail_archive_strategy"):
        load_config(ensure_files=False)
