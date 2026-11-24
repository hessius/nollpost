"""Deterministic email classifier for nollpost."""

from __future__ import annotations

import re
from typing import Optional

from nollpost.config import TriageConfig
from nollpost.models import (
    ActionType,
    ClassifiedItem,
    ExtractedEmailInfo,
    MailHeader,
)
from nollpost.packs import resolve_packs
from nollpost.store import SenderMemoryRecord, days_since_iso, hours_since_iso

SECURITY_KEYWORDS_RE = re.compile(
    r"\b(?:security\s+alert|app-specific\s+password|verification\s+code|sign-in|two-factor|2fa)\b",
    re.I,
)

COMMENT_REPLY_RE = re.compile(
    r"\b(?:replied\s+to|svarade\s+på|commented\s+on|kommenterade|mentioned\s+you|nämnde\s+dig)\b",
    re.I,
)

GITHUB_BOT_SUBJECT_RE = re.compile(
    r"\b(?:deployment\s+(?:failed|succeeded)|workflow\s+run|dependabot|release\s+please|chore\(deps\))\b",
    re.I,
)

NOREPLY_RE = re.compile(
    r"(?:no[-_.]?reply|donotreply|do[-_.]?not[-_.]?reply|notifications?@|mailer-daemon|postmaster@|newsletter|nyheter|info@|news@|hello@|shop@|support@|marketing@)",
    re.I,
)

STRONG_RETAIL_PROMO_RE = re.compile(
    r"\b(?:black\s+(?:week|friday)|% off|discount\s+code)\b",
    re.I,
)

CRITICAL_BILL_TERMS_RE = re.compile(
    r"\b(?:invoice|due\s+date|payout|payment\s+reminder|past\s+due|billing)\b",
    re.I,
)

BASE_SECURITY_DOMAINS = ("accounts.google.com", "email.apple.com", "id.apple.com")
BASE_BILL_SENDERS = ("accounts.google.com", "email.apple.com")
BASE_TRANSACTIONAL_RE = re.compile(r"\b(?:tracking\s+number|order\s+shipped|out\s+for\s+delivery)\b", re.I)


def _domain_matches(domain: str, candidates: list[str] | tuple[str, ...]) -> bool:
    dom = domain.lower().strip()
    if not dom:
        return False
    return any(dom == c or dom.endswith("." + c) for c in candidates)


def _contains_any_keyword(text: str, keywords: list[str] | tuple[str, ...]) -> Optional[str]:
    for kw in keywords:
        if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text, re.I):
            return kw
    return None


def _base_allowed_actions(
    header: MailHeader,
    extracted: ExtractedEmailInfo,
    category: str,
    catchup_mode: bool = False,
) -> list[ActionType]:
    if catchup_mode:
        actions: list[ActionType] = ["no_action"]
        if category == "newsletters" and extracted.unsub_target is not None:
            actions.append("unsub_only")
        return actions

    actions = ["no_action", "mark_read"]
    if extracted.pdf_attachments:
        actions.append("read_show_invoice")
    if extracted.pertinent_url:
        actions.append("read_open_link")

    if category == "consulting":
        if extracted.consulting_links.decline_url:
            actions.append("decline_and_archive")
        else:
            actions.append("draft_decline")
            actions.append("send_decline_and_archive")

    actions.append("archive")
    if extracted.unsub_target is not None:
        actions.append("archive_and_unsub")
    return actions


def classify_email(
    header: MailHeader,
    extracted: ExtractedEmailInfo,
    config: TriageConfig,
    archive_mailbox: str,
    sender_mem: Optional[SenderMemoryRecord] = None,
    catchup_mode: bool = False,
) -> ClassifiedItem:
    """Classify one email into category, default action, allowed actions, and badges."""
    uid = f"{header.account}:{header.mailbox}:{header.internal_id}"
    sender_domain_low = header.sender_domain.lower()
    sender_email_low = header.sender_email.lower()
    combined = f"{header.subject}\n{header.sender}\n{extracted.plain_excerpt}".lower()
    badges: list[str] = []
    learned_pref: Optional[str] = None
    repeat_days: Optional[int] = None

    packs = resolve_packs(config.enabled_packs)
    has_unsub = extracted.unsub_target is not None
    looks_like_newsletter = bool(extracted.newsletter_signals)

    # 1. Explicit "always_keep" memory override
    if sender_mem and sender_mem.preferred_action == "always_keep":
        allowed = _base_allowed_actions(header, extracted, "updates", catchup_mode=catchup_mode)
        default_act: ActionType = (
            "no_action"
            if (header.is_read or catchup_mode)
            else ("read_open_link" if extracted.pertinent_url else "no_action")
        )
        badges.append("Pinned Keep")
        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="updates",
            subcategory="Pinned Sender",
            default_action=default_act,
            allowed_actions=allowed,
            badges=badges,
            archive_mailbox=archive_mailbox,
            learned_preference="always_keep",
            reason="Explicitly marked always keep",
        )

    # Pre-check security alerts & Gov/Kivra notices before consulting outreach so 2FA/login alerts are never auto-declined
    is_security = bool(
        SECURITY_KEYWORDS_RE.search(combined)
        or _domain_matches(header.sender_domain, BASE_SECURITY_DOMAINS)
        or any(p.security_text_re and p.security_text_re.search(combined) for p in packs)
    )
    matched_gov_pack = next(
        (
            p
            for p in packs
            if p.gov_domain_token
            and (p.gov_domain_token in combined or p.gov_domain_token in sender_domain_low)
        ),
        None,
    )
    if is_security or matched_gov_pack is not None:
        allowed = _base_allowed_actions(header, extracted, "attention", catchup_mode=catchup_mode)
        if is_security:
            subcat = "Security Alert"
            badges.append("Security")
        else:
            assert matched_gov_pack is not None
            subcat = matched_gov_pack.gov_subcategory
            badges.append(matched_gov_pack.gov_badge)
        if catchup_mode or header.is_read:
            default_act = "no_action"
            if header.is_read and not catchup_mode:
                badges.append("Already read")
        elif extracted.pdf_attachments:
            default_act = "read_show_invoice"
        elif extracted.pertinent_url:
            default_act = "read_open_link"
        else:
            default_act = "no_action"
        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="attention",
            subcategory=subcat,
            default_action=default_act,
            allowed_actions=allowed,
            badges=badges,
            archive_mailbox=archive_mailbox,
            reason=f"Matched {'security alert' if is_security else 'government/digital mail'}",
        )

    # 2. Consulting outreach (opt-in via [consulting] enabled = true or configured networks)
    is_consulting = config.consulting_enabled and (
        _domain_matches(header.sender_domain, config.consulting_networks)
        or bool(extracted.consulting_links.decline_url or extracted.consulting_links.review_url)
    )
    if is_consulting:
        pos_kw = _contains_any_keyword(combined, config.consulting_match_keywords)
        neg_kw = _contains_any_keyword(combined, config.consulting_negative_keywords)
        allowed = _base_allowed_actions(header, extracted, "consulting", catchup_mode=catchup_mode)

        if pos_kw and not neg_kw:
            badges.append(f"Expertise Match: {pos_kw}")
            default_act = (
                "no_action"
                if (header.is_read or catchup_mode)
                else ("read_open_link" if extracted.pertinent_url else "no_action")
            )
            subcat = "Consulting Match"
            reason = f"Matches expertise keyword '{pos_kw}'"
        elif pos_kw and neg_kw:
            badges.append(f"Review Match: {pos_kw}")
            default_act = (
                "no_action"
                if (header.is_read or catchup_mode)
                else ("read_open_link" if extracted.pertinent_url else "no_action")
            )
            subcat = "Consulting Match"
            reason = f"Mentions '{pos_kw}' alongside '{neg_kw}'; review manually"
        else:
            mismatch_lbl = f"Mismatch: {neg_kw}" if neg_kw else "Mismatch: Outside Expertise"
            badges.append(mismatch_lbl)
            if catchup_mode or header.is_read:
                default_act = "no_action"
            elif extracted.consulting_links.decline_url:
                default_act = "decline_and_archive"
            else:
                default_act = "draft_decline"
            reason = (
                "Consulting request outside configured expertise with clickable Decline button"
                if extracted.consulting_links.decline_url
                else "Consulting request outside configured expertise (no Decline button; draft boilerplate ready)"
            )
            subcat = "Consulting Mismatch"

        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="consulting",
            subcategory=subcat,
            default_action=default_act,
            allowed_actions=allowed,
            badges=badges,
            archive_mailbox=archive_mailbox,
            reason=reason,
        )

    # 3. Bills, Invoices, Gov / Digital Mail, Security Alerts (protected even if ESP sends with List-Unsubscribe!)
    effective_bill_keywords = list(config.bill_keywords)
    explicit_bill_senders = list(BASE_BILL_SENDERS)
    for pack in packs:
        effective_bill_keywords.extend(pack.bill_keywords)
        explicit_bill_senders.extend(pack.explicit_bill_senders)

    bill_kw = _contains_any_keyword(combined, effective_bill_keywords)
    is_explicit_bill_sender = any(
        k in sender_domain_low or k in sender_email_low for k in explicit_bill_senders
    )
    has_critical_bill = bool(
        CRITICAL_BILL_TERMS_RE.search(combined)
        or any(p.critical_bill_terms_re and p.critical_bill_terms_re.search(combined) for p in packs)
    )
    has_retail_promo = bool(
        STRONG_RETAIL_PROMO_RE.search(combined)
        or any(p.retail_promo_re and p.retail_promo_re.search(combined) for p in packs)
    )
    is_retail_promo_noise = has_retail_promo and not has_critical_bill and not extracted.pdf_attachments

    # 3a. Critical safety guardrails in Attention (PDF invoices)
    is_protected_pdf = bool(extracted.pdf_attachments) and config.auto_skip_pdf_invoices
    is_auto_archive_pref = bool(sender_mem and sender_mem.preferred_action == "auto_archive")
    is_always_archive_override = bool(
        config.auto_archive_always_archive and sender_mem and sender_mem.preferred_action == "archive"
    )

    if (is_auto_archive_pref or is_always_archive_override) and not is_protected_pdf:
        allowed = _base_allowed_actions(header, extracted, "newsletters", catchup_mode=catchup_mode)
        default_act = "no_action" if (header.is_read or catchup_mode) else "archive"
        subcat = "Auto-Archive" if is_auto_archive_pref else "Remembered Archive"
        badges.append("Auto-Archive" if is_auto_archive_pref else "Learned: archive")
        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="newsletters",
            subcategory=subcat,
            default_action=default_act,
            allowed_actions=allowed,
            badges=badges,
            archive_mailbox=archive_mailbox,
            learned_preference=sender_mem.preferred_action,
            reason=(
                "Explicitly marked Auto (Archive & Summarize)"
                if is_auto_archive_pref
                else "Remembered Always Archive sender (autopilot enabled)"
            ),
        )

    if is_explicit_bill_sender or extracted.pdf_attachments or is_security or (bill_kw and not is_retail_promo_noise):
        allowed = _base_allowed_actions(header, extracted, "attention", catchup_mode=catchup_mode)
        if is_security:
            subcat = "Security Alert"
            badges.append("Security")
        elif matched_gov_pack is not None:
            subcat = matched_gov_pack.gov_subcategory
            badges.append(matched_gov_pack.gov_badge)
        else:
            subcat = "Bill / Invoice"
            badges.append("Invoice" if extracted.pdf_attachments else "Billing / Admin")

        if catchup_mode:
            default_act = "no_action"
        elif header.is_read:
            default_act = "no_action"
            badges.append("Already read")
        elif extracted.pdf_attachments:
            default_act = "read_show_invoice"
        elif extracted.pertinent_url:
            default_act = "read_open_link"
        else:
            default_act = "no_action"

        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="attention",
            subcategory=subcat,
            default_action=default_act,
            allowed_actions=allowed,
            badges=badges,
            archive_mailbox=archive_mailbox,
            reason=f"Matched {'security alert' if is_security else (bill_kw or 'PDF attachment')}",
        )

    # 4. GitHub (Human vs Bot)
    if header.sender_domain == "github.com":
        sender_low = header.sender.lower()
        is_bot = any(bot in sender_low for bot in config.bot_senders) or bool(
            GITHUB_BOT_SUBJECT_RE.search(header.subject)
        )
        if is_bot:
            allowed = _base_allowed_actions(header, extracted, "newsletters", catchup_mode=catchup_mode)
            default_act = (
                "no_action"
                if (header.is_read or catchup_mode)
                else "archive"
            )
            badges.append("GitHub Bot")
            return ClassifiedItem(
                uid=uid,
                header=header,
                extracted=extracted,
                category="newsletters",
                subcategory="Bot Noise",
                default_action=default_act,
                allowed_actions=allowed,
                badges=badges,
                archive_mailbox=archive_mailbox,
                reason="Automated GitHub bot/deployment notification",
            )
        allowed = _base_allowed_actions(header, extracted, "updates", catchup_mode=catchup_mode)
        default_act = (
            "no_action"
            if (header.is_read or catchup_mode)
            else ("read_open_link" if extracted.pertinent_url else "no_action")
        )
        badges.append("GitHub Human")
        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="updates",
            subcategory="GitHub Human",
            default_action=default_act,
            allowed_actions=allowed,
            badges=badges,
            archive_mailbox=archive_mailbox,
            reason="GitHub notification from human contributor",
        )

    # 5. Wanted Creator / Project Updates & Comment Replies
    is_wanted_domain = _domain_matches(header.sender_domain, config.wanted_domains)
    if is_wanted_domain:
        allowed = _base_allowed_actions(header, extracted, "updates", catchup_mode=catchup_mode)
        is_comment = bool(COMMENT_REPLY_RE.search(header.subject))
        subcat = "Comment Reply" if is_comment else "Creator Update"
        badges.append("Comment" if is_comment else "Wanted Update")
        if catchup_mode:
            default_act = "no_action"
        elif header.is_read:
            default_act = "no_action"
            badges.append("Already read")
        elif extracted.pertinent_url:
            default_act = "read_open_link"
        else:
            default_act = "mark_read"
        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="updates",
            subcategory=subcat,
            default_action=default_act,
            allowed_actions=allowed,
            badges=badges,
            archive_mailbox=archive_mailbox,
            reason="Matched wanted domain rule",
        )

    # 6. Transactional Delivery / Order Notifications (archive only, never auto-unsub)
    effective_tx_domains = list(config.transactional_domains)
    for pack in packs:
        effective_tx_domains.extend(pack.transactional_domains)
    is_transactional = (
        _domain_matches(header.sender_domain, effective_tx_domains)
        or bool(BASE_TRANSACTIONAL_RE.search(combined))
        or any(p.transactional_text_re and p.transactional_text_re.search(combined) for p in packs)
    )
    if is_transactional:
        allowed = _base_allowed_actions(header, extracted, "newsletters", catchup_mode=catchup_mode)
        badges.append("Transactional")
        default_act = "no_action" if (header.is_read or catchup_mode) else "archive"
        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="newsletters",
            subcategory="Transactional",
            default_action=default_act,
            allowed_actions=allowed,
            badges=badges,
            archive_mailbox=archive_mailbox,
            reason="Transactional order/delivery notification (Archive only)",
        )

    # 7. Newsletters & Promotions
    if looks_like_newsletter:
        allowed = _base_allowed_actions(header, extracted, "newsletters", catchup_mode=catchup_mode)
        no_unsub = not has_unsub

        if sender_mem and sender_mem.keep_subscribed and sender_mem.preferred_action:
            learned_pref = sender_mem.preferred_action
            badges.append(f"Learned: {learned_pref}")
            if catchup_mode or header.is_read:
                default_act = "no_action"
            elif learned_pref in allowed:
                default_act = learned_pref  # type: ignore[assignment]
            else:
                default_act = "archive"
        elif sender_mem and sender_mem.unsubscribed_at:
            hrs = hours_since_iso(sender_mem.unsubscribed_at)
            days = days_since_iso(sender_mem.unsubscribed_at) or 0
            if hrs is not None and hrs <= 48.0:
                badges.append("Unsubbed recently")
                default_act = "no_action" if (header.is_read or catchup_mode) else "archive"
            else:
                repeat_days = days
                badges.append(f"Sent after unsub ({days}d ago)")
                if catchup_mode:
                    default_act = "unsub_only" if has_unsub else "no_action"
                elif header.is_read:
                    default_act = "no_action"
                else:
                    default_act = "archive_and_unsub" if has_unsub else "archive"
        else:
            if no_unsub:
                badges.append("No unsub found")
            if catchup_mode:
                default_act = "unsub_only" if has_unsub else "no_action"
            elif header.is_read:
                default_act = "no_action"
                badges.append("Already read")
            else:
                default_act = "archive_and_unsub" if has_unsub else "archive"

        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="newsletters",
            subcategory="Newsletter",
            default_action=default_act,
            allowed_actions=allowed,
            badges=badges,
            no_unsub_found=no_unsub,
            repeat_offender_days=repeat_days,
            learned_preference=learned_pref,
            archive_mailbox=archive_mailbox,
            reason="Newsletter/promo signals detected"
            + ("" if has_unsub else " (no reliable unsubscribe target extracted)"),
        )

    # 8. Catch-all in Archive Catch-Up mode (skip non-newsletters)
    if catchup_mode:
        allowed = _base_allowed_actions(header, extracted, "newsletters", catchup_mode=True)
        return ClassifiedItem(
            uid=uid,
            header=header,
            extracted=extracted,
            category="newsletters",
            subcategory="Non-Newsletter",
            default_action="no_action",
            allowed_actions=["no_action"],
            archive_mailbox=archive_mailbox,
            reason="No newsletter signals in Archive Catch-Up",
        )

    # 9. Direct Human vs Unrecognized Automated (conservative No Action fallback!)
    is_automated = bool(NOREPLY_RE.search(header.sender_email) or extracted.newsletter_signals)
    allowed = _base_allowed_actions(header, extracted, "attention", catchup_mode=False)
    if is_automated:
        badges.append("Unclassified Automated")
        subcat = "Unclassified Automated"
        reason = "Automated sender without clear promo/unsub or invoice signals; kept safe as No action"
    else:
        badges.append("Human")
        subcat = "Direct Human"
        reason = "Likely human email"

    return ClassifiedItem(
        uid=uid,
        header=header,
        extracted=extracted,
        category="attention",
        subcategory=subcat,
        default_action="no_action",
        allowed_actions=allowed,
        badges=badges,
        archive_mailbox=archive_mailbox,
        reason=reason,
    )
