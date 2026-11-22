"""Configuration loader and runtime paths for nollpost."""

from __future__ import annotations

import locale
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from nollpost.packs import resolve_packs

_BCP47_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4}|-[0-9]{3})?$")


def _normalize_ui_language(raw: str, *, strict: bool = False) -> str:
    """Normalize a UI language preference ('', 'auto', 'en', 'sv', or BCP-47 tag)."""
    lang = str(raw or "").strip()
    if lang.lower() in ("", "auto"):
        return ""
    if not _BCP47_LOCALE_RE.fullmatch(lang):
        if strict:
            raise ValueError(f"Invalid ui_language {lang!r}; expected 'auto', '', 'en', 'sv', or a BCP-47 tag")
        return ""
    if "-" in lang:
        primary, region = lang.split("-", 1)
        return f"{primary.lower()}-{region}"
    return lang.lower()

VALID_GMAIL_ARCHIVE_STRATEGIES = ("background", "ui_menu", "tag_staging", "two_hop")

DEFAULT_CONFIG_TOML = """# nollpost configuration

[user]
display_name = ""
# Review Board UI language: "" (auto from browser/OS), "en", or "sv"
ui_language = ""
check_updates = true
idle_timeout_minutes = 45
mark_read_on_archive = true
max_inbox_messages = 500
catchup_scan_limit = 200
catchup_days_back = 14

[accounts]
# Strategy for archiving Gmail messages via Apple Mail:
# - "background"  : Silent headless two-hop via Gmail Trash -> All Mail to strip \\Inbox (default; no focus steal)
# - "ui_menu"     : Clicks Mail.app's Archive toolbar/menu action (requires Accessibility permission)
# - "two_hop"     : Legacy stages via _ArchiveTmp and moves to All Mail
# - "tag_staging" : Tags _ArchiveTmp for a user-installed Google Apps Script trigger (see README.md)
gmail_archive_strategy = "background"

[accounts.archive_map]
# Optional per-account archive overrides (auto-detected by default; never Trash/Deleted).
# iCloud = "Archive"
# Google = "[Gmail]/All Mail"

[rules]
# Optional built-in regional/domain rule packs (e.g., ["se"] for Swedish billing/Kivra/delivery rules).
enabled_packs = []

[browser]
# Playwright Chromium locale and Accept-Language header (auto-detected from OS or "en-US" when empty).
locale = ""
accept_language = ""

[consulting]
# Expert-network outreach classification is disabled by default.
enabled = false
networks = []
match_keywords = []
negative_keywords = []
decline_boilerplate = \"\"\"Hi,

Thank you for reaching out regarding this consultation. This topic falls outside my areas of expertise, so I will pass on this opportunity.

Best regards\"\"\"

[profile]
# Sender domains you always want surfaced under Updates (e.g., ["kickstarter.com", "patreon.com"]).
wanted_domains = []

bot_senders = [
    "vercel[bot]",
    "copilot",
    "dependabot[bot]",
    "github-actions[bot]",
    "renovate[bot]",
    "codecov[bot]",
    "netlify[bot]",
]

# Order/delivery domains kept archive-only (never auto-unsubscribed).
transactional_domains = []

bill_keywords = [
    "invoice",
    "payout",
    "due date",
    "payment reminder",
    "receipt",
    "billing",
]
"""


@dataclass
class TriageConfig:
    """Resolved runtime configuration for nollpost."""

    config_path: Path
    data_dir: Path
    cache_dir: Path
    db_path: Path
    invoices_dir: Path
    screenshots_dir: Path
    display_name: str = ""
    ui_language: str = ""
    check_updates: bool = True
    idle_timeout_minutes: int = 45
    mark_read_on_archive: bool = True
    max_inbox_messages: int = 500
    catchup_scan_limit: int = 200
    catchup_days_back: int = 14
    consulting_enabled: bool = False
    consulting_decline_boilerplate: str = ""
    gmail_archive_strategy: str = "background"
    browser_locale: str = "en-US"
    accept_language: str = "en-US,en;q=0.9"
    patterns_path: Optional[Path] = None
    enabled_packs: list[str] = field(default_factory=list)
    archive_map: dict[str, str] = field(default_factory=dict)
    consulting_networks: list[str] = field(default_factory=list)
    consulting_match_keywords: list[str] = field(default_factory=list)
    consulting_negative_keywords: list[str] = field(default_factory=list)
    wanted_domains: list[str] = field(default_factory=list)
    bot_senders: list[str] = field(default_factory=list)
    transactional_domains: list[str] = field(default_factory=list)
    bill_keywords: list[str] = field(default_factory=list)


def _resolve_path_with_legacy(
    env_keys: tuple[str, ...],
    primary_rel: str,
    legacy_rel: str,
) -> Path:
    for key in env_keys:
        override = os.environ.get(key)
        if override:
            return Path(override).expanduser()
    primary = Path(primary_rel).expanduser()
    legacy = Path(legacy_rel).expanduser()
    if not primary.exists() and legacy.exists():
        return legacy
    return primary


def get_config_path() -> Path:
    return _resolve_path_with_legacy(
        ("NOLLPOST_CONFIG", "MAIL_TRIAGE_CONFIG"),
        "~/.config/nollpost/config.toml",
        "~/.config/mail-triage/config.toml",
    )


def get_data_dir() -> Path:
    return _resolve_path_with_legacy(
        ("NOLLPOST_DATA_DIR", "MAIL_TRIAGE_DATA_DIR"),
        "~/Library/Application Support/nollpost",
        "~/Library/Application Support/mail-triage",
    )


def get_cache_dir() -> Path:
    return _resolve_path_with_legacy(
        ("NOLLPOST_CACHE_DIR", "MAIL_TRIAGE_CACHE_DIR"),
        "~/Library/Caches/nollpost",
        "~/Library/Caches/mail-triage",
    )


def _detect_system_locale() -> str:
    raw_loc = locale.getlocale()[0] or os.environ.get("LANG", "")
    cleaned = raw_loc.split(".")[0].split("@")[0].replace("_", "-").strip()
    if not cleaned or cleaned.upper() in ("C", "POSIX") or not _BCP47_LOCALE_RE.fullmatch(cleaned):
        return "en-US"
    parts = cleaned.split("-")
    if len(parts) == 2:
        return f"{parts[0].lower()}-{parts[1].upper() if len(parts[1]) == 2 else parts[1]}"
    return parts[0].lower()


def _default_accept_language(bcp47_locale: str) -> str:
    lang = bcp47_locale.split("-")[0].lower()
    has_region = "-" in bcp47_locale
    if lang == "en":
        return f"{bcp47_locale},en;q=0.9" if has_region else "en,en-US;q=0.9"
    if has_region:
        return f"{bcp47_locale},{lang};q=0.9,en;q=0.8"
    return f"{bcp47_locale},en;q=0.8"


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, val in updates.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _validate_toml_sections(merged: dict[str, Any]) -> None:
    for sec in ("user", "accounts", "rules", "browser", "consulting", "profile"):
        if not isinstance(merged.get(sec), dict):
            raise ValueError(f"[{sec}] in config.toml must be a table")
    if not isinstance(merged["accounts"].get("archive_map", {}), dict):
        raise ValueError("[accounts.archive_map] in config.toml must be a table")


def _require_str_list(name: str, val: Any, *, lowercase: bool = True) -> list[str]:
    if not isinstance(val, list):
        raise ValueError(f"{name} must be a list of strings")
    return _normalize_str_list(val, lowercase=lowercase)


def load_config(
    ensure_files: bool = True,
    *,
    config_path: Optional[Path] = None,
) -> TriageConfig:
    """Load config from TOML, creating default config and state directories if needed."""
    resolved_config_path = config_path or get_config_path()
    data_dir = get_data_dir()
    cache_dir = get_cache_dir()
    db_path = data_dir / "state.db"
    invoices_dir = cache_dir / "invoices"
    screenshots_dir = data_dir / "screenshots"

    if ensure_files:
        resolved_config_path.parent.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        invoices_dir.mkdir(parents=True, exist_ok=True)
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        if not resolved_config_path.exists():
            resolved_config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")

    defaults = tomllib.loads(DEFAULT_CONFIG_TOML)
    user_data: dict[str, Any] = {}
    if resolved_config_path.exists():
        user_data = tomllib.loads(resolved_config_path.read_text(encoding="utf-8"))
        merged = _deep_merge(defaults, user_data)
    else:
        merged = defaults

    _validate_toml_sections(merged)
    user_sec = merged["user"]
    acct_sec = merged["accounts"]
    rules_sec = merged["rules"]
    browser_sec = merged["browser"]
    consult_sec = merged["consulting"]
    prof_sec = merged["profile"]

    browser_locale = str(browser_sec.get("locale", "")).strip() or _detect_system_locale()
    accept_language = str(browser_sec.get("accept_language", "")).strip() or _default_accept_language(browser_locale)

    patterns_override = os.environ.get("NOLLPOST_PATTERNS")
    if patterns_override:
        patterns_path: Optional[Path] = Path(patterns_override).expanduser()
    else:
        candidate = resolved_config_path.parent / "unsub_patterns.toml"
        patterns_path = candidate if candidate.exists() else None

    gmail_strategy = str(acct_sec.get("gmail_archive_strategy", "background")).strip().lower()
    if gmail_strategy not in VALID_GMAIL_ARCHIVE_STRATEGIES:
        raise ValueError(
            f"Invalid gmail_archive_strategy {gmail_strategy!r}; "
            f"expected one of {', '.join(VALID_GMAIL_ARCHIVE_STRATEGIES)}"
        )

    raw_packs = rules_sec.get("enabled_packs", [])
    if not isinstance(raw_packs, list):
        raise ValueError("rules.enabled_packs must be a list of pack names (e.g. [\"se\"])")
    enabled_packs = [p.name for p in resolve_packs(raw_packs)]

    # Support both [consulting] (v1.0) and legacy [profile].consulting_* / [user].consulting_decline_boilerplate
    user_consult = user_data.get("consulting") if isinstance(user_data.get("consulting"), dict) else {}
    user_prof = user_data.get("profile") if isinstance(user_data.get("profile"), dict) else {}
    user_user = user_data.get("user") if isinstance(user_data.get("user"), dict) else {}
    networks = consult_sec.get("networks") or prof_sec.get("consulting_networks") or []
    match_kw = consult_sec.get("match_keywords") or prof_sec.get("consulting_match_keywords") or []
    neg_kw = consult_sec.get("negative_keywords") or prof_sec.get("consulting_negative_keywords") or []
    boilerplate = str(
        user_consult.get("decline_boilerplate")
        or user_user.get("consulting_decline_boilerplate")
        or consult_sec["decline_boilerplate"]
    )
    if "enabled" in user_consult:
        consulting_enabled = bool(user_consult["enabled"])
    elif user_prof.get("consulting_networks") or user_consult.get("networks"):
        consulting_enabled = True
    else:
        consulting_enabled = bool(consult_sec.get("enabled", False))

    ui_language = _normalize_ui_language(str(user_sec.get("ui_language", "")), strict=False)

    return TriageConfig(
        config_path=resolved_config_path,
        data_dir=data_dir,
        cache_dir=cache_dir,
        db_path=db_path,
        invoices_dir=invoices_dir,
        screenshots_dir=screenshots_dir,
        display_name=str(user_sec["display_name"]),
        ui_language=ui_language,
        check_updates=bool(user_sec.get("check_updates", True)),
        idle_timeout_minutes=int(user_sec.get("idle_timeout_minutes", 45)),
        mark_read_on_archive=bool(user_sec["mark_read_on_archive"]),
        max_inbox_messages=int(user_sec["max_inbox_messages"]),
        catchup_scan_limit=int(user_sec["catchup_scan_limit"]),
        catchup_days_back=int(user_sec.get("catchup_days_back", 14)),
        consulting_enabled=consulting_enabled,
        consulting_decline_boilerplate=boilerplate,
        gmail_archive_strategy=gmail_strategy,
        browser_locale=browser_locale,
        accept_language=accept_language,
        patterns_path=patterns_path,
        enabled_packs=enabled_packs,
        archive_map={str(k): str(v) for k, v in acct_sec.get("archive_map", {}).items()},
        consulting_networks=[s.lower() for s in networks],
        consulting_match_keywords=[s.lower() for s in match_kw],
        consulting_negative_keywords=[s.lower() for s in neg_kw],
        wanted_domains=[s.lower() for s in prof_sec["wanted_domains"]],
        bot_senders=[s.lower() for s in prof_sec["bot_senders"]],
        transactional_domains=[s.lower() for s in prof_sec["transactional_domains"]],
        bill_keywords=[s.lower() for s in prof_sec["bill_keywords"]],
    )


def toml_str(value: str) -> str:
    """Serialize a Python string as a single-line TOML double-quoted string."""
    out: list[str] = ['"']
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    for ch in normalized:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif code < 32 or code == 127:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def toml_str_list(values: list[str]) -> str:
    """Serialize a list of strings as a TOML array."""
    if not values:
        return "[]"
    items = ", ".join(toml_str(v) for v in values)
    return f"[{items}]"


def render_config_toml(data: dict[str, Any]) -> str:
    """Render structured config dictionary as valid TOML and verify round-trip parse."""
    user = data["user"]
    accounts = data["accounts"]
    archive_map = accounts.get("archive_map", {})
    rules = data["rules"]
    browser = data["browser"]
    consulting = data["consulting"]
    profile = data["profile"]

    lines = [
        "# nollpost configuration",
        "",
        "[user]",
        f"display_name = {toml_str(str(user.get('display_name', '')))}",
        f"ui_language = {toml_str(str(user.get('ui_language', '')))}",
        f"check_updates = {'true' if user.get('check_updates', True) else 'false'}",
        f"idle_timeout_minutes = {int(user.get('idle_timeout_minutes', 45))}",
        f"mark_read_on_archive = {'true' if user.get('mark_read_on_archive', True) else 'false'}",
        f"max_inbox_messages = {int(user.get('max_inbox_messages', 500))}",
        f"catchup_scan_limit = {int(user.get('catchup_scan_limit', 200))}",
        f"catchup_days_back = {int(user.get('catchup_days_back', 14))}",
        "",
        "[accounts]",
        f"gmail_archive_strategy = {toml_str(str(accounts.get('gmail_archive_strategy', 'background')))}",
        "",
        "[accounts.archive_map]",
    ]
    for acct_name, box_name in archive_map.items():
        lines.append(f"{toml_str(str(acct_name))} = {toml_str(str(box_name))}")

    lines.extend(
        [
            "",
            "[rules]",
            f"enabled_packs = {toml_str_list([str(x) for x in rules.get('enabled_packs', [])])}",
            "",
            "[browser]",
            f"locale = {toml_str(str(browser.get('locale', '')))}",
            f"accept_language = {toml_str(str(browser.get('accept_language', '')))}",
            "",
            "[consulting]",
            f"enabled = {'true' if consulting.get('enabled', False) else 'false'}",
            f"networks = {toml_str_list([str(x) for x in consulting.get('networks', [])])}",
            f"match_keywords = {toml_str_list([str(x) for x in consulting.get('match_keywords', [])])}",
            f"negative_keywords = {toml_str_list([str(x) for x in consulting.get('negative_keywords', [])])}",
            f"decline_boilerplate = {toml_str(str(consulting.get('decline_boilerplate', '')))}",
            "",
            "[profile]",
            f"wanted_domains = {toml_str_list([str(x) for x in profile.get('wanted_domains', [])])}",
            f"bot_senders = {toml_str_list([str(x) for x in profile.get('bot_senders', [])])}",
            f"transactional_domains = {toml_str_list([str(x) for x in profile.get('transactional_domains', [])])}",
            f"bill_keywords = {toml_str_list([str(x) for x in profile.get('bill_keywords', [])])}",
            "",
        ]
    )
    rendered = "\n".join(lines)
    tomllib.loads(rendered)
    return rendered


def _normalize_str_list(values: list[str], *, lowercase: bool = True) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in values:
        item = str(raw).strip()
        if lowercase:
            item = item.lower()
        if item and item not in seen:
            seen.add(item)
            cleaned.append(item)
    return cleaned


def _validate_int_range(name: str, value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer between {lo} and {hi}")
    try:
        num = int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{name} must be an integer between {lo} and {hi}") from err
    if not (lo <= num <= hi):
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return num


def update_config_file(
    config_path: Path,
    updates: dict[str, Any],
    *,
    archive_validator: Optional[Callable[[str], str]] = None,
) -> TriageConfig:
    """Validate and persist updated settings into `config_path`, returning freshly loaded config."""
    defaults = tomllib.loads(DEFAULT_CONFIG_TOML)
    user_data: dict[str, Any] = {}
    if config_path.exists():
        user_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    merged = _deep_merge(defaults, user_data)

    # Preserve effective legacy consulting fields when saving other sections
    resolved_existing = load_config(ensure_files=False, config_path=config_path)
    user_consult = user_data.get("consulting") if isinstance(user_data.get("consulting"), dict) else {}
    if "enabled" not in user_consult:
        merged["consulting"]["enabled"] = resolved_existing.consulting_enabled
    if not merged["consulting"].get("networks") and resolved_existing.consulting_networks:
        merged["consulting"]["networks"] = list(resolved_existing.consulting_networks)
    if not merged["consulting"].get("match_keywords") and resolved_existing.consulting_match_keywords:
        merged["consulting"]["match_keywords"] = list(resolved_existing.consulting_match_keywords)
    if not merged["consulting"].get("negative_keywords") and resolved_existing.consulting_negative_keywords:
        merged["consulting"]["negative_keywords"] = list(resolved_existing.consulting_negative_keywords)
    if resolved_existing.consulting_decline_boilerplate:
        merged["consulting"]["decline_boilerplate"] = resolved_existing.consulting_decline_boilerplate

    if "user" in updates and isinstance(updates["user"], dict):
        u = updates["user"]
        if "display_name" in u:
            merged["user"]["display_name"] = str(u["display_name"]).strip()
        if "ui_language" in u:
            merged["user"]["ui_language"] = _normalize_ui_language(str(u["ui_language"]), strict=True)
        if "check_updates" in u:
            merged["user"]["check_updates"] = bool(u["check_updates"])
        if "idle_timeout_minutes" in u:
            merged["user"]["idle_timeout_minutes"] = _validate_int_range(
                "idle_timeout_minutes", u["idle_timeout_minutes"], 0, 1440
            )
        if "mark_read_on_archive" in u:
            merged["user"]["mark_read_on_archive"] = bool(u["mark_read_on_archive"])
        if "max_inbox_messages" in u:
            merged["user"]["max_inbox_messages"] = _validate_int_range(
                "max_inbox_messages", u["max_inbox_messages"], 1, 5000
            )
        if "catchup_scan_limit" in u:
            merged["user"]["catchup_scan_limit"] = _validate_int_range(
                "catchup_scan_limit", u["catchup_scan_limit"], 1, 5000
            )
        if "catchup_days_back" in u:
            merged["user"]["catchup_days_back"] = _validate_int_range(
                "catchup_days_back", u["catchup_days_back"], 1, 365
            )

    if "accounts" in updates and isinstance(updates["accounts"], dict):
        a = updates["accounts"]
        if "gmail_archive_strategy" in a:
            strat = str(a["gmail_archive_strategy"]).strip().lower()
            if strat not in VALID_GMAIL_ARCHIVE_STRATEGIES:
                raise ValueError(
                    f"Invalid gmail_archive_strategy {strat!r}; expected one of {', '.join(VALID_GMAIL_ARCHIVE_STRATEGIES)}"
                )
            merged["accounts"]["gmail_archive_strategy"] = strat
        if "archive_map" in a:
            raw_map = a["archive_map"]
            if not isinstance(raw_map, dict):
                raise ValueError("archive_map must be a key-value mapping")
            clean_map: dict[str, str] = {}
            for k, v in raw_map.items():
                acct = str(k).strip()
                dest = str(v).strip()
                if not acct or not dest:
                    continue
                if archive_validator is not None:
                    dest = archive_validator(dest)
                clean_map[acct] = dest
            merged["accounts"]["archive_map"] = clean_map

    if "rules" in updates and isinstance(updates["rules"], dict):
        r = updates["rules"]
        if "enabled_packs" in r:
            packs = _require_str_list("enabled_packs", r["enabled_packs"], lowercase=True)
            merged["rules"]["enabled_packs"] = [p.name for p in resolve_packs(packs)]

    if "browser" in updates and isinstance(updates["browser"], dict):
        b = updates["browser"]
        if "locale" in b:
            loc = str(b["locale"]).strip()
            if loc and not _BCP47_LOCALE_RE.fullmatch(loc):
                raise ValueError(f"Invalid browser locale {loc!r}; expected BCP-47 tag such as 'en-US' or 'sv-SE'")
            merged["browser"]["locale"] = loc
        if "accept_language" in b:
            merged["browser"]["accept_language"] = str(b["accept_language"]).strip()

    if "consulting" in updates and isinstance(updates["consulting"], dict):
        c = updates["consulting"]
        if "enabled" in c:
            merged["consulting"]["enabled"] = bool(c["enabled"])
        if "networks" in c:
            merged["consulting"]["networks"] = _require_str_list("consulting.networks", c["networks"])
        if "match_keywords" in c:
            merged["consulting"]["match_keywords"] = _require_str_list("consulting.match_keywords", c["match_keywords"])
        if "negative_keywords" in c:
            merged["consulting"]["negative_keywords"] = _require_str_list("consulting.negative_keywords", c["negative_keywords"])
        if "decline_boilerplate" in c:
            merged["consulting"]["decline_boilerplate"] = str(c["decline_boilerplate"])

    if "profile" in updates and isinstance(updates["profile"], dict):
        p = updates["profile"]
        for key in ("wanted_domains", "bot_senders", "transactional_domains", "bill_keywords"):
            if key in p:
                merged["profile"][key] = _require_str_list(f"profile.{key}", p[key])

    rendered = render_config_toml(merged)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(".toml.tmp")
    tmp_path.write_text(rendered, encoding="utf-8")
    tmp_path.replace(config_path)
    return load_config(ensure_files=False, config_path=config_path)
