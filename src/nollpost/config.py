"""Configuration loader and runtime paths for nollpost."""

from __future__ import annotations

import locale
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from nollpost.packs import resolve_packs

VALID_GMAIL_ARCHIVE_STRATEGIES = ("ui_menu", "tag_staging", "two_hop")

DEFAULT_CONFIG_TOML = """# nollpost configuration

[user]
display_name = ""
mark_read_on_archive = true
max_inbox_messages = 500
catchup_scan_limit = 200
catchup_days_back = 14

[accounts]
# Strategy for archiving Gmail messages via Apple Mail:
# - "ui_menu"     : Clicks Mail.app's Archive menu item (safe default; requires Accessibility permission)
# - "two_hop"     : Stages via _ArchiveTmp and moves to All Mail
# - "tag_staging" : Tags _ArchiveTmp for a user-installed Google Apps Script trigger (see README.md)
gmail_archive_strategy = "ui_menu"

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
    mark_read_on_archive: bool = True
    max_inbox_messages: int = 500
    catchup_scan_limit: int = 200
    catchup_days_back: int = 14
    consulting_enabled: bool = False
    consulting_decline_boilerplate: str = ""
    gmail_archive_strategy: str = "ui_menu"
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
    cleaned = raw_loc.split(".")[0].replace("_", "-").strip()
    if not cleaned or cleaned.upper() in ("C", "POSIX"):
        return "en-US"
    return cleaned


def _default_accept_language(bcp47_locale: str) -> str:
    lang = bcp47_locale.split("-")[0].lower()
    if lang == "en":
        return f"{bcp47_locale},en;q=0.9"
    return f"{bcp47_locale},{lang};q=0.9,en;q=0.8"


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, val in updates.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load_config(ensure_files: bool = True) -> TriageConfig:
    """Load config from TOML, creating default config and state directories if needed."""
    config_path = get_config_path()
    data_dir = get_data_dir()
    cache_dir = get_cache_dir()
    db_path = data_dir / "state.db"
    invoices_dir = cache_dir / "invoices"
    screenshots_dir = data_dir / "screenshots"

    if ensure_files:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        invoices_dir.mkdir(parents=True, exist_ok=True)
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        if not config_path.exists():
            config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")

    defaults = tomllib.loads(DEFAULT_CONFIG_TOML)
    user_data: dict[str, Any] = {}
    if config_path.exists():
        user_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        merged = _deep_merge(defaults, user_data)
    else:
        merged = defaults

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
        candidate = config_path.parent / "unsub_patterns.toml"
        patterns_path = candidate if candidate.exists() else None

    gmail_strategy = str(acct_sec.get("gmail_archive_strategy", "ui_menu")).strip().lower()
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
    user_consult = user_data.get("consulting", {})
    user_prof = user_data.get("profile", {})
    networks = consult_sec.get("networks") or prof_sec.get("consulting_networks") or []
    match_kw = consult_sec.get("match_keywords") or prof_sec.get("consulting_match_keywords") or []
    neg_kw = consult_sec.get("negative_keywords") or prof_sec.get("consulting_negative_keywords") or []
    boilerplate = str(
        user_consult.get("decline_boilerplate")
        or user_data.get("user", {}).get("consulting_decline_boilerplate")
        or consult_sec["decline_boilerplate"]
    )
    if "enabled" in user_consult:
        consulting_enabled = bool(user_consult["enabled"])
    elif user_prof.get("consulting_networks") or user_consult.get("networks"):
        consulting_enabled = True
    else:
        consulting_enabled = bool(consult_sec.get("enabled", False))

    return TriageConfig(
        config_path=config_path,
        data_dir=data_dir,
        cache_dir=cache_dir,
        db_path=db_path,
        invoices_dir=invoices_dir,
        screenshots_dir=screenshots_dir,
        display_name=str(user_sec["display_name"]),
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
