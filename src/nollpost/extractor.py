"""MIME parsing, multilingual unsubscribe link extraction, pertinent CTA ranking, and PDF invoices."""

from __future__ import annotations

import email
import html as html_lib
import re
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from nollpost.models import ConsultingLinks, ExtractedEmailInfo, PdfAttachment, UnsubTarget
from nollpost.packs import resolve_packs
from nollpost.patterns_loader import UnsubPatterns, load_unsub_patterns

ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")

_DEFAULT_PATTERNS = load_unsub_patterns()
STRONG_UNSUB_PATTERNS = list(_DEFAULT_PATTERNS.strong_unsub_patterns)
STRONG_UNSUB_RE = _DEFAULT_PATTERNS.strong_unsub_re
URL_UNSUB_HINT_RE = _DEFAULT_PATTERNS.url_unsub_hint_re

CLICK_HERE_RE = re.compile(
    r"^(?:här|klicka\s+här|tryck\s+här|here|click\s+here|this\s+link|länk|denna\s+länk)$",
    re.I,
)

UNSUB_NEGATIVE_LINK_RE = re.compile(
    r"(?:privacy|integritetspolicy|sekretess|terms|villkor|cookie|view\s+in\s+browser|visa\s+i\s+webbläsare|support|help\s+center)",
    re.I,
)

PERTINENT_NEGATIVE_RE = re.compile(
    r"(?:\b(?:privacy(?:\s+policy)?|integritetspolicy|sekretess|terms\s+(?:of\s+(?:use|service)|and\s+conditions)|allmänna\s+villkor|cookie[-_\s]+(?:policy|settings|notice|consent)|unsubscribe|avprenumerera|avregistrera|manage\s+(?:your\s+)?(?:email\s+)?(?:preferences|settings|subscriptions?)|email[-_]?settings|upgrade\s+to\s+unlock|google\s+play|app\s+store)\b|upgrade_link)",
    re.I,
)

BROWSER_OR_TOPIC_CTA_RE = re.compile(
    r"\b(?:view|open|read)\s+(?:this\s+(?:email|message)\s+)?(?:in\s+(?:your\s+)?browser|online)\b|\b(?:web\s+version|visa\s+i\s+webbläsare[n]?|öppna\s+i\s+webbläsare[n]?|visit\s+topic|view\s+topic|view\s+thread|view\s+post|read\s+post|open\s+post|view\s+reply|se\s+svar|watch\s+now)\b",
    re.I,
)

CANONICAL_PATH_RE = re.compile(
    r"(?:^|/)(?:posts?|p|blogs?|updates?|topics?|t|threads?|articles?|newsletters?|c/projects|projects|inbox)(?:/|$)",
    re.I,
)

PROFILE_OR_NOISE_PATH_RE = re.compile(
    r"(?:^|/)(?:u|user|users|profile|member|pages/links)(?:/|$)",
    re.I,
)

SUBJECT_STOPWORDS = frozenset(
    {
        "re",
        "fw",
        "fwd",
        "the",
        "and",
        "for",
        "from",
        "with",
        "your",
        "this",
        "that",
        "have",
        "new",
        "issue",
        "update",
        "just",
        "launched",
        "och",
        "för",
        "från",
        "med",
        "din",
        "ditt",
        "nya",
        "nytt",
        "till",
    }
)

SOCIAL_DOMAINS = (
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
)

ESP_HEADER_MARKERS = (
    ("x-kmail-", "ESP:klaviyo"),
    ("x-mailer", "X-Mailer"),
    ("x-mailgun-", "ESP:mailgun"),
    ("x-br-message", "ESP:bloomreach"),
    ("x-mc-user", "ESP:mailchimp"),
    ("x-sg-eid", "ESP:sendgrid"),
    ("x-hs-", "ESP:hubspot"),
    ("x-campaign", "X-Campaign"),
)

PROMO_TEXT_RE = re.compile(
    r"\b(?:nyhetsbrev|newsletter|black\s+(?:week|friday)|rabatt|erbjudande|fynd|sale|discount|% off|avprenumerera|avregistrera|unsubscribe)\b",
    re.I,
)


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", ZERO_WIDTH_RE.sub("", html_lib.unescape(s))).strip()


def _safe_decode(payload: bytes, charset: Optional[str]) -> str:
    """Decode bytes with a tolerant fallback when MIME declares an unknown encoding."""
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _decode_mime_header_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    chunks = decode_header(raw_text)
    out = []
    for part, charset in chunks:
        if isinstance(part, bytes):
            out.append(_safe_decode(part, charset))
        else:
            out.append(str(part))
    return "".join(out).strip()


def _decode_mime_filename(raw_name: str) -> str:
    decoded = unquote(_decode_mime_header_text(raw_name)).strip()
    return decoded or "attachment.pdf"


def sanitize_filename(name: str) -> str:
    base = Path(name).name
    cleaned = re.sub(r"[^\w.\- åäöÅÄÖ&]+", "_", base).strip("._")
    if not cleaned:
        cleaned = "invoice.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


def _parse_list_unsubscribe_header(value: str) -> tuple[Optional[str], Optional[str]]:
    """Return (https_url, mailto_url) from a List-Unsubscribe header."""
    https_url: Optional[str] = None
    mailto_url: Optional[str] = None
    for match in re.finditer(r"<([^>]+)>", value):
        uri = match.group(1).strip()
        low = uri.lower()
        if (low.startswith("https://") or low.startswith("http://")) and not https_url:
            https_url = uri
        elif low.startswith("mailto:") and not mailto_url:
            mailto_url = uri
    if not https_url and not mailto_url:
        stripped = value.strip()
        if stripped.lower().startswith(("https://", "http://")):
            https_url = stripped
        elif stripped.lower().startswith("mailto:"):
            mailto_url = stripped
    return https_url, mailto_url


def _extract_bodies_and_pdfs(msg: EmailMessage) -> tuple[str, str, list[PdfAttachment]]:
    html_parts: list[str] = []
    plain_parts: list[str] = []
    pdfs: list[PdfAttachment] = []

    for idx, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        disp = (part.get_content_disposition() or "").lower()
        raw_fname = part.get_filename()
        filename = _decode_mime_filename(raw_fname) if raw_fname else ""

        if ctype == "application/pdf" or filename.lower().endswith(".pdf"):
            payload = part.get_payload(decode=True) or b""
            pdfs.append(
                PdfAttachment(
                    filename=filename or f"attachment-{idx}.pdf",
                    part_index=idx,
                    size_bytes=len(payload),
                )
            )
            continue

        if disp == "attachment":
            continue

        try:
            content = part.get_content()
            if isinstance(content, bytes):
                content = _safe_decode(content, part.get_content_charset())
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = _safe_decode(payload, part.get_content_charset())

        if ctype == "text/html":
            html_parts.append(str(content))
        elif ctype == "text/plain":
            plain_parts.append(str(content))

    return "\n".join(html_parts), "\n".join(plain_parts), pdfs


def _is_social_or_noise_url(url: str) -> bool:
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return True
    parsed = urlparse(low)
    host = parsed.netloc
    if any(host == dom or host.endswith("." + dom) for dom in SOCIAL_DOMAINS):
        return True
    if any(tok in low for tok in ("pixel", "open.gif", "/wf/open", "/o?", "beacon", "unsubscribe_pixel")):
        return True
    return False


def _score_unsub_anchor(
    anchor_text: str,
    aria: str,
    parent_text: str,
    href: str,
    patterns: UnsubPatterns | None = None,
) -> tuple[float, str]:
    pats = patterns or _DEFAULT_PATTERNS
    low_href = href.lower()
    if not low_href.startswith(("http://", "https://")):
        return -100.0, ""
    parsed = urlparse(low_href)
    if any(parsed.netloc == dom or parsed.netloc.endswith("." + dom) for dom in SOCIAL_DOMAINS):
        return -100.0, ""

    label = _clean_text(anchor_text or aria)
    if UNSUB_NEGATIVE_LINK_RE.search(label) and not pats.strong_unsub_re.search(label):
        return -50.0, label

    score = 0.0
    if label and pats.strong_unsub_re.search(label):
        score += 10.0
    elif label and CLICK_HERE_RE.match(label) and parent_text and pats.strong_unsub_re.search(parent_text):
        score += 8.0
    elif not label and parent_text and pats.strong_unsub_re.search(parent_text):
        score += 6.0

    if pats.url_unsub_hint_re.search(low_href):
        score += 4.0

    if UNSUB_NEGATIVE_LINK_RE.search(low_href) and not pats.strong_unsub_re.search(label):
        score -= 8.0

    return score, label or _clean_text(parent_text)[:80]


def _extract_consulting_links(anchors: list[dict[str, str]], combined_text: str, sender_domain: str) -> ConsultingLinks:
    consulting_hint = any(
        tok in sender_domain or tok in combined_text.lower()
        for tok in ("guidepoint", "coleman", "visasq", "glg", "alphasights", "thirdbridge", "dialectica", "atheneum", "consultation request", "consulting request")
    )
    if not consulting_hint:
        return ConsultingLinks()

    review_url = None
    decline_url = None
    decline_label = None
    for a in anchors:
        href = a["href"]
        text = a["text"]
        if not href.lower().startswith(("http://", "https://")):
            continue
        low = text.lower().strip()
        if not decline_url and (
            re.search(r"\b(?:decline\s+now|decline\s+the\s+project|not\s+interested|pass\s+on)\b", low)
            or low == "decline"
            or "decline" in href.lower()
        ):
            decline_url = href
            decline_label = text or "Decline"
        elif not review_url and re.search(
            r"\b(?:review\s+project|complete\s+questionnaire|view\s+project|review\s+request|accept)\b",
            low,
        ):
            review_url = href
    return ConsultingLinks(review_url=review_url, decline_url=decline_url, decline_label=decline_label)


def _unwrap_tracking_url(href: str) -> tuple[str, str, str]:
    """Return (decoded_full_lower, effective_host, effective_path) inspecting wrapped click-tracker URLs."""
    decoded = unquote(href.strip())
    low_decoded = decoded.lower()
    outer = urlparse(low_decoded)
    host = outer.netloc
    path = outer.path or ""

    embedded = re.search(r"https?://[^\s<>\"']+", decoded[8:], re.I)
    if embedded:
        inner = urlparse(embedded.group(0).lower())
        if inner.netloc:
            host = inner.netloc
            path = inner.path or ""
    elif host.startswith("track.pstmrk.it"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and "." in parts[1]:
            wrapped = urlparse("https://" + parts[1])
            host = wrapped.netloc
            path = wrapped.path or ""
    return low_decoded, host, path


def _registrable_root(host: str) -> str:
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _extract_pertinent_url(
    anchors: list[dict[str, str]],
    unsub_url: Optional[str],
    consulting: ConsultingLinks,
    sender_domain: str,
    subject: str = "",
    wanted_domains: Sequence[str] = (),
    enabled_packs: Sequence[str] = (),
    patterns: UnsubPatterns | None = None,
) -> tuple[Optional[str], Optional[str]]:
    if consulting.review_url:
        return consulting.review_url, "Review consulting project"

    pats = patterns or _DEFAULT_PATTERNS

    packs = resolve_packs(enabled_packs)
    subj_tokens = [
        tok
        for tok in re.findall(r"[a-z0-9åäö]{4,}", subject.lower())
        if tok not in SUBJECT_STOPWORDS
    ]
    sender_domain_low = sender_domain.lower().strip()
    sender_root = _registrable_root(sender_domain_low)
    wanted_lower = tuple(d.lower().strip() for d in wanted_domains if d.strip())
    wanted_roots = tuple(_registrable_root(d) for d in wanted_lower)

    best_score = -1.0
    best_url: Optional[str] = None
    best_label: Optional[str] = None

    for a in anchors:
        href = a["href"]
        text = a["text"]
        if _is_social_or_noise_url(href):
            continue
        if unsub_url and href == unsub_url:
            continue
        if consulting.decline_url and href == consulting.decline_url:
            continue

        decoded_full, dest_host, dest_path = _unwrap_tracking_url(href)
        low_text = text.lower()
        if pats.url_unsub_hint_re.search(decoded_full) or pats.strong_unsub_re.search(text):
            continue
        if PERTINENT_NEGATIVE_RE.search(text) or PERTINENT_NEGATIVE_RE.search(decoded_full):
            continue

        score = 1.0
        dest_root = _registrable_root(dest_host)
        same_brand_host = bool(sender_root and dest_root == sender_root)

        for pack in packs:
            if pack.gov_domain_token and pack.gov_domain_token in sender_domain_low:
                if pack.gov_app_host and pack.gov_app_host in decoded_full:
                    score += 8.0
                elif pack.gov_domain_token in dest_host:
                    score += 3.0
                if pack.gov_cta_re and pack.gov_cta_re.search(low_text):
                    score += 8.0
                if pack.gov_noise_path_re and pack.gov_noise_path_re.search(dest_path):
                    score -= 5.0

        if "github.com" in decoded_full and re.search(r"/(?:pull|issues|discussions|commit)/", dest_path):
            score += 12.0

        if BROWSER_OR_TOPIC_CTA_RE.search(low_text):
            score += 14.0

        if CANONICAL_PATH_RE.search(dest_path):
            score += 10.0
            if re.search(r"/(?:blogs?|updates?|posts?|t)/", dest_path):
                score += 2.0

        if same_brand_host or any(
            dest_host == d or dest_host.endswith("." + d) or dest_root == wr
            for d, wr in zip(wanted_lower, wanted_roots)
        ):
            score += 4.0

        if subj_tokens:
            hay = f"{low_text} {dest_path}"
            hits = sum(1 for tok in dict.fromkeys(subj_tokens) if tok in hay)
            if hits:
                score += min(10.0, hits * 3.5)

        if re.search(r"\b(?:faktura|invoice|betala|pay|se\s+faktura|view\s+invoice|aktivera|activate|öppna|visa|open)\b", low_text):
            score += 6.0
        elif re.search(r"\breview\b", low_text) and len(text) <= 32 and not re.search(r"\ba\s+review\b", low_text):
            score += 5.0

        # Penalize profile pages, app deep links, empty homepages, and community/discord link hubs
        if PROFILE_OR_NOISE_PATH_RE.search(dest_path) or "creator_link" in decoded_full or "join the server" in low_text:
            score -= 10.0
        if dest_path in ("", "/"):
            score -= 5.0
        if dest_host.startswith("open.") or "view in app" in low_text:
            score -= 7.0

        if len(text) >= 4:
            score += 1.0

        if score > best_score:
            best_score = score
            best_url = href
            best_label = text[:70] if text else dest_host

    return best_url, best_label


def extract_email_info(
    raw_source: bytes,
    sender_domain: str = "",
    *,
    consulting_enabled: bool = True,
    wanted_domains: Sequence[str] = (),
    enabled_packs: Sequence[str] = (),
    patterns: UnsubPatterns | None = None,
) -> ExtractedEmailInfo:
    """Parse raw RFC 822 bytes into unsubscribe targets, consulting links, pertinent link, and PDFs."""
    pats = patterns or _DEFAULT_PATTERNS
    msg = email.message_from_bytes(raw_source, policy=policy.default)

    list_unsub_raw = " ".join(msg.get_all("List-Unsubscribe", []))
    list_unsub_post_raw = " ".join(msg.get_all("List-Unsubscribe-Post", []))
    header_https, mailto_unsub = _parse_list_unsubscribe_header(list_unsub_raw)
    has_one_click = "list-unsubscribe=one-click" in list_unsub_post_raw.lower()

    signals: list[str] = []
    if list_unsub_raw:
        signals.append("List-Unsubscribe")
    if msg.get("List-Id"):
        signals.append("List-Id")
    prec = str(msg.get("Precedence", "")).lower()
    if prec in ("bulk", "list", "junk"):
        signals.append(f"Precedence:{prec}")
    for hdr in msg.keys():
        low_hdr = hdr.lower()
        for prefix, label in ESP_HEADER_MARKERS:
            if low_hdr.startswith(prefix) and label not in signals:
                signals.append(label)

    html_body, plain_body, pdfs = _extract_bodies_and_pdfs(msg)

    anchors: list[dict[str, str]] = []
    best_body_unsub: Optional[UnsubTarget] = None
    best_body_score = 0.0
    footer_snapshot = ""

    if html_body:
        soup = BeautifulSoup(html_body, "html.parser")
        for a in soup.find_all("a", href=True):
            href = str(a["href"]).strip()
            if not href.lower().startswith(("http://", "https://")):
                continue
            anchor_text = _clean_text(a.get_text(" ", strip=True))
            aria = _clean_text(str(a.get("aria-label") or a.get("title") or ""))
            parent_text = _clean_text(a.parent.get_text(" ", strip=True))[:220] if a.parent else ""
            anchors.append({"href": href, "text": anchor_text or aria})
            score, matched_lbl = _score_unsub_anchor(anchor_text, aria, parent_text, href, patterns=pats)
            if score >= 7.0 and score > best_body_score:
                best_body_score = score
                best_body_unsub = UnsubTarget(
                    kind="https_link",
                    url=href,
                    source="body",
                    matched_label=matched_lbl or "Unsubscribe link",
                    confidence=min(0.95, 0.55 + score * 0.03),
                )
        footer_snapshot = html_body[-4000:]
        combined_text = _clean_text(soup.get_text(" ", strip=True))
    else:
        combined_text = _clean_text(plain_body)

    if plain_body and best_body_unsub is None:
        for m in re.finditer(r"https?://[^\s<>\"')]+", plain_body):
            url = m.group(0).rstrip(".,;:")
            ctx_start = max(0, m.start() - 120)
            ctx_end = min(len(plain_body), m.end() + 80)
            ctx = _clean_text(plain_body[ctx_start:ctx_end])
            if not html_body:
                anchors.append({"href": url, "text": ""})
            if pats.strong_unsub_re.search(ctx) or pats.url_unsub_hint_re.search(url):
                best_body_unsub = UnsubTarget(
                    kind="https_link",
                    url=url,
                    source="body",
                    matched_label=ctx[:80],
                    confidence=0.80,
                )
                break
        if not footer_snapshot:
            footer_snapshot = plain_body[-3000:]

    if PROMO_TEXT_RE.search(combined_text):
        signals.append("text:promo_or_unsub")

    unsub_target: Optional[UnsubTarget] = None
    fallback_unsub_url: Optional[str] = None
    if header_https:
        unsub_target = UnsubTarget(
            kind="rfc8058_one_click" if has_one_click else "https_link",
            url=header_https,
            source="header",
            matched_label="List-Unsubscribe-Post" if has_one_click else "List-Unsubscribe",
            confidence=1.0 if has_one_click else 0.95,
        )
        if best_body_unsub and best_body_unsub.url != header_https:
            fallback_unsub_url = best_body_unsub.url
    elif best_body_unsub:
        unsub_target = best_body_unsub

    subject_decoded = _decode_mime_header_text(str(msg.get("Subject", "")))
    consulting_links = (
        _extract_consulting_links(anchors, combined_text, sender_domain)
        if consulting_enabled
        else ConsultingLinks()
    )
    pertinent_url, pertinent_label = _extract_pertinent_url(
        anchors,
        unsub_target.url if unsub_target else None,
        consulting_links,
        sender_domain,
        subject=subject_decoded,
        wanted_domains=wanted_domains,
        enabled_packs=enabled_packs,
        patterns=pats,
    )

    interesting_headers = []
    for k in ("From", "Subject", "Date", "Message-ID", "List-Unsubscribe", "List-Unsubscribe-Post", "List-Id", "Precedence", "X-Mailer"):
        v = msg.get(k)
        if v:
            interesting_headers.append(f"{k}: {v}")

    return ExtractedEmailInfo(
        unsub_target=unsub_target,
        fallback_unsub_url=fallback_unsub_url,
        mailto_unsub=mailto_unsub,
        newsletter_signals=signals,
        consulting_links=consulting_links,
        pertinent_url=pertinent_url,
        pertinent_label=pertinent_label,
        pdf_attachments=pdfs,
        headers_snapshot="\n".join(interesting_headers),
        footer_html_snapshot=footer_snapshot,
        candidate_links=anchors if len(anchors) <= 40 else (anchors[:10] + anchors[-30:]),
        plain_excerpt=combined_text[:320],
    )


def extract_pdf_attachment(raw_source: bytes, part_index: int, target_dir: Path, prefix: str = "") -> Path:
    """Safely extract a PDF MIME part into target_dir and return its Path."""
    target_dir.mkdir(parents=True, exist_ok=True)
    msg = email.message_from_bytes(raw_source, policy=policy.default)
    for idx, part in enumerate(msg.walk()):
        if idx == part_index:
            raw_name = part.get_filename()
            decoded = _decode_mime_filename(raw_name) if raw_name else f"invoice-{idx}.pdf"
            safe_name = sanitize_filename(f"{prefix}_{decoded}" if prefix else decoded)
            out_path = (target_dir / safe_name).resolve()
            if target_dir.resolve() not in out_path.parents:
                raise ValueError("Resolved PDF path escapes target_dir")
            payload = part.get_payload(decode=True) or b""
            out_path.write_bytes(payload)
            return out_path
    raise FileNotFoundError(f"MIME part {part_index} not found")
