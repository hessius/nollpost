"""Shared domain models for nollpost."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

ActionType = Literal[
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
]

CategoryType = Literal["attention", "updates", "consulting", "newsletters"]


@dataclass
class AccountInfo:
    """Resolved Apple Mail account and localized mailbox names."""

    name: str
    addresses: list[str]
    inbox_mailbox: str
    archive_mailbox: str
    sent_mailbox: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MailHeader:
    """Metadata header returned by batched Apple Mail scan."""

    account: str
    mailbox: str
    internal_id: str
    rfc_message_id: str
    subject: str
    sender: str
    sender_email: str
    sender_domain: str
    date_received: str
    is_read: bool
    is_flagged: bool
    attachment_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UnsubTarget:
    """Reliably extracted unsubscribe endpoint."""

    kind: Literal["rfc8058_one_click", "https_link"]
    url: str
    source: Literal["header", "body"]
    matched_label: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConsultingLinks:
    """Review and one-click decline links parsed from consulting outreach."""

    review_url: Optional[str] = None
    decline_url: Optional[str] = None
    decline_label: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PdfAttachment:
    """Metadata for a PDF attachment extracted from MIME parts."""

    filename: str
    part_index: int
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractedEmailInfo:
    """Parsed MIME details for a single message."""

    unsub_target: Optional[UnsubTarget] = None
    fallback_unsub_url: Optional[str] = None
    mailto_unsub: Optional[str] = None
    newsletter_signals: list[str] = field(default_factory=list)
    consulting_links: ConsultingLinks = field(default_factory=ConsultingLinks)
    pertinent_url: Optional[str] = None
    pertinent_label: Optional[str] = None
    pdf_attachments: list[PdfAttachment] = field(default_factory=list)
    headers_snapshot: str = ""
    footer_html_snapshot: str = ""
    candidate_links: list[dict[str, str]] = field(default_factory=list)
    plain_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unsub_target": self.unsub_target.to_dict() if self.unsub_target else None,
            "fallback_unsub_url": self.fallback_unsub_url,
            "mailto_unsub": self.mailto_unsub,
            "newsletter_signals": list(self.newsletter_signals),
            "consulting_links": self.consulting_links.to_dict(),
            "pertinent_url": self.pertinent_url,
            "pertinent_label": self.pertinent_label,
            "pdf_attachments": [p.to_dict() for p in self.pdf_attachments],
            "candidate_links": list(self.candidate_links),
            "plain_excerpt": self.plain_excerpt,
        }


@dataclass
class ClassifiedItem:
    """Fully classified email ready for review board or CLI summary."""

    uid: str
    header: MailHeader
    extracted: ExtractedEmailInfo
    category: CategoryType
    subcategory: str
    default_action: ActionType
    allowed_actions: list[ActionType]
    badges: list[str] = field(default_factory=list)
    group_count: int = 1
    no_unsub_found: bool = False
    repeat_offender_days: Optional[int] = None
    learned_preference: Optional[str] = None
    archive_mailbox: str = "Archive"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "header": self.header.to_dict(),
            "extracted": self.extracted.to_dict(),
            "category": self.category,
            "subcategory": self.subcategory,
            "default_action": self.default_action,
            "allowed_actions": list(self.allowed_actions),
            "badges": list(self.badges),
            "group_count": self.group_count,
            "no_unsub_found": self.no_unsub_found,
            "repeat_offender_days": self.repeat_offender_days,
            "learned_preference": self.learned_preference,
            "archive_mailbox": self.archive_mailbox,
            "reason": self.reason,
        }


@dataclass
class PlaywrightOutcome:
    """Result from Playwright or RFC 8058 execution."""

    status: Literal["success", "blocked_captcha", "failed"]
    final_url: str
    detail: str
    screenshot_path: Optional[str] = None
    dom_snapshot: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionItemResult:
    """Result for one executed message action."""

    uid: str
    account: str
    internal_id: str
    sender: str
    subject: str
    action: str
    status: Literal["ok", "blocked_captcha", "failed", "drafted", "skipped"]
    detail: str
    opened_target: Optional[str] = None
    screenshot_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
