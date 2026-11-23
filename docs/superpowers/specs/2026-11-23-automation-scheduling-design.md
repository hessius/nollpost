# Nollpost Scheduled Automation & Menu Bar Integration Design

**Date:** 2026-11-23  
**Status:** Ready for User Approval (Builder Check Complete)

---

## 1. Intent & Why

Nollpost currently operates purely on demand: the user must remember to run `nollpost scan` or open the Review Board via `nollpost serve` / `Nollpost.app`, review every item, and manually click Execute. Even after setting a sender to `Always Archive` or configuring consulting mismatches, nothing happens until the user initiates a session—and `Nollpost.app` shuts the local server down after 45 minutes of inactivity.

The goal of this design is to make Nollpost handle predictable, low-risk email streams automatically on a configurable schedule while the menu bar app is running, while keeping the user informed through a calm **menu bar dot + Web UI digest** workflow:

- **Consulting refusals** (`Consulting Mismatch`) are declined automatically (clicking the `Decline` URL or sending the configured boilerplate reply) and archived.
- **Un-opt-out-able notifications** (such as bank alerts, transaction notices, or delivery updates) can be enrolled with **one click** in the UI to auto-archive and appear in an automatic summary digest, so the user still gets notice of what happened without inbox clutter.
- **Menu bar (`Nollpost.app`)** owns the timer (default: once per hour) and displays a subtle notification dot on the Snabel-noll icon when unacknowledged automatic actions have happened since the last summary was dismissed.
- **Web UI (Review Board)** presents the full "Handled Automatically" summary/digest, provides one-click **Undo**, and hosts the one-click sender rules and category autopilot settings.

---

## 2. Goals, Non-Goals, and Anti-Goals

### Goals
1. **Hands-free scheduled execution** on a configurable interval (default `60` minutes), active **only while `Nollpost.app` is running**.
2. **One-click automation rule creation** right where emails appear in the Review Board (extending the existing `"Remember..."` selector and **Sender Rules** modal with `Auto (Archive & Summarize)`), plus category-level autopilot toggles in **Settings**.
3. **Clear surface split:**
   - **Menu bar (`Nollpost.app`):** Timer scheduling, server/CLI orchestration, and a lightweight notification dot + status line indicating unseen automatic actions.
   - **CLI / Server (`nollpost`):** Scanning, safe rule evaluation, execution (`BatchExecutor`), SQLite persistence, and a standalone `nollpost auto` command (`--dry-run`, `--json`).
   - **Web UI (Review Board):** Digest presentation of automatic actions taken since last visit, one-click Undo, and rule/autopilot configuration.
4. **Zero-deletion & safety preservation:** All automatic actions remain strictly Archive-only (`0 deleted`), honor `Security Alert` and `PDF Invoice` safety guardrails, and write to `action_log` with full Undo support.

### Non-Goals
- Running background schedules via `launchd` when `Nollpost.app` is quit.
- Custom multi-condition rule builders (subject regexes, boolean AND/OR trees) in the UI—one-click sender/domain preferences and category switches are sufficient.
- Rendering full email lists or interactive undo tables inside native Swift menu bar menus.
- Automatic unsubscribing (`archive_and_unsub`) of general newsletters without user review; autopilot focuses on predictable archive-only streams, consulting mismatch declines, and explicitly opted-in/already-unsubscribed senders.

### Anti-Goals
- **Never delete messages** or route around Gmail's Archive-only guarantees.
- **Never auto-archive security alerts or PDF invoices/bills** by accident through a broad sender/category rule.
- **Never keep a heavy Python/uvicorn server resident 24/7** when the user isn't looking at the Review Board.
- **Never run two concurrent Mail.app / Playwright sessions** (e.g., an hourly `nollpost auto` colliding with an active Review Board scan or execution).

---

## 3. Constraints

1. **macOS TCC Automation permissions:** Apple Mail (`com.apple.mail`) Automation permission is granted by macOS to the launching app bundle (`Nollpost.app`, via `NSAppleEventsUsageDescription` in `macos/NollpostMenuBar/Info.plist`). Running scheduled tasks as child processes (or HTTP requests) from `Nollpost.app` ensures all Mail.app scripting uses that single trusted permission context.
2. **Mail.app availability:** Scheduled runs should only proceed if `Mail.app` is running (or should skip cleanly without popping dialogs or launching Mail unexpectedly in the background while the user is away—unless user triggers manually).
3. **Existing idle-timeout model:** `nollpost serve` stops after `idle_timeout_minutes` (default `45`) of HTTP inactivity unless disabled. Scheduled automation must preserve this rather than forcing the web server to stay resident continuously.
4. **Concurrency:** Only one process may scan or mutate `Mail.app` / `state.db` / Playwright Chromium at a time.

---

## 4. Approaches Considered

### Approach A: Headless One-Shot CLI Runner (`nollpost auto`) triggered by `Nollpost.app` *(Selected)*
- **Mechanism:** `Nollpost.app` runs the configurable interval timer. When a tick fires:
  - If the Review Board server is **stopped / idleStopped / failed**, `Nollpost.app` spawns `nollpost auto --json`, which scans `INBOX`, filters items eligible for autopilot, executes them via `BatchExecutor`, logs results to SQLite (`action_log` + digest tracking), updates a tiny status marker JSON file, and exits.
  - If the Review Board server is **already running**, `Nollpost.app` calls `POST /api/auto/run` on the live `127.0.0.1` server (authenticated with its ephemeral session token) instead of spawning a second Python process.
- **Trade-offs:** Preserves zero idle memory footprint and the 45-minute idle auto-stop design, provides a first-class testable/scriptable `nollpost auto` CLI command (`--dry-run`, `--json`), and keeps TCC under `Nollpost.app`. Requires a lightweight file lock (`flock`) between `nollpost auto` and `nollpost serve` plus a small status file so `Nollpost.app` can detect unseen counts and digest acknowledgements immediately.

### Approach B: Always-On Background Server (`nollpost serve --idle-timeout 0`)
- **Mechanism:** `Nollpost.app` keeps `nollpost serve` running continuously and lets FastAPI run an in-process `asyncio` hourly loop, while `Nollpost.app` polls `/api/auto/status`.
- **Trade-offs:** Simplest single-process concurrency and warm Review Board cache, but keeps Python + uvicorn + Playwright overhead resident in memory all day and conflicts with Nollpost's idle-timeout model.

### Approach C: `Nollpost.app` Wakes Full `nollpost serve` on Every Tick
- **Mechanism:** Each hourly tick starts `nollpost serve --no-open`, hits an endpoint, and lets it idle-shutdown later.
- **Trade-offs:** Unnecessary HTTP server startup latency and blurs user-opened Review Board sessions with background ticks.

**Why Approach A:** It gives the cleanest separation of concerns—`Nollpost.app` is the lightweight macOS timer & indicator, `nollpost auto` is the clean headless execution unit, and `nollpost serve` stays strictly on-demand for interactive review.

---

## 5. Detailed Design

### 5.1 What Can Be Automated & Safety Rules

#### A. Per-Sender One-Click Rule (`sender_memory`)
Extend `sender_memory.preferred_action` (currently `always_keep`, `archive`, `mark_read`, or `None`) with a new value:
- **`auto_archive`** *(UI label: `Auto (Archive & Summarize)`)*

Behavior when `sender_mem.preferred_action == "auto_archive"` (and `always_archive` when `auto_archive_always_archive` is enabled):
1. **Exact `classify_email` Precedence *(Builder-check call)*:**
   - **Step 1 (`always_keep`)**: Remains highest priority; never automated.
   - **Step 2 (`consulting`)**: Evaluated before `auto_archive` so `Consulting Match` is never silenced and `Consulting Mismatch` follows consulting-decline rules.
   - **Step 3 (`attention` critical safety guardrails)**: `is_security`, `extracted.pdf_attachments` (when `skip_pdf_invoices = true`), and `matched_gov_pack` (e.g., Gov/Kivra notices) remain protected in `attention` and are **never** overridden by `auto_archive` or `auto_archive_always_archive`.
   - **Step 3b (`auto_archive` / `auto_archive_always_archive` override)**: Evaluated immediately *after* those protected checks and *before* `is_explicit_bill_sender`, generic `bill_keywords`, GitHub, Wanted, Transactional, Newsletters, and Step 9 (`Unclassified Automated` / `Direct Human`). This ensures plain bank/account notifications—which often lack newsletter headers or match generic `bill_keywords` like `payment` or `receipt`—still match cleanly with `default_action = "archive"`, `category = "newsletters"`, `subcategory = "Auto-Archive"`, and an `Auto-Archive` badge.
   - Likewise, when `auto_archive_always_archive = true`, senders with `preferred_action == "archive"` ("Always Archive") are honored at Step 3b across non-newsletter messages too (subject to the same critical safety guardrails above).

#### B. Category-Level Autopilot Settings (`[automation]` in `config.toml`)
All category autopilot switches are **opt-in** (`false` by default for safety, configurable in Settings UI and `config.toml`), except when the user explicitly enables them or uses `auto_archive` per sender:

```toml
[automation]
enabled = true
interval_minutes = 60
auto_archive_sender_rules = true        # Execute sender_memory "auto_archive" rules
auto_decline_consulting = false         # Consulting Mismatch -> decline_and_archive or send_decline_and_archive
auto_archive_transactional = false      # Shipping/order notifications (e.g. PostNord, Budbee, order confirmations)
auto_archive_bot_noise = false          # GitHub bot / CI notifications (dependabot, vercel[bot], etc.)
auto_archive_always_archive = false     # Also auto-run senders marked "Always Archive"
auto_archive_recent_unsubs = false      # Auto-archive newsletters from senders unsubscribed within 48h grace period
skip_security_alerts = true             # Never auto-archive security alerts
skip_pdf_invoices = true                # Never auto-archive messages with PDF attachments
```

**Consulting Mismatch Automation (`auto_decline_consulting = true`):**
- Only triggers for items classified as `Consulting Mismatch` (i.e., `consulting_enabled = true`, no positive `consulting_match_keywords` match, and unread in `INBOX`).
- Items classified as `Consulting Match` (positive keyword match, or mixed positive+negative review match) are **never** auto-declined and remain in `INBOX`.
- Execution action for `Consulting Mismatch` when `auto_decline_consulting` is enabled:
  - If `extracted.consulting_links.decline_url` is present -> `decline_and_archive` (Playwright clicks Decline URL, then archives).
  - If no `decline_url` is present -> `send_decline_and_archive` (Mail.app sends `consulting_decline_boilerplate` silently and archives).
  - *Note:* In interactive manual review (`nollpost serve`), link-less mismatches still default to `draft_decline` unless the user picks `send_decline_and_archive` or enables `auto_decline_consulting`.
  - If Playwright fails on `decline_url` (e.g., login wall / timeout) or `send_decline_and_archive` fails: *(Builder-check call)* the message stays unread in `INBOX` and is logged in `action_log` (`source = 'auto'`, with `detail`, `opened_target`, and `screenshot_path`) so it surfaces once in the **"Handled Automatically"** digest for manual review—it is **not** inserted into `missed_unsubscribes` (which remains dedicated to unsubscribe retries/exports).

**Failure, Retry & `ui_menu` Safety Policy *(Builder-check calls)*:**
- **No automatic retry loops across hourly ticks:** `select_auto_actions` skips any unread `INBOX` message that already has a prior `source = 'auto'` non-`ok` entry (`failed`, `blocked_captcha`, `timeout`, `unsupported`) in `action_log` for the same `(account, internal_id, rfc_message_id)` and action. That prevents re-running Playwright or re-attempting outbound `send_decline_and_archive` replies every hour and keeps `unseen_count` from inflating repeatedly.
- **`gmail_archive_strategy = "ui_menu"` guard:** Because `ui_menu` activates `Mail.app` and opens windows on screen (requiring Accessibility UI scripting), `nollpost auto` skips Gmail auto-archiving when `gmail_archive_strategy == "ui_menu"` and emits a warning in `auto_status.json` / the digest recommending `"background"` (or `"tag_staging"`) for unattended Gmail automation. Non-Gmail accounts proceed normally.

**Summary / Digest Eligibility:**
- Every action executed by an automated run (`auto_run`) is recorded in SQLite so the Web UI digest can show:
  - **What was declined & archived** (sender, subject, decline method: link vs boilerplate reply).
  - **What was archived as notice** (sender, subject, rule/category reason—e.g. `Auto-Archive rule` for bank notifications, `Transactional`, `Bot Noise`).
  - **Any failures / skipped items** (e.g. decline link failed, kept in `INBOX`).

---

### 5.2 CLI & Backend (`nollpost`)

#### A. New CLI Subcommand: `nollpost auto`
Add `auto` to `src/nollpost/cli.py`:
- `nollpost auto [--dry-run] [--json]`
- Behavior:
  1. Checks if `Mail.app` is running (if not, exits cleanly with `{"status": "skipped", "reason": "mail_not_running"}` and exit code `0`—does **not** launch Mail.app unexpectedly in the background).
  2. Acquires an exclusive non-blocking file lock (`<data_dir>/run.lock` via `fcntl.flock`). If another scan/execution (`nollpost serve` or another `nollpost auto`) holds the lock, exits cleanly with `{"status": "skipped", "reason": "busy"}` (exit code `0`).
  3. Runs `run_scan(config, store, bridge, mode="inbox", sync_mail=True)` (respecting existing `max_inbox_messages` and cache).
  4. Filters unread `INBOX` items matching active automation rules (`select_auto_actions(items, config)`).
  5. If `--dry-run`, skips execution and prints planned actions.
  6. Otherwise runs `BatchExecutor.execute(...)` with `run_id = "auto-<timestamp>-<shorthex>"` (or records `source = "auto"` in SQLite).
  7. Updates the auto-digest state in SQLite + writes `<data_dir>/auto_status.json` atomically.
  8. Prints human summary or JSON report (`executed`, `archived`, `declined`, `failed`, `unseen_count`, `last_run_at`).

#### B. Concurrency Coordination with `nollpost serve`
- `AppRuntime` in `web.py` acquires the same `fcntl.flock` around Mail.app scan/execute operations (or `Nollpost.app` checks `serverState` and uses `POST /api/auto/run` whenever `serverState == .running`).
- Using **both** (menu bar routes to `POST /api/auto/run` when server is up, and file lock guards against edge-case races like user clicking "Open Review Board" right as a background `nollpost auto` finishes) guarantees zero concurrent Mail.app / SQLite / Playwright contention.

#### C. SQLite Persistence & Status Marker (`store.py`)
1. **`action_log` integration:**
   - Reuse existing `action_log` table (or add a lightweight `source TEXT NOT NULL DEFAULT 'manual'` column with `ALTER TABLE` migration, plus `auto_runs` / `app_state` key-value table):
     - `app_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)` storing:
       - `auto_last_run_at`
       - `auto_last_status` (`ok` / `skipped` / `error`)
       - `auto_last_error`
       - `auto_digest_ack_at` (or `auto_digest_ack_log_id`: highest `action_log.id` acknowledged by the user in the Web UI)
   - Unseen auto-actions = `SELECT COUNT(*) FROM action_log WHERE source = 'auto' AND id > COALESCE(auto_digest_ack_log_id, 0) AND status IN ('ok', 'failed')`.
   - Every auto-archived item remains 100% undoable via the existing `POST /api/audit-log/{entry_id}/undo` flow!
2. **Atomic Status Marker File (`<data_dir>/auto_status.json`):**
   - Written atomically (temp file + `os.replace`) whenever an auto run completes or the digest is acknowledged in the Web UI:
     ```json
     {
       "unseen_count": 4,
       "last_run_at": "2026-11-23T22:00:00+00:00",
       "last_status": "ok",
       "last_summary": { "archived": 3, "declined": 1, "failed": 0 },
       "ack_log_id": 142
     }
     ```
   - `Nollpost.app` can read this file directly in Swift (via `DispatchSourceFileSystemObject` / periodic check on menu open / after `nollpost auto` finishes) without spawning a Python interpreter just to check whether to show the dot.

#### D. Web API Endpoints (`web.py`)
Add endpoints protected by the existing session token middleware:
- `POST /api/auto/run` — Runs an auto-triage pass inside the active `AppRuntime` (shares `scan_lock` / `exec_lock`), returns summary + updates `auto_status.json`.
- `GET /api/auto/digest` — Returns unseen (and recent) `source = 'auto'` entries from `action_log` grouped by summary counts (`declined`, `archived`, `failed`) plus `unseen_count` and `last_run_at`.
- `POST /api/auto/ack` — Marks all auto-actions up to `max_id` as seen (`auto_digest_ack_log_id`), updates `auto_status.json` (`unseen_count = 0`), so the menu bar dot clears immediately.

---

### 5.3 Menu Bar App (`macos/NollpostMenuBar/main.swift`)

#### A. Configurable Interval Scheduler
- `Nollpost.app` reads `[automation]` (`enabled`, `interval_minutes`, default `60`) from `config.toml` (generalizing the existing `userSectionConfigValue` helper into a section-aware TOML key reader, or reading via `nollpost` config).
- Starts an interval timer when `Nollpost.app` launches (and resets/re-evaluates after `NSWorkspace.didWakeNotification` if the elapsed time since `last_run_at` exceeds `interval_minutes`, so sleeping laptops catch up cleanly after waking!).
- When the timer fires:
  - Checks `AutomationStatus` silently (`prompt: false`) and skips if permission is not granted or Mail.app is not running.
  - If `serverState` is `.running(url)`, sends `POST /api/auto/run` using the stored `url`'s `key` query parameter / `X-Triage-Key` header.
  - If `serverState` is `.stopped` / `.idleStopped` / `.failed`, spawns `nollpost auto --json` in the background, logs output to `menubar.log`, and refreshes the icon/menu state from `auto_status.json` (or the command's JSON stdout).
  - If `serverState` is `.starting`, defers until ready or skips until next tick.

#### B. Notification Dot & Menu UI
- **Snabel-noll Icon (`makeSnabelNollMenuIcon`):**
  - When `unseenCount > 0`, draw a small crisp status dot in the top-right corner of the 18x18 template/status image (or switch `isTemplate = false` only for an accent dot / keep monochrome template dot so it looks native in both Light and Dark macOS menu bars—let's keep native monochrome template dot by default or a clean red/accent badge dot).
  - Update `button.toolTip` (e.g. `"Nollpost — 4 auto-actions since last review"`).
- **Menu Items:**
  - Status line right under server state: e.g. `Auto-Triage: 4 new actions (Last: 22:00)` or `Auto-Triage: Idle (Every 60m)`.
  - **`Run Auto-Triage Now`** menu item (triggers an immediate `nollpost auto` or `POST /api/auto/run` pass, helpful for testing/manual catch-up).
  - **`Pause Auto-Triage`** toggle (persisted in `UserDefaults`, analogous to `Auto-Stop When Idle`, allowing quick temporary pause without editing `config.toml`).
- **Clearing the Dot:**
  - `Nollpost.app` watches `<data_dir>/auto_status.json` (via `DispatchSource.makeFileSystemObjectSource` on its parent directory + check on `menuWillOpen`), so as soon as the Web UI calls `POST /api/auto/ack`, `unseen_count` drops to `0` and the menu bar dot disappears right away.

---

### 5.4 Web UI (Review Board — `static/index.html`, `static/app.js`, `locales/*.json`)

#### A. "Handled Automatically" Digest Banner / Drawer
- On page load (and after `POST /api/auto/run`), the frontend fetches `GET /api/auto/digest` (keyed by `action_log.id > auto_digest_ack_log_id`).
- If there are unacknowledged automatic actions:
  - Renders a clean, collapsible **"Handled Automatically"** summary card at the top of the Review Board:
    - Headline counts, e.g.: **`4 handled automatically since your last visit`** · `1 consulting offer declined` · `3 notifications archived` (plus any failed count in warning style).
    - Compact expandable/inline list showing each item: **Sender**, **Subject**, **Rule/Action** (`Declined & Archived`, `Auto-Archived (Bank/Rule)`, etc.), timestamp, and one-click **Undo** (reusing `/api/audit-log/{id}/undo`) or **Change Rule** button.
  - The banner and the menu bar dot **persist across page refreshes and new sessions** until the user explicitly clicks **"Mark summary seen"** (or **Dismiss**), which calls `POST /api/auto/ack` (`auto_digest_ack_log_id`), sets `unseen_count = 0` in `auto_status.json`, and clears the menu bar dot. Historical entries remain inspectable in the **Audit Log** modal (with an `"Auto only"` filter chip).

#### B. One-Click Rule Definition
1. **On every email row (`Remember...` `<select class="pref-select">`):**
   - Add **`Auto (Archive & Summarize)`** (`value="auto_archive"`).
   - Selecting it immediately saves `preferred_action = "auto_archive"` via `POST /api/preferences` and shows a clear `Auto-Archive` pill on the row (and optionally offers a toast/hint: *"Future emails from this sender will be archived automatically and shown in your summary (Security Alerts & PDFs excluded)."*).
2. **In Sender Rules modal (`renderSenderRulesModalBody`):**
   - Add `Auto (Archive & Summarize)` filter tab (`kind="auto_archive"`), status pill, single-row edit option, and bulk action option.
3. **In Settings modal (`[automation]`):**
   - Controls for:
     - Enable scheduled auto-triage (`enabled`)
     - Run interval in minutes (`interval_minutes`, default `60`)
     - Toggle switches for category autopilot:
       - Auto-decline Consulting Mismatches (`auto_decline_consulting`)
       - Auto-archive Transactional / Delivery updates (`auto_archive_transactional`)
       - Auto-archive Bot / CI noise (`auto_archive_bot_noise`)
       - Auto-archive `"Always Archive"` senders (`auto_archive_always_archive`)
       - Auto-archive recent unsubscribes (`auto_archive_recent_unsubs`)
     - Safety guardrails (checked by default): Skip Security Alerts, Skip PDF Invoices/Attachments.

---

## 6. What's Left to the Builder

- Exact module layout for shared auto-selection/runner logic (e.g., `src/nollpost/auto_runner.py` imported by both `cli.py` and `web.py`).
- Exact SQLite migration steps in `TriageStore._init_schema()` and `app_state` helpers.
- Exact Swift CoreGraphics geometry for the Snabel-noll notification dot and `DispatchSource` / timer wake handling in `macos/NollpostMenuBar/main.swift`.
- Localization keys in `src/nollpost/static/locales/en.json` and `sv.json` (plus `tests/test_i18n.py` parity).
- Comprehensive unit & integration tests covering:
  - `auto_archive` classification + Security Alert / PDF safety overrides
  - `Consulting Mismatch` auto-decline (`decline_and_archive` vs `send_decline_and_archive`) vs `Consulting Match` preservation
  - `nollpost auto --dry-run --json` and `run.lock` concurrency behavior
  - `/api/auto/digest`, `/api/auto/ack`, `/api/auto/run`, and `auto_status.json` updates
  - `Nollpost.app` menu bar Swift helpers / config parsing if tested.
