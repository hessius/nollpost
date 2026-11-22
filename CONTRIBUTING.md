# Contributing to Nollpost

Thank you for helping make Nollpost safer and more accurate across mail providers and languages!

## Development Setup

```bash
git clone https://github.com/hessius/nollpost.git
cd nollpost
uv venv --python 3.13
uv pip install -e ".[dev]"
.venv/bin/playwright install chromium
.venv/bin/pytest -v
```

## Core Safety Invariant

Nollpost **never deletes emails**. Any feature that deletes messages, routes to `Trash` / `Deleted Messages` / `Papperskorgen` / `Skraeppost`, or bypasses user confirmation before mutating Mail.app or sending external replies will not be accepted.

## Reporting Missed Unsubscribes (Privacy-First)

When automatic extraction or Playwright fails, Nollpost captures the failure in your local **Missed-Unsub Queue**:

1. Export a **privacy-redacted** report:
   - In the Review Board **Missed-Unsub Queue** tab: click **Inspect Snapshot** → **Copy Redacted Bug Report**.
   - Or via CLI:
     ```bash
     nollpost missed
     nollpost missed export <id>
     ```
2. Double-check that personal `To:`/`From:` addresses, names, and query tokens show as `<redacted>` / `[REDACTED]`.
3. Open a GitHub issue using the **Missed Unsubscribe Pattern** template.

## Adding Unsubscribe Patterns or Rule Packs

- **Multilingual / button / confirmation phrases:** Edit [`src/nollpost/patterns/unsub_patterns.toml`](src/nollpost/patterns/unsub_patterns.toml) (no code changes required for standard phrases).
- **Optional regional or domain packs:** Add opt-in packs in [`src/nollpost/packs.py`](src/nollpost/packs.py) so defaults remain neutral for all users.

## Pull Request Checklist

- All existing and new tests pass (`pytest -v`)
- No personal emails, domains, credentials, or local filesystem paths in code, tests, or fixtures
- Any externalizable phrasing belongs in TOML data files or opt-in packs rather than core defaults
