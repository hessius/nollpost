# Scheduled Auto-Triage & Menu Bar Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add configurable hourly background auto-triage (consulting mismatch declines and one-click `auto_archive` sender/category rules) orchestrated by `Nollpost.app`, with a menu bar notification dot and an explicit-dismiss "Handled Automatically" digest in the Review Board web UI.

**Architecture:** `Nollpost.app` owns an interval timer (default 60m, active only while the menu bar app is running) and delegates to `nollpost auto --json` when the local server is stopped or `POST /api/auto/run` when it is running, guarded by an `flock` run lock. Executed auto-actions are recorded in SQLite `action_log` (`source = 'auto'`) and mirrored to an atomic `auto_status.json` marker watched by Swift until the user clicks "Mark summary seen" (`POST /api/auto/ack`) in the Web UI.

**Tech Stack:** Python 3.12+, FastAPI, SQLite, Playwright, Apple Mail AppleScript, Vanilla JS/HTML/CSS, Swift 5 / AppKit (`macos/NollpostMenuBar/main.swift`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-automation-scheduling-design.md`

## Global Constraints

- Strictly Archive-only (`0 deleted`); never move messages to Trash/Deleted permanently.
- Background automation runs **only** while `Nollpost.app` is running (no `launchd` plists); CLI `nollpost auto` is one-shot/manual.
- Preserve `idle_timeout_minutes = 45` on-demand server behavior.
- Never auto-archive `Security Alert`, `matched_gov_pack` (e.g., Gov/Kivra), or PDF attachments when `skip_pdf_invoices = true`.
- Never track secrets, `.db`/runtime files, or non-placeholder `/Users/<name>` paths (`scripts/check_hygiene.py`).
- Maintain full `en.json` / `sv.json` localization parity (`tests/test_i18n.py`).

## Review Focus

1. **Plain bank notification with generic bill keywords (`payment`/`receipt`) and no newsletter headers:** `auto_archive` must override generic `bill_keywords`, `is_explicit_bill_sender`, and `Unclassified Automated`, while still yielding to `Security Alert`, `PDF attachments`, and `matched_gov_pack`. (Pinned in Task 3.)
2. **Failed auto-decline or CAPTCHA on an unread `INBOX` item:** Must stay unread in `INBOX`, record once in `action_log` (never in `missed_unsubscribes`), and be skipped on subsequent hourly ticks rather than spamming outbound replies or Playwright loops. (Pinned in Task 2 & Task 4.)
3. **Concurrent `nollpost auto` vs. `nollpost serve` scan/execute:** Both paths must share `run.lock` (`fcntl.flock`) so `nollpost auto` exits cleanly with `{"status": "skipped", "reason": "busy"}` instead of racing on `Mail.app` or SQLite. (Pinned in Task 4 & Task 5.)
4. **`gmail_archive_strategy = "ui_menu"` during background `nollpost auto`:** Must skip Gmail auto-archiving with a clear digest warning instead of activating `Mail.app` windows on screen, while non-Gmail accounts proceed. (Pinned in Task 4.)
5. **Digest persistence across refreshes until explicit dismissal:** `GET /api/auto/digest` must keep returning unacknowledged items across page reloads and `Refresh Scan` until `POST /api/auto/ack` advances `auto_digest_ack_log_id` and resets `unseen_count` to `0` in `auto_status.json`. (Pinned in Task 2 & Task 5.)

---

### Task 1: `[automation]` Config Schema, Defaults, and TOML Persistence

**Files:**
- Modify: `src/nollpost/config.py:34-142,215-340,372-576`
- Test: `tests/test_settings_and_sender_memory.py`

**Interfaces:**
- Consumes: Existing `DEFAULT_CONFIG_TOML`, `TriageConfig`, `load_config`, `render_config_toml`, `update_config_file` in `src/nollpost/config.py`.
- Produces: `TriageConfig` attributes `auto_enabled: bool`, `auto_interval_minutes: int`, `auto_archive_sender_rules: bool`, `auto_decline_consulting: bool`, `auto_archive_transactional: bool`, `auto_archive_bot_noise: bool`, `auto_archive_always_archive: bool`, `auto_archive_recent_unsubs: bool`, `auto_skip_security_alerts: bool`, `auto_skip_pdf_invoices: bool`, plus `[automation]` validation and round-trip persistence in `update_config_file`.

- [ ] **Step 1: Write the failing test**

In `tests/test_settings_and_sender_memory.py`, add tests `test_automation_config_defaults_and_roundtrip(tmp_path, monkeypatch)` and `test_automation_config_rejects_invalid_interval(tmp_path, monkeypatch)`:
- Verify `load_config()` without an `[automation]` section yields `auto_enabled is True`, `auto_interval_minutes == 60`, `auto_archive_sender_rules is True`, `auto_decline_consulting is False`, `auto_archive_transactional is False`, `auto_archive_bot_noise is False`, `auto_archive_always_archive is False`, `auto_archive_recent_unsubs is False`, `auto_skip_security_alerts is True`, `auto_skip_pdf_invoices is True`.
- Verify `update_config_file(..., {"automation": {"interval_minutes": 30, "auto_decline_consulting": True, "auto_archive_transactional": True}})` persists and reloads cleanly.
- Verify `update_config_file(..., {"automation": {"interval_minutes": 0}})` and `{"automation": {"interval_minutes": True}}` raise `ValueError`.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_settings_and_sender_memory.py -k automation_config -v`
Expected: FAIL with `AttributeError: 'TriageConfig' object has no attribute 'auto_interval_minutes'`.

- [ ] **Step 3: Add `[automation]` defaults, fields, validation, and rendering to `src/nollpost/config.py`**

- Add `[automation]` section to `DEFAULT_CONFIG_TOML` with `enabled = true`, `interval_minutes = 60`, `auto_archive_sender_rules = true`, `auto_decline_consulting = false`, `auto_archive_transactional = false`, `auto_archive_bot_noise = false`, `auto_archive_always_archive = false`, `auto_archive_recent_unsubs = false`, `skip_security_alerts = true`, `skip_pdf_invoices = true`.
- Add matching fields on `TriageConfig`.
- Validate `"automation"` in `_validate_toml_sections`, parse it in `load_config` (validating `interval_minutes` in `1..1440`), render it in `render_config_toml`, and update it in `update_config_file` with `_validate_int_range("automation.interval_minutes", ..., 1, 1440)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_settings_and_sender_memory.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nollpost/config.py tests/test_settings_and_sender_memory.py
git commit -m "feat(config): add [automation] schedule and autopilot settings"
```

---

### Task 2: SQLite Store Extensions (`action_log.source`, `app_state`, Digest & `auto_status.json`)

**Files:**
- Modify: `src/nollpost/store.py:112-203,328-370,645-755`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `TriageStore`, `action_log`, `sender_memory` in `src/nollpost/store.py`.
- Produces:
  - `TriageStore.log_action(..., source: str = "manual") -> int`
  - `TriageStore.list_action_log(..., source: str = "all") -> dict[str, Any]`
  - `TriageStore.list_sender_memory(..., filter_kind: str = "all")` supporting `"auto_archive"`
  - `TriageStore.get_app_state(key: str, default: str = "") -> str`
  - `TriageStore.set_app_state(key: str, value: str) -> None`
  - `TriageStore.has_auto_failure(account: str, internal_id: str, rfc_message_id: str, action: str) -> bool`
  - `TriageStore.record_auto_run_status(status: str, summary: dict[str, int], error: str = "", warnings: Optional[list[str]] = None, status_file: Optional[Path] = None) -> dict[str, Any]`
  - `TriageStore.get_auto_digest(limit: int = 100) -> dict[str, Any]`
  - `TriageStore.ack_auto_digest(max_log_id: Optional[int] = None, status_file: Optional[Path] = None) -> dict[str, Any]`

- [ ] **Step 1: Write the failing tests**

In `tests/test_store.py`, add:
- `test_sender_memory_filter_auto_archive(tmp_path)`: verifies `record_sender_preference(..., preferred_action="auto_archive", keep_subscribed=True)` appears under `list_sender_memory(filter_kind="auto_archive")` and `"learned"`.
- `test_auto_digest_ack_and_status_file(tmp_path)`: verifies `log_action(..., source="auto")` increments `unseen_count` in `get_auto_digest()` and atomic `auto_status.json`; manual logs (`source="manual"`) are excluded from digest and included/filtered properly in `list_action_log(source="auto")`; `ack_auto_digest()` sets `auto_digest_ack_log_id` monotonically, empties `get_auto_digest()["items"]`, and sets `unseen_count == 0` in `auto_status.json`.
- `test_has_auto_failure_suppresses_repeats(tmp_path)`: verifies `has_auto_failure` returns `True` after a `source="auto"`, `status="failed"` (or `"blocked_captcha"`) row for `(account, internal_id, rfc_message_id, action)`, and `False` for `status="ok"` or `source="manual"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_store.py -k "auto_archive or auto_digest or has_auto_failure" -v`
Expected: FAIL with `TypeError` / `AttributeError`.

- [ ] **Step 3: Implement schema migration, `app_state`, digest, and failure-check methods in `src/nollpost/store.py`**

- Migrate `action_log` with `source TEXT NOT NULL DEFAULT 'manual'` if missing; add `CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)`.
- Add `source` support to `log_action` and `list_action_log`, plus `filter_kind == "auto_archive"` to `list_sender_memory`.
- Implement `get_app_state`, `set_app_state`, `has_auto_failure`, `get_auto_digest`, `record_auto_run_status`, `ack_auto_digest`, and atomic `.tmp` -> `.replace` writer for `auto_status.json`.
- Notice `ack_auto_digest` must only advance `auto_digest_ack_log_id` (`max(current_ack, target_ack)`) so concurrent writes never move it backward.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nollpost/store.py tests/test_store.py
git commit -m "feat(store): add auto-triage digest state, failure tracking, and status file"
```

---

### Task 3: Classifier Precedence for `auto_archive` & Guardrails

**Files:**
- Modify: `src/nollpost/classifier.py:98-275`
- Test: `tests/test_classifier.py`

**Interfaces:**
- Consumes: `SenderMemoryRecord.preferred_action` (`"auto_archive"`, `"archive"`, `"always_keep"`), `TriageConfig.auto_archive_always_archive`, `TriageConfig.auto_skip_pdf_invoices` from Tasks 1–2.
- Produces: `ClassifiedItem` with `category="newsletters"`, `subcategory="Auto-Archive"` (or `"Remembered Archive"`), `default_action="archive"`, `learned_preference=sender_mem.preferred_action`, badge `"Auto-Archive"` when `auto_archive` (or `archive` with `auto_archive_always_archive`) applies after critical guardrails.

- [ ] **Step 1: Write the failing tests**

In `tests/test_classifier.py`, add:
- `test_auto_archive_overrides_plain_bank_alert_and_bill_keywords()`: bank notification from `alerts@mybank.example` with no `List-Unsubscribe` header and subject `"Card payment receipt"` + sender memory `preferred_action="auto_archive"` classifies with `default_action == "archive"` and `"Auto-Archive"` badge.
- `test_auto_archive_respects_security_pdf_and_gov_guardrails()`: same `auto_archive` sender sending `"Security Alert: New login detected"`, a PDF attachment (`pdf_attachments=[...]`), or a `matched_gov_pack` (`"kivra"`) stays in `category == "attention"` and is NOT defaulted to `"archive"`.
- `test_auto_archive_does_not_override_always_keep_or_consulting()`: `Consulting Match` and `Consulting Mismatch` keep `category == "consulting"`.
- `test_always_archive_with_auto_flag_applies_to_non_newsletter()`: `preferred_action == "archive"` with `config.auto_archive_always_archive = True` classifies non-newsletter notification as `default_action == "archive"` (still respecting security/PDF/gov guardrails).

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_classifier.py -k "auto_archive or always_archive_with_auto" -v`
Expected: FAIL (returns `attention` or `Direct Human` / `no_action`).

- [ ] **Step 3: Update `classify_email` in `src/nollpost/classifier.py`**

- Compute `matched_gov_pack`, `is_security`, `is_explicit_bill_sender`, `bill_kw`, `is_retail_promo_noise` cleanly inside Step 3.
- Keep `is_security`, `extracted.pdf_attachments` (when `config.auto_skip_pdf_invoices` is True, or always in `attention`), and `matched_gov_pack is not None` returning `category="attention"` immediately.
- Insert Step 3b right before `is_explicit_bill_sender` / `bill_kw` fallback: if `sender_mem` has `preferred_action == "auto_archive"` or (`config.auto_archive_always_archive` and `sender_mem.preferred_action == "archive"`), return `ClassifiedItem` with `category="newsletters"`, `subcategory="Auto-Archive"` (or `"Remembered Archive"`), `default_action="no_action" if (header.is_read or catchup_mode) else "archive"`, `allowed_actions=_base_allowed_actions(...)`, badge `"Auto-Archive"`, `learned_preference=sender_mem.preferred_action`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_classifier.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nollpost/classifier.py tests/test_classifier.py
git commit -m "feat(classifier): add safe auto_archive precedence over generic notices"
```

---

### Task 4: Headless Auto-Runner, Lock Coordination, and `nollpost auto` CLI

**Files:**
- Create: `src/nollpost/auto_runner.py`
- Modify: `src/nollpost/executor.py:23-60,300-660` (pass `source: str = "manual"` into `store.log_action`)
- Modify: `src/nollpost/cli.py:55-285`
- Test: `tests/test_auto_runner.py`

**Interfaces:**
- Consumes: `TriageConfig`, `TriageStore` (Tasks 1–2), `classify_email` (Task 3), `run_scan`, `BatchExecutor.execute(..., source=...)`.
- Produces:
  - `acquire_run_lock(data_dir: Path)` context manager yielding `bool` (`True` if lock acquired via `fcntl.flock(..., LOCK_EX | LOCK_NB)`, `False` if busy)
  - `select_auto_actions(items: list[ClassifiedItem], config: TriageConfig, store: TriageStore) -> tuple[list[tuple[ClassifiedItem, str]], list[str]]`
  - `run_auto_triage(config: TriageConfig, store: TriageStore, bridge: AppleMailBridge, *, dry_run: bool = False, headless: bool = True, mail_running_checker: Callable[[], bool] = ...) -> dict[str, Any]`
  - CLI command `nollpost auto [--dry-run] [--json]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auto_runner.py` covering:
- `test_select_auto_actions_rules_and_guardrails`: tests `auto_archive` sender, `Consulting Mismatch` with `decline_url` (`decline_and_archive`), `Consulting Mismatch` without `decline_url` (`send_decline_and_archive`), `Consulting Match` (excluded), `Transactional` / `Bot Noise` / `Unsubbed recently` (respecting config flags), read/non-INBOX items (excluded), and `store.has_auto_failure(...)` suppression.
- `test_select_auto_actions_skips_gmail_when_ui_menu`: with `gmail_archive_strategy="ui_menu"`, Gmail items are skipped with a warning while iCloud items remain selected.
- `test_run_auto_triage_dry_run_and_execute_writes_digest_and_status`: fake bridge + `tmp_path` config/data dirs; verifies `--dry-run` makes zero Mail moves and zero `action_log` rows; real run executes, writes `source="auto"` rows (not `missed_unsubscribes`), and updates `auto_status.json`.
- `test_run_auto_triage_skips_when_mail_not_running_or_lock_held`: verifies `status == "skipped"`, `reason == "mail_not_running"` and `reason == "busy"` when `run.lock` is held.
- `test_cli_auto_json(tmp_path, monkeypatch, capsys)`: runs `cli.main(["auto", "--dry-run", "--json"])` with monkeypatched bridge/mail checker and verifies valid JSON output.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_auto_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nollpost.auto_runner'`.

- [ ] **Step 3: Implement `BatchExecutor(..., source="manual")`, `src/nollpost/auto_runner.py`, and `cli.py` `auto` command**

- Add `source: str = "manual"` parameter to `BatchExecutor.execute(...)` (defaulting `"manual"` so all existing callers stay identical) and forward it to `self.store.log_action(..., source=source)`.
- Implement `src/nollpost/auto_runner.py` with `acquire_run_lock`, `select_auto_actions`, and `run_auto_triage` (sync + async helper `async_run_auto_triage` for FastAPI).
- Add `auto` subcommand (`--dry-run`, `--json`) to `src/nollpost/cli.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_auto_runner.py tests/test_audit_log_undo.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nollpost/auto_runner.py src/nollpost/executor.py src/nollpost/cli.py tests/test_auto_runner.py
git commit -m "feat(auto): add headless nollpost auto runner, run lock, and CLI command"
```

---

### Task 5: Web Server Auto-Triage APIs, Preferences, Settings & Lock Guard

**Files:**
- Modify: `src/nollpost/web.py:181-195,246-299,483-630,919-932,1070-1082`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `auto_runner.async_run_auto_triage`, `acquire_run_lock` (Task 4), `TriageStore` digest/ack/source methods (Task 2), `TriageConfig` automation fields (Task 1).
- Produces:
  - `POST /api/preferences` and `POST /api/sender-memory/bulk` accepting `"auto_archive"`
  - `GET /api/settings` & `PUT /api/settings` exposing/updating `"automation"`
  - `GET /api/audit-log?source=all|manual|auto`
  - `POST /api/auto/run`
  - `GET /api/auto/digest`
  - `POST /api/auto/ack`

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py`, add API tests:
- `test_auto_preferences_and_settings_endpoints`: sets `auto_archive` via `/api/preferences` and `/api/sender-memory/bulk`, filters `/api/sender-memory?kind=auto_archive`, reads and updates `[automation]` via `GET/PUT /api/settings`.
- `test_auto_run_digest_and_ack_endpoints`: runs `POST /api/auto/run`, verifies `GET /api/auto/digest` returns unacknowledged items and counts, verifies `GET /api/audit-log?source=auto` filters correctly, and calls `POST /api/auto/ack` -> subsequent `GET /api/auto/digest` returns `unseen_count == 0` and empty `items` while `auto_status.json` is updated.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_web.py -k "auto_preferences or auto_run_digest" -v`
Expected: FAIL with 400 / 404.

- [ ] **Step 3: Implement endpoints and `run.lock` integration in `src/nollpost/web.py`**

- Update `SettingsUpdateRequest`, `_settings_payload`, `api_preferences`, `api_bulk_sender_memory`, `api_audit_log`.
- Add `POST /api/auto/run`, `GET /api/auto/digest`, `POST /api/auto/ack` (using `runtime.scan_lock` + `acquire_run_lock` when scanning/executing).
- Also acquire `acquire_run_lock` inside `get_snapshot` / batch execution / undo around Mail.app access so a concurrent CLI `nollpost auto` can't collide with an active server operation.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nollpost/web.py tests/test_web.py
git commit -m "feat(web): add auto-triage digest, ack, run, and automation settings APIs"
```

---

### Task 6: Web UI — Digest Banner, One-Click `Auto (Archive & Summarize)`, Settings & i18n

**Files:**
- Modify: `src/nollpost/static/index.html:54-65`
- Modify: `src/nollpost/static/style.css`
- Modify: `src/nollpost/static/app.js`
- Modify: `src/nollpost/static/locales/en.json`
- Modify: `src/nollpost/static/locales/sv.json`
- Test: `tests/test_web.py`, `tests/test_i18n.py`

**Interfaces:**
- Consumes: `GET /api/auto/digest`, `POST /api/auto/ack`, `POST /api/audit-log/{id}/undo`, `POST /api/preferences` (`"auto_archive"`), `GET/PUT /api/settings` (`automation`), `GET /api/audit-log?source=...`.
- Produces: `#auto-digest-banner` with explicit **"Mark summary seen"** button, `Auto (Archive & Summarize)` in row `Remember...` + Sender Rules modal, `[automation]` section in Settings modal, `"Auto only"` filter in Audit Log, and synced `en.json` / `sv.json` keys.

- [ ] **Step 1: Write the failing browser/UI & i18n tests**

In `tests/test_web.py`, add Playwright Chromium test `test_auto_digest_banner_and_one_click_rule_in_chromium`:
- Loads board when unseen `auto` actions exist -> asserts `#auto-digest-banner` is visible with item summary and Undo button, reloads page -> asserts banner persists, clicks **"Mark summary seen"** -> asserts `POST /api/auto/ack` fires and banner hides.
- Selects `auto_archive` in a row's `.pref-select` -> asserts `POST /api/preferences` sends `"auto_archive"`.
- Opens Settings modal -> toggles `automation.auto_decline_consulting` and sets `interval_minutes = 30` -> saves and verifies reload reflects it.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_web.py -k "auto_digest_banner" -v`
Expected: FAIL (missing `#auto-digest-banner` / options).

- [ ] **Step 3: Implement HTML, CSS, JS, and `en.json` / `sv.json` translations**

- Add `#auto-digest-banner` container in `index.html` and calm banner/pill styling in `style.css`.
- Implement digest fetch, rendering, expand/collapse, Undo, and **"Mark summary seen"** (`POST /api/auto/ack`) in `app.js`.
- Add `auto_archive` (`Auto (Archive & Summarize)`) option to row `.pref-select`, Sender Rules modal (tab, badge, single/bulk dropdowns), Audit Log modal (`source` filter), and Settings modal (`[automation]` controls).
- Add all new `t(...)` / `data-i18n` keys to both `en.json` and `sv.json`.

- [ ] **Step 4: Run Playwright UI and i18n tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_web.py tests/test_i18n.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nollpost/static/index.html src/nollpost/static/style.css src/nollpost/static/app.js src/nollpost/static/locales/en.json src/nollpost/static/locales/sv.json tests/test_web.py
git commit -m "feat(ui): add auto-triage digest banner, one-click auto_archive rule, and settings"
```

---

### Task 7: macOS Menu Bar Scheduler, `auto_status.json` Watcher & Notification Dot

**Files:**
- Modify: `macos/NollpostMenuBar/main.swift`
- Test: `tests/test_packaging.py` (or `tests/test_macos_app_build.py` verifying `scripts/build_macos_app.py --arch host --no-zip` compiles cleanly)

**Interfaces:**
- Consumes: `config.toml` `[automation]` (`enabled`, `interval_minutes`), `<data_dir>/auto_status.json`, `nollpost auto --json`, `POST /api/auto/run` on running server URL.
- Produces: Interval timer + wake-catchup scheduler in `NollpostMenuApp`, `Run Auto-Triage Now` & `Pause Auto-Triage` menu items, `Auto-Triage: ...` status item, `DispatchSource` watcher on `<data_dir>` for `auto_status.json`, and notification dot drawn onto `makeSnabelNollMenuIcon` when `unseen_count > 0`.

- [ ] **Step 1: Write the failing build/packaging check**

In `tests/test_packaging.py` (or verify existing macOS build test), add a macOS-only test `test_menubar_swift_compiles_with_automation_ui()` asserting `build_app_bundle(..., arch="host", make_zip=False)` succeeds and the compiled binary exists, plus checking `main.swift` handles `auto_status.json` parsing. (Verify current `tests/test_packaging.py` first before adding.)

- [ ] **Step 2: Implement scheduler, `auto_status.json` watcher, menu items, and notification dot in `macos/NollpostMenuBar/main.swift`**

- Generalize `userSectionConfigValue` into `sectionConfigValue(section: String, key: String) -> String?` so `[automation]` `enabled` and `interval_minutes` (default `60`, clamped `1...1440`) are read cleanly, alongside `resolvedDataDir()` (`NOLLPOST_DATA_DIR`, `~/Library/Application Support/nollpost`, legacy fallback).
- Add `AutoStatusSnapshot` (`Codable`) parsing `auto_status.json`, watched via `DispatchSource.makeFileSystemObjectSource` on `dataDir` + refreshed on `menuWillOpen` and after every auto run.
- Draw a crisp notification dot in `makeSnabelNollMenuIcon(strokeFractions:..., showBadgeDot: Bool)` when `unseenCount > 0 && serverState != .starting`, and update `statusItem.button?.toolTip`.
- Schedule `autoTriageTimer` at `interval_minutes * 60` seconds and listen to `NSWorkspace.shared.notificationCenter` for `didWakeNotification` to run a catch-up tick if `last_run_at` is older than the interval.
- Route auto-triage execution:
  - Skip silently if paused (`UserDefaults`), disabled in `config.toml`, Mail.app isn't running, or `queryMailAutomationPermission(prompt: false)` is not `.granted` (unless user clicked **Run Auto-Triage Now**, which can prompt/ensure Mail).
  - If `serverState` is `.running(url)`, send `POST /api/auto/run` with `X-Triage-Key`.
  - If stopped/idleStopped/failed, run `nollpost auto --json` via `Process` and reload `auto_status.json`.

- [ ] **Step 3: Build & verify macOS app + run full test suite and hygiene checks**

Run:
- `.venv/bin/python scripts/build_macos_app.py --arch host --no-zip`
- `PYTHONPATH=src .venv/bin/pytest tests -v`
- `.venv/bin/ruff check src tests scripts`
- `.venv/bin/python scripts/check_hygiene.py`
Expected: All compile, lint, hygiene, and pytest checks PASS.

- [ ] **Step 4: Commit**

```bash
git add macos/NollpostMenuBar/main.swift tests/test_packaging.py
git commit -m "feat(menubar): add hourly auto-triage scheduler, status watcher, and badge dot"
```
