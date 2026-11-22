"""Privacy-redacted Missed-Unsubscribe bug report builder."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

_RECIPIENT_HEADER_RE = re.compile(
    r"^(To|Delivered-To|X-Original-To|Cc|Bcc|Envelope-To)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_SENDER_HEADER_RE = re.compile(
    r"^(From|Sender|Reply-To)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_ANGLE_ADDR_RE = re.compile(r"^(?P<name>[^<]*?)\s*<(?P<email>[^>@\s]+@[^>]+)>\s*$")
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>()]+", re.IGNORECASE)
_MAILTO_URL_RE = re.compile(r"mailto:[^\s\"'<>()]+", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{4,}){0,2}\b")

_SAFE_QUERY_KEYS = frozenset({
    "lang",
    "locale",
    "language",
    "action",
    "mode",
    "type",
    "step",
    "format",
    "view",
})


def _looks_like_token_segment(seg: str) -> bool:
    if not seg:
        return False
    lowered = seg.lower()
    if "@" in seg or "%40" in lowered:
        return True
    if seg.startswith("eyJ") and len(seg) >= 12:
        return True
    if re.fullmatch(r"[0-9a-fA-F]{16,}", seg):
        return True
    if len(seg) >= 20 and re.fullmatch(r"[A-Za-z0-9_.\-=]+", seg):
        has_digit = any(c.isdigit() for c in seg)
        has_upper = any(c.isupper() for c in seg)
        has_lower = any(c.islower() for c in seg)
        if has_digit and (has_upper or has_lower or len(seg) >= 24):
            return True
        if "=" in seg or (has_upper and has_lower and len(seg) >= 24):
            return True
    return False


def redact_url(url: str) -> str:
    """Strip auth tokens, emails, userinfo, and long token path segments while keeping ESP domain/path/query keys."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("mailto:"):
        addr_part = raw[7:].split("?", 1)[0].strip()
        if "@" in addr_part:
            domain = addr_part.rsplit("@", 1)[1]
            return f"mailto:<redacted>@{domain}"
        return "mailto:<redacted>"

    trailing = ""
    while raw and raw[-1] in ".,;":
        trailing = raw[-1] + trailing
        raw = raw[:-1]

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return redact_emails(raw) + trailing

    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return redact_emails(raw) + trailing

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port_part = f":{parsed.port}" if parsed.port else ""
    netloc = f"{host}{port_part}"

    # Redact token-like or email path segments
    segments = parsed.path.split("/")
    cleaned_segments = []
    for seg in segments:
        if _looks_like_token_segment(seg):
            cleaned_segments.append("<redacted-email>" if ("@" in seg or "%40" in seg.lower()) else "<redacted-token>")
        else:
            cleaned_segments.append(seg)
    path = "/".join(cleaned_segments)

    query_str = ""
    if parsed.query:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        out_pairs: list[str] = []
        for key, val in pairs:
            ek = quote(key, safe="-_.~")
            if (
                key.lower() in _SAFE_QUERY_KEYS
                and "@" not in val
                and "%40" not in val.lower()
                and re.fullmatch(r"[A-Za-z_-]{1,20}", val or "")
            ):
                out_pairs.append(f"{ek}={quote(val, safe='-_.~')}")
            elif val == "":
                out_pairs.append(ek)
            else:
                out_pairs.append(f"{ek}=<redacted>")
        query_str = "&".join(out_pairs)

    frag = "<redacted>" if parsed.fragment else ""
    rebuilt = f"{parsed.scheme.lower()}://{netloc}{path}"
    if query_str:
        rebuilt += f"?{query_str}"
    if frag:
        rebuilt += f"#{frag}"
    return rebuilt + trailing


def redact_emails(
    text: str,
    *,
    hide_domain: bool = False,
    hidden_domains: set[str] | None = None,
) -> str:
    """Replace email local-parts (and optionally domains or recipient domains) in arbitrary text."""
    hide_doms = {d.lower() for d in (hidden_domains or set()) if d}

    def _repl(match: re.Match[str]) -> str:
        prefix, local, at_domain = match.group(1), match.group(2), match.group(3)
        dom_only = at_domain[1:].lower()
        if local == "redacted" and prefix == "<":
            if dom_only in hide_doms:
                return f"{prefix}<redacted>@<redacted>"
            return match.group(0)
        if hide_domain or dom_only in hide_doms:
            return f"{prefix}<redacted>@<redacted>"
        return f"{prefix}<redacted>{at_domain}"

    out = re.sub(
        r"(^|[^A-Za-z0-9._%+\-=])([A-Za-z0-9._%+\-=]+)(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
        _repl,
        text,
    )
    out = re.sub(
        r"([A-Za-z0-9._%+\-=]+)(%40([A-Za-z0-9.\-]+\.[A-Za-z]{2,}))",
        lambda m: (
            "<redacted>@<redacted>"
            if (hide_domain or m.group(3).lower() in hide_doms)
            else f"<redacted>@{m.group(3)}"
        ),
        out,
        flags=re.IGNORECASE,
    )
    return out


def _extract_recipient_info(header_val: str) -> tuple[list[str], set[str]]:
    tokens: list[str] = []
    domains: set[str] = set()
    val = header_val.strip()
    m = _ANGLE_ADDR_RE.match(val)
    if m:
        name = m.group("name").strip().strip("\"'")
        if len(name) >= 3:
            tokens.append(name)
            for part in re.split(r"[\s,]+", name):
                clean = part.strip(".\"'()")
                if len(clean) >= 4:
                    tokens.append(clean)
    for local, dom in re.findall(r"([A-Za-z0-9._%+\-=]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", val):
        if len(local) >= 4:
            tokens.append(local)
        if dom:
            domains.add(dom.lower())
    return tokens, domains


def _redact_personal_tokens(text: str, tokens: list[str]) -> str:
    out = text
    for tok in sorted(set(tokens), key=len, reverse=True):
        if len(tok) < 3:
            continue
        escaped = re.escape(tok)
        # Word-boundary replace to avoid corrupting unrelated HTML/CSS tokens
        out = re.sub(rf"(?i)(?<![A-Za-z0-9_-]){escaped}(?![A-Za-z0-9_-])", "[REDACTED]", out)
    return out


def redact_text_preserve_structure(
    text: str,
    personal_tokens: list[str] | None = None,
    hidden_domains: set[str] | None = None,
) -> str:
    """Redact URLs, tokens, emails, and known personal names from text/HTML while preserving tags and structure."""
    if not text:
        return ""
    out = _HTTP_URL_RE.sub(lambda m: redact_url(m.group(0)), text)
    out = _MAILTO_URL_RE.sub(lambda m: redact_url(m.group(0)), out)
    out = _JWT_RE.sub("<redacted-token>", out)
    out = redact_emails(out, hidden_domains=hidden_domains)
    if personal_tokens:
        out = _redact_personal_tokens(out, personal_tokens)
    return out


def redact_headers_snapshot(
    headers_snapshot: str,
    *,
    preserve_domain: str = "",
) -> tuple[str, list[str], set[str]]:
    """Redact personal names/addresses from headers while keeping ESP headers intact."""
    if not headers_snapshot:
        return "", [], set()
    lines = headers_snapshot.splitlines()
    personal_tokens: list[str] = []
    recipient_domains: set[str] = set()
    for line in lines:
        rec_m = _RECIPIENT_HEADER_RE.match(line)
        if rec_m:
            toks, doms = _extract_recipient_info(rec_m.group(2))
            personal_tokens.extend(toks)
            recipient_domains.update(doms)
    if preserve_domain:
        recipient_domains.discard(preserve_domain.lower())

    out_lines: list[str] = []
    for line in lines:
        rec_m = _RECIPIENT_HEADER_RE.match(line)
        if rec_m:
            hdr = rec_m.group(1)
            out_lines.append(f"{hdr}: [REDACTED] <redacted>@<redacted>")
            continue
        snd_m = _SENDER_HEADER_RE.match(line)
        if snd_m:
            hdr, val = snd_m.group(1), snd_m.group(2)
            ang = _ANGLE_ADDR_RE.match(val.strip())
            if ang:
                name = ang.group("name").strip().strip("\"'")
                if name:
                    personal_tokens.append(name)
                red_email = redact_emails(ang.group("email"), hidden_domains=recipient_domains)
                out_lines.append(f"{hdr}: [REDACTED] <{red_email}>" if name else f"{hdr}: <{red_email}>")
            else:
                out_lines.append(f"{hdr}: {redact_emails(val.strip(), hidden_domains=recipient_domains)}")
            continue
        red_line = redact_text_preserve_structure(line, personal_tokens, recipient_domains)
        out_lines.append(red_line)
    return "\n".join(out_lines), personal_tokens, recipient_domains


def redact_missed_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a privacy-redacted copy of a missed_unsubscribes row suitable for bug reports."""
    sender_domain = str(entry.get("sender_domain", ""))
    headers_red, personal_tokens, hidden_domains = redact_headers_snapshot(
        entry.get("headers_snapshot", ""),
        preserve_domain=sender_domain,
    )
    sender_raw = str(entry.get("sender", ""))
    ang = _ANGLE_ADDR_RE.match(sender_raw.strip())
    if ang and ang.group("name").strip():
        personal_tokens.append(ang.group("name").strip().strip("\"'"))

    sender_red = redact_text_preserve_structure(sender_raw, personal_tokens, hidden_domains)
    if ang and ang.group("name").strip():
        sender_red = f"[REDACTED] <{redact_emails(ang.group('email'), hidden_domains=hidden_domains)}>"

    subject_red = redact_text_preserve_structure(str(entry.get("subject", "")), personal_tokens, hidden_domains)
    reason_red = redact_text_preserve_structure(str(entry.get("failure_reason", "")), personal_tokens, hidden_domains)
    final_url_red = redact_url(str(entry.get("final_url", "")))
    footer_red = redact_text_preserve_structure(str(entry.get("footer_html_snapshot", "")), personal_tokens, hidden_domains)

    red_links: list[dict[str, str]] = []
    for link in entry.get("candidate_links") or []:
        href = redact_url(str(link.get("href", "")))
        txt = redact_text_preserve_structure(str(link.get("text", "")), personal_tokens, hidden_domains)
        if href or txt:
            red_links.append({"text": txt, "href": href})

    return {
        "id": entry.get("id"),
        "sender_domain": sender_domain,
        "sender_redacted": sender_red,
        "subject_redacted": subject_red,
        "stage": str(entry.get("stage", "")),
        "failure_reason": reason_red,
        "final_url": final_url_red,
        "candidate_links": red_links,
        "headers_snapshot": headers_red,
        "footer_html_snapshot": footer_red,
    }


def format_missed_bug_report(entry: dict[str, Any]) -> str:
    """Format a missed_unsubscribes entry as a redacted Markdown bug report."""
    red = redact_missed_entry(entry)
    links_lines = "\n".join(
        f"- {l['text'] or '(no text)'}: `{l['href']}`" for l in red["candidate_links"]
    ) or "- (none)"
    target_line = f"`{red['final_url']}`" if red["final_url"] else "(none)"
    return (
        f"### Redacted Missed-Unsubscribe Report\n\n"
        f"- **Sender Domain:** `{red['sender_domain'] or 'unknown'}`\n"
        f"- **Sender (redacted):** {red['sender_redacted'] or '(unknown)'}\n"
        f"- **Stage:** `{red['stage']}`\n"
        f"- **Failure Reason:** {red['failure_reason']}\n"
        f"- **Final Target URL:** {target_line}\n\n"
        f"#### Candidate Links ({len(red['candidate_links'])})\n"
        f"{links_lines}\n\n"
        f"#### Redacted Headers\n"
        f"```text\n{red['headers_snapshot'] or '(none)'}\n```\n\n"
        f"#### Redacted Footer HTML Snippet\n"
        f"```html\n{red['footer_html_snapshot'] or '(none)'}\n```\n"
    )
