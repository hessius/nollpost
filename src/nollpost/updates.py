"""Lightweight, cached update checker against the official Homebrew tap formula."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from nollpost import __version__ as _FALLBACK_VERSION

DEFAULT_TAP_FORMULA_URL = "https://raw.githubusercontent.com/Hessius/homebrew-tap/main/Formula/nollpost.rb"
UPGRADE_COMMAND = "brew upgrade hessius/tap/nollpost"
RELEASES_BASE_URL = "https://github.com/Hessius/nollpost/releases/tag"
CACHE_TTL_SECONDS = 6 * 3600

_URL_VERSION_RE = re.compile(r'^\s*url\s+"[^"]+/archive/refs/tags/v([0-9]+(?:\.[0-9]+)*)\.tar\.gz"', re.MULTILINE)
_FORMULA_VERSION_RE = re.compile(r'^\s*version\s+"v?([0-9]+(?:\.[0-9]+)*)"', re.MULTILINE)


def get_current_version() -> str:
    """Return the installed nollpost package version string."""
    try:
        return pkg_version("nollpost")
    except PackageNotFoundError:
        return _FALLBACK_VERSION


def parse_version_tuple(raw: str) -> tuple[int, ...]:
    """Parse leading numeric semver components (e.g. 'v0.2.1' -> (0, 2, 1))."""
    cleaned = str(raw or "").strip().lstrip("vV")
    m = re.match(r"^(\d+(?:\.\d+)*)", cleaned)
    if not m:
        return ()
    return tuple(int(part) for part in m.group(1).split("."))


def is_newer_version(latest: str, current: str) -> bool:
    """Return True strictly when `latest` is a higher numeric version than `current`."""
    latest_parts = parse_version_tuple(latest)
    current_parts = parse_version_tuple(current)
    if not latest_parts or not current_parts:
        return False
    width = max(len(latest_parts), len(current_parts))
    padded_latest = latest_parts + (0,) * (width - len(latest_parts))
    padded_current = current_parts + (0,) * (width - len(current_parts))
    return padded_latest > padded_current


def extract_version_from_formula(formula_text: str) -> Optional[str]:
    """Extract the published release version from `Formula/nollpost.rb` contents."""
    for pattern in (_URL_VERSION_RE, _FORMULA_VERSION_RE):
        m = pattern.search(formula_text or "")
        if m:
            return m.group(1)
    return None


async def _default_fetch_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
        resp = await client.get(url, headers={"User-Agent": f"nollpost/{get_current_version()}"})
        resp.raise_for_status()
        return resp.text


def _build_payload(
    *,
    current: str,
    latest: str,
    checked_at: str,
    disabled: bool = False,
    error: str = "",
) -> dict[str, Any]:
    newer = False if disabled else is_newer_version(latest, current)
    clean_latest = latest.lstrip("vV") if latest else current
    return {
        "current_version": current,
        "latest_version": clean_latest,
        "update_available": newer,
        "upgrade_command": UPGRADE_COMMAND,
        "release_url": f"{RELEASES_BASE_URL}/v{clean_latest}",
        "checked_at": checked_at,
        "disabled": disabled,
        "error": error,
    }


def _read_fresh_cache(cache_file: Path, current: str) -> Optional[dict[str, Any]]:
    if not cache_file.is_file():
        return None
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(cached, dict):
            return None
        checked_epoch = float(cached.get("checked_epoch", 0))
        if time.time() - checked_epoch > CACHE_TTL_SECONDS:
            return None
        latest = str(cached.get("latest_version") or "")
        if not latest:
            return None
        return _build_payload(
            current=current,
            latest=latest,
            checked_at=str(cached.get("checked_at") or ""),
        )
    except (OSError, ValueError, TypeError):
        return None


def _write_cache(cache_file: Path, latest: str, checked_at: str) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(
                {
                    "latest_version": latest,
                    "checked_at": checked_at,
                    "checked_epoch": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


async def async_check_for_update(
    cache_dir: Optional[Path] = None,
    *,
    enabled: bool = True,
    force_refresh: bool = False,
    formula_url: Optional[str] = None,
    current_version: Optional[str] = None,
    fetcher: Optional[Callable[[str], Awaitable[str]]] = None,
) -> dict[str, Any]:
    """Check whether a newer Homebrew release exists, using a 6h disk cache."""
    current = (current_version or get_current_version()).lstrip("vV")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    env_disabled = os.environ.get("NOLLPOST_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes")
    if not enabled or env_disabled:
        return _build_payload(current=current, latest=current, checked_at=now_iso, disabled=True)

    cache_file = (cache_dir / "update_check.json") if cache_dir is not None else None
    if cache_file is not None and not force_refresh:
        cached = _read_fresh_cache(cache_file, current)
        if cached is not None:
            return cached

    target_url = os.environ.get("NOLLPOST_UPDATE_URL") or formula_url or DEFAULT_TAP_FORMULA_URL
    fetch_fn = fetcher or _default_fetch_text
    try:
        text = await fetch_fn(target_url)
        latest = extract_version_from_formula(text)
        if not latest:
            return _build_payload(
                current=current,
                latest=current,
                checked_at=now_iso,
                error="Could not parse version from Homebrew formula",
            )
        if cache_file is not None:
            _write_cache(cache_file, latest, now_iso)
        return _build_payload(current=current, latest=latest, checked_at=now_iso)
    except Exception as exc:
        return _build_payload(current=current, latest=current, checked_at=now_iso, error=str(exc))
