# <img src="src/nollpost/static/favicon.svg" width="28" height="28" alt="Nollpost Snabel-noll logo" /> Nollpost

**Nollpost** (*Swedish: "zero mail"*) is a local-first, zero-token multi-inbox triage board for **macOS Apple Mail**, featuring a strictly Archive-only workflow, deterministic RFC 8058 + Playwright unsubscribe runner, and persistent sender preference memory.

> **Safety & Privacy First:** Nollpost runs entirely on your Mac (`127.0.0.1` with an ephemeral session token), sends zero emails to external LLMs/APIs, and **never deletes emails** or touches Trash/Junk folders. See [PRIVACY.md](PRIVACY.md) for details.

## Screenshots

*All screenshots below use simulated demo inbox data.*

![Nollpost Review Board showing multi-account triage, lifetime tallies, Safety Tray, and per-email action toggles](docs/screenshots/review-board.png)

<details>
<summary>More screenshots: Confirmation Preview &amp; Audit Log with one-click Undo Archive</summary>

![Confirmation modal showing 0 deleted emails guarantee, Archive destinations, and Unsubscribe targets](docs/screenshots/confirm-modal.png)

![Audit Log modal showing batch filters, execution outcomes, and one-click Undo Archive](docs/screenshots/audit-log.png)

</details>

## Requirements

- **macOS** with **Apple Mail** configured (iCloud, Gmail, Fastmail, IMAP, etc.)
- **Python 3.12+**
- **Playwright Chromium** (`playwright install chromium`)
- macOS **Automation** permission for your terminal/Python to control `Mail.app` (`System Settings → Privacy & Security → Automation`)
- *(Default Gmail mode only)* macOS **Accessibility** permission if using `gmail_archive_strategy = "ui_menu"` (`System Settings → Privacy & Security → Accessibility`)

## Installation

### Recommended: Homebrew (macOS Apple Silicon & Intel)

Homebrew is the **recommended** installation method: it manages the isolated Python 3.13 environment, Playwright Chromium browser binaries, and compiles the native `Nollpost.app` menu-bar launcher in one command.

```bash
brew tap hessius/tap
brew trust hessius/tap   # required once on Homebrew 7.0+
brew install hessius/tap/nollpost

# Optional: open the menu-bar launcher (starts Review Board, checks Mail permissions, supports Open at Login)
open "$(brew --prefix nollpost)/Nollpost.app"
```

See [docs/macos-distribution.md](docs/macos-distribution.md) for Homebrew tap release automation and update-check details.

### CLI-Only Alternatives (`uv` or `pipx`)

```bash
uv tool install git+https://github.com/Hessius/nollpost.git
# or: pipx install git+https://github.com/Hessius/nollpost.git

# Guided onboarding (installs Playwright Chromium, confirms Archive folders, writes config.toml)
nollpost setup

# Verify macOS Automation/Accessibility permissions and Chromium readiness
nollpost doctor
```

### From Source (Contributors)

```bash
git clone https://github.com/Hessius/nollpost.git
cd nollpost
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
nollpost setup
```

## Quick Start

```bash
# Verify environment & Mail.app permissions
nollpost doctor

# Scan all configured Apple Mail inboxes and print a terminal summary
nollpost scan

# Launch the local Review Board web UI in your browser
nollpost serve

# Scan Archive folders for repeat newsletters to unsubscribe from
nollpost scan --mode catchup

# View lifetime triage tallies or inspect failed/missed unsubscribe snapshots
nollpost stats
nollpost missed
```

On first run, Nollpost creates `~/.config/nollpost/config.toml` where you can customize scan limits, UI language (`ui_language = ""` auto-detects from your browser or supports `"en"` / `"sv"`, also configurable in **Settings**), per-account Archive folder overrides, wanted domains, and billing keywords.

## Gmail Archiving Strategies

Moving a message directly to `[Gmail]/All Mail` via Apple Mail AppleScript does not strip Gmail's `Inbox` label over IMAP. Nollpost supports three strategies under `[accounts]` in `config.toml`:

1. **`gmail_archive_strategy = "ui_menu"` (Default):** Opens the message in Apple Mail and clicks Mail's native **Archive** menu item, verifying that only the target message left `INBOX`. Requires macOS **Accessibility** permission.
2. **`gmail_archive_strategy = "two_hop"`:** Stages messages through a temporary `_ArchiveTmp` folder and flushes them to All Mail.
3. **`gmail_archive_strategy = "tag_staging"` (Fastest headless option):** Moves messages to a Gmail label `_ArchiveTmp` so a tiny Google Apps Script on your own Google account archives them cleanly in the background (no UI window flashing or Accessibility permission needed).

<details>
<summary>Optional: Setting up Google Apps Script for <code>tag_staging</code></summary>

1. Open [script.google.com](https://script.google.com) on your Google account and create a **New project**.
2. Paste the following script:

```javascript
function archiveLabeledEmails() {
  var labelName = '_ArchiveTmp'; // Replace with your exact label name
  var label = GmailApp.getUserLabelByName(labelName);

  if (!label) return; // Stops safely if the label name isn't found

  var threads = GmailApp.search('in:inbox label:' + labelName);

  for (var i = 0; i < threads.length; i++) {
    threads[i].moveToArchive();
    threads[i].removeLabel(label);
  }
}
```

3. Click **Run** once to grant the script permission to manage your Gmail threads.
4. Open **Triggers** (clock icon) → **Add Trigger** → run `archiveLabeledEmails` on a **Time-driven** schedule (e.g., every 5 minutes).
5. Set `gmail_archive_strategy = "tag_staging"` under `[accounts]` in `config.toml`.

</details>

## License & Notices

- Licensed under the [MIT License](LICENSE).
- Third-party notices and acknowledgements are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
