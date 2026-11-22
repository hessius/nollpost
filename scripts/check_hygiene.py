"""Repository hygiene check for tracked files: secrets, local paths, and runtime state."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_FILENAMES = frozenset({"config.toml", ".env"})
FORBIDDEN_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".png", ".jpg", ".jpeg", ".pdf"})

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key header", re.compile(r"-----BEGIN (?:[A-Z ]*?)PRIVATE KEY-----")),
    ("GitHub classic PAT", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API key", re.compile(r"\bsk-(?:ant|proj)-[A-Za-z0-9_\-]{20,}\b")),
]

LOCAL_HOME_RE = re.compile(r"(/(?:Users|home)/([A-Za-z0-9._\-]+))")
ALLOWED_HOME_NAMES = frozenset({"example", "alice", "runner", "user", "test"})


def tracked_files(repo_root: Path) -> list[Path]:
    out = subprocess.check_output(["git", "ls-files"], cwd=repo_root, text=True)
    return [repo_root / line for line in out.splitlines() if line.strip()]


def scan_paths(paths: list[Path], repo_root: Path) -> list[str]:
    violations: list[str] = []
    for path in paths:
        rel = path.relative_to(repo_root).as_posix()
        if path.name in FORBIDDEN_FILENAMES and not rel.startswith("src/nollpost/"):
            violations.append(f"{rel}: tracked sensitive config/env filename")
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            if path.suffix.lower() == ".png" and rel.startswith("docs/screenshots/"):
                continue
            violations.append(f"{rel}: tracked binary/state file ({path.suffix})")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pat in SECRET_PATTERNS:
                if pat.search(line):
                    violations.append(f"{rel}:{lineno}: potential {label} detected")
            for match in LOCAL_HOME_RE.finditer(line):
                username = match.group(2)
                if username.lower() not in ALLOWED_HOME_NAMES:
                    violations.append(f"{rel}:{lineno}: non-placeholder local home path '{match.group(1)}'")
    return violations


def scan_repository(repo_root: Path) -> list[str]:
    return scan_paths(tracked_files(repo_root), repo_root)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    issues = scan_repository(root)
    if issues:
        for item in issues:
            print(f"HYGIENE FAIL: {item}", file=sys.stderr)
        return 1
    print("Hygiene check passed: 0 secrets, tracked runtime files, or hardcoded local home paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
