"""Loader for packaged and user-extended unsubscribe/confirmation regex patterns."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_PATTERNS_PATH = Path(__file__).parent / "patterns" / "unsub_patterns.toml"


@dataclass(frozen=True)
class UnsubPatterns:
    """Compiled regex sets and ordered button patterns loaded from TOML data files."""

    strong_unsub_patterns: tuple[str, ...]
    strong_unsub_re: re.Pattern[str]
    url_unsub_hint_re: re.Pattern[str]
    unsub_button_patterns: tuple[str, ...]
    optout_choice_patterns: tuple[str, ...]
    optout_choice_re: re.Pattern[str]
    optout_guard_re: re.Pattern[str]
    resubscribe_button_re: re.Pattern[str]
    success_text_re: re.Pattern[str]
    decline_button_patterns: tuple[str, ...]
    captcha_re: re.Pattern[str]


def _flatten_str_lists(section: Any, ordered_keys: tuple[str, ...] | None = None) -> list[str]:
    if not isinstance(section, dict):
        raise ValueError("Pattern section must be a TOML table")
    out: list[str] = []
    keys = list(ordered_keys or section.keys())
    for k in section:
        if k not in keys:
            keys.append(k)
    for k in keys:
        vals = section.get(k)
        if vals is None:
            continue
        if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
            raise ValueError(f"Pattern list '{k}' must be an array of strings")
        out.extend(v for v in vals if v)
    return out


def _compile_alternation(patterns: list[str], name: str, *, anchor_wrap: str = "") -> re.Pattern[str]:
    if not patterns:
        raise ValueError(f"Pattern set '{name}' cannot be empty")
    for p in patterns:
        try:
            re.compile(p, re.I)
        except re.error as exc:
            raise ValueError(f"Invalid regex in '{name}': {p!r} ({exc})") from exc
    joined = "|".join(patterns)
    expr = anchor_wrap.format(joined=joined) if anchor_wrap else f"(?:{joined})"
    try:
        return re.compile(expr, re.I)
    except re.error as exc:
        raise ValueError(f"Invalid combined regex in '{name}': {exc}") from exc


def _validate_pattern_list(patterns: list[str], name: str) -> tuple[str, ...]:
    for p in patterns:
        try:
            re.compile(p, re.I)
        except re.error as exc:
            raise ValueError(f"Invalid regex in '{name}': {p!r} ({exc})") from exc
    if patterns:
        try:
            re.compile("|".join(patterns), re.I)
        except re.error as exc:
            raise ValueError(f"Invalid combined regex in '{name}': {exc}") from exc
    return tuple(patterns)


def _merge_section_patterns(
    base_data: dict[str, Any],
    custom_data: dict[str, Any],
    section: str,
    ordered_keys: tuple[str, ...] | None = None,
) -> list[str]:
    base_list = _flatten_str_lists(base_data.get(section, {}), ordered_keys=ordered_keys)
    if section not in custom_data:
        return base_list
    extra = _flatten_str_lists(custom_data[section], ordered_keys=ordered_keys)
    # Preserve order while deduplicating exact repeats
    merged = list(base_list)
    seen = set(merged)
    for pat in extra:
        if pat not in seen:
            seen.add(pat)
            merged.append(pat)
    return merged


def _merge_button_tiers(base_data: dict[str, Any], custom_data: dict[str, Any]) -> list[str]:
    tiers = ("tier1_all", "tier2_confirm", "tier3_save")
    base_sec = base_data.get("unsub_buttons", {})
    custom_sec = custom_data.get("unsub_buttons", {})
    if not isinstance(base_sec, dict) or not isinstance(custom_sec, dict):
        raise ValueError("unsub_buttons section must be a TOML table")

    merged: list[str] = []
    seen: set[str] = set()
    all_keys = list(tiers)
    for k in list(base_sec.keys()) + list(custom_sec.keys()):
        if k not in all_keys:
            all_keys.append(k)
    for tier in all_keys:
        for src in (base_sec, custom_sec):
            vals = src.get(tier)
            if vals is None:
                continue
            if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
                raise ValueError(f"unsub_buttons.{tier} must be an array of strings")
            for pat in vals:
                if pat and pat not in seen:
                    seen.add(pat)
                    merged.append(pat)
    return merged


@lru_cache(maxsize=16)
def _load_cached(custom_path_str: str, mtime_ns: int) -> UnsubPatterns:
    del mtime_ns  # cache key invalidation only
    base_data = tomllib.loads(DEFAULT_PATTERNS_PATH.read_text(encoding="utf-8"))
    custom_data: dict[str, Any] = {}
    if custom_path_str:
        cpath = Path(custom_path_str)
        if cpath.exists():
            custom_data = tomllib.loads(cpath.read_text(encoding="utf-8"))

    strong = _merge_section_patterns(base_data, custom_data, "strong_unsub")
    url_hints = _merge_section_patterns(base_data, custom_data, "url_unsub_hints")
    unsub_buttons = _merge_button_tiers(base_data, custom_data)
    optout_choices = _merge_section_patterns(base_data, custom_data, "optout_choices")
    resub_buttons = _merge_section_patterns(base_data, custom_data, "resubscribe_buttons")
    success_phrases = _merge_section_patterns(base_data, custom_data, "success_phrases")
    decline_buttons = _merge_section_patterns(base_data, custom_data, "decline_buttons")
    captcha_patterns = _merge_section_patterns(base_data, custom_data, "captcha")

    return UnsubPatterns(
        strong_unsub_patterns=_validate_pattern_list(strong, "strong_unsub"),
        strong_unsub_re=_compile_alternation(strong, "strong_unsub"),
        url_unsub_hint_re=_compile_alternation(url_hints, "url_unsub_hints"),
        unsub_button_patterns=_validate_pattern_list(unsub_buttons, "unsub_buttons"),
        optout_choice_patterns=_validate_pattern_list(optout_choices, "optout_choices"),
        optout_choice_re=_compile_alternation(optout_choices, "optout_choices"),
        optout_guard_re=_compile_alternation([*optout_choices, r"all\s+email"], "optout_choices"),
        resubscribe_button_re=_compile_alternation(
            resub_buttons,
            "resubscribe_buttons",
            anchor_wrap=r"^\s*[+↺]?\s*(?:{joined})\s*$",
        ),
        success_text_re=_compile_alternation(success_phrases, "success_phrases"),
        decline_button_patterns=_validate_pattern_list(decline_buttons, "decline_buttons"),
        captcha_re=_compile_alternation(captcha_patterns, "captcha"),
    )


def load_unsub_patterns(custom_path: Path | None = None) -> UnsubPatterns:
    """Load packaged unsubscribe patterns merged with an optional user override file."""
    if custom_path is not None and custom_path.exists():
        stat = custom_path.stat()
        return _load_cached(str(custom_path.resolve()), stat.st_mtime_ns)
    return _load_cached("", 0)
