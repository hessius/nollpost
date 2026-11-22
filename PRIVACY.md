# Privacy & Safety Architecture

Nollpost is designed to run **100% locally** on your Mac with strict guardrails around your inbox data.

## 1. Local-First & Zero Telemetry
- **No cloud backend, no analytics, no telemetry:** Nollpost never phones home.
- **Zero LLM tokens by default:** Classification, link extraction, and unsubscribe execution run deterministically on your machine.
- **Localhost-only Web UI:** `nollpost serve` binds strictly to `127.0.0.1` and requires an ephemeral per-session token (`secrets.token_urlsafe(24)`) passed via `?key=...`, `X-Triage-Key`, or an `HttpOnly; SameSite=Strict` cookie.

## 2. Strict Archive-Only Non-Deletion Invariant
- Nollpost **never deletes emails** and never allows `Trash`, `Deleted Messages`, `Papperskorgen`, `Junk`, `Skräppost`, or `Spam` as an Archive destination.
- In Gmail's default `"background"` strategy, each message with an RFC `Message-ID` is moved momentarily to `[Gmail]/Trash` (localized, e.g. `[Gmail]/Papperskorgen`, to trigger Gmail to clear `\Inbox`) and immediately relocated by `Message-ID` into `[Gmail]/All Mail` / `[Gmail]/All e-post` (with automatic rollback to `INBOX` if relocation fails).
- `validate_archive_destination()` enforces a hard allow/deny check across every mailbox path segment before any AppleScript move runs.
- Every batch execution requires explicit approval in the **Confirmation Preview** modal (`0 deleted`).

## 3. Outbound Network & SSRF Safeguards
- Nollpost only makes outbound HTTP requests when you confirm an unsubscribe or decline action, or (unless disabled) performs a lightweight read-only check (`GET` to the public Homebrew tap formula with a 6-hour local cache) for new Nollpost versions.
- Update checks send zero user, inbox, or telemetry data and can be disabled via `check_updates = false` in `config.toml` (or **Settings**) or `NOLLPOST_NO_UPDATE_CHECK=1`.
- `is_public_http_url()` validates target URLs (and every HTTP redirect / Playwright route) to block `localhost`, `.local`/`.internal` hostnames, private/loopback/link-local IPv4/IPv6 ranges, and obfuscated IP literals.
- **CAPTCHA / Bot Challenges:** Nollpost never solves or bypasses CAPTCHAs (`reCAPTCHA`, `hCaptcha`, `Turnstile`). If one appears, automation stops immediately (`blocked_captcha`) and surfaces the URL for manual completion.

## 4. Local State Storage
All state lives locally in SQLite (`state.db`), extracted PDFs (`invoices/`), and unsubscribe confirmation screenshots (`screenshots/`), under your macOS `Application Support` and `Caches` directories (or custom `NOLLPOST_DATA_DIR` / `NOLLPOST_CACHE_DIR` paths).
