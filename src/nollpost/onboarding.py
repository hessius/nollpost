"""Interactive onboarding wizard (`setup`) and preflight diagnostics (`doctor`)."""

from __future__ import annotations

import platform
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from nollpost.config import (
    DEFAULT_CONFIG_TOML,
    VALID_GMAIL_ARCHIVE_STRATEGIES,
    TriageConfig,
    _deep_merge,
    render_config_toml,
    toml_str,
    toml_str_list,
)
from nollpost.mail_bridge import (
    AppleMailBridge,
    is_gmail_all_mail_destination,
    run_osascript,
    validate_archive_destination,
)
from nollpost.models import AccountInfo

APPS_SCRIPT_SNIPPET = """function archiveLabeledEmails() {
  var labelName = '_ArchiveTmp';
  var label = GmailApp.getUserLabelByName(labelName);
  if (!label) return;
  var threads = GmailApp.search('in:inbox label:' + labelName);
  for (var i = 0; i < threads.length; i++) {
    threads[i].moveToArchive();
    threads[i].removeLabel(label);
  }
}"""


@dataclass
class DoctorCheck:
    """Result of one preflight diagnostic check."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    remediation: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _query_chromium_executable() -> Path:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return Path(p.chromium.executable_path)


def _check_chromium_installed() -> tuple[bool, str]:
    """Return (installed, detail) for Playwright's expected Chromium binary."""
    try:
        try:
            exe = _query_chromium_executable()
        except RuntimeError:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                exe = pool.submit(_query_chromium_executable).result(timeout=15)
        if exe.exists():
            return True, str(exe)
        return False, f"Expected Chromium binary missing at {exe}"
    except Exception as exc:
        return False, f"Playwright Chromium unavailable ({exc})"


def _check_accessibility_enabled() -> tuple[bool, str]:
    try:
        out = run_osascript('tell application "System Events" to get UI elements enabled', timeout=10)
        if out.strip().lower() == "true":
            return True, "Accessibility UI scripting is enabled"
        return False, "Accessibility UI scripting is disabled"
    except Exception as exc:
        return False, f"Could not query Accessibility status: {exc}"


def _is_mail_running() -> bool:
    try:
        res = subprocess.run(["pgrep", "-x", "Mail"], capture_output=True, text=True, check=False)
        return res.returncode == 0
    except Exception:
        return False


def run_doctor(
    config: TriageConfig,
    bridge: AppleMailBridge,
    *,
    config_error: str = "",
    platform_name: Optional[str] = None,
    chromium_checker: Callable[[], tuple[bool, str]] = _check_chromium_installed,
    accessibility_checker: Callable[[], tuple[bool, str]] = _check_accessibility_enabled,
    mail_running_checker: Callable[[], bool] = _is_mail_running,
) -> tuple[bool, list[DoctorCheck]]:
    """Run preflight checks and return (all_critical_ok, checks)."""
    checks: list[DoctorCheck] = []
    if config_error:
        checks.append(
            DoctorCheck(
                "config",
                "fail",
                f"Existing config at {config.config_path} is invalid: {config_error}",
                "Fix the TOML syntax/values or run `nollpost setup` to regenerate a clean config.",
            )
        )
    sys_name = platform_name or platform.system()

    if sys_name == "Darwin":
        checks.append(DoctorCheck("macos", "ok", f"macOS ({platform.mac_ver()[0] or 'Darwin'})"))
    else:
        checks.append(
            DoctorCheck(
                "macos",
                "fail",
                f"Unsupported OS '{sys_name}' (v1.0 requires macOS + Apple Mail)",
                "Run Nollpost on macOS with Apple Mail configured.",
            )
        )

    if sys_name == "Darwin":
        if mail_running_checker():
            checks.append(DoctorCheck("mail_app", "ok", "Mail.app is running"))
        else:
            checks.append(
                DoctorCheck(
                    "mail_app",
                    "warn",
                    "Mail.app does not appear to be running",
                    "Open Apple Mail so scanning/archiving doesn't stall waiting for startup.",
                )
            )

    accounts: list[AccountInfo] = []
    if sys_name == "Darwin":
        try:
            accounts = bridge.list_accounts()
            if not accounts:
                checks.append(
                    DoctorCheck(
                        "mail_automation",
                        "warn",
                        "Apple Mail responded, but 0 accounts were found",
                        "Add at least one account in Mail → Settings → Accounts.",
                    )
                )
            else:
                summary = "; ".join(f"{a.name} ({a.inbox_mailbox} → {a.archive_mailbox})" for a in accounts)
                checks.append(DoctorCheck("mail_automation", "ok", f"{len(accounts)} account(s): {summary}"))
        except Exception as exc:
            err = str(exc)
            is_auth_err = "-1743" in err or "not authorized" in err.lower() or "privilege" in err.lower()
            remedy = (
                "Grant Automation permission in System Settings → Privacy & Security → Automation → allow your terminal/Python to control Mail."
                if is_auth_err
                else "Ensure Apple Mail is open, signed in, and responsive, then re-run `nollpost doctor`."
            )
            checks.append(DoctorCheck("mail_automation", "fail", f"Apple Mail check failed: {err}", remedy))

    chrome_ok, chrome_detail = chromium_checker()
    if chrome_ok:
        checks.append(DoctorCheck("chromium", "ok", f"Playwright Chromium ready ({chrome_detail})"))
    else:
        checks.append(
            DoctorCheck(
                "chromium",
                "fail",
                chrome_detail,
                "Run `playwright install chromium` (or `nollpost setup`).",
            )
        )

    has_gmail = any(is_gmail_all_mail_destination(a.archive_mailbox) for a in accounts)
    if sys_name == "Darwin" and config.gmail_archive_strategy == "ui_menu" and has_gmail:
        ax_ok, ax_detail = accessibility_checker()
        if ax_ok:
            checks.append(DoctorCheck("accessibility", "ok", ax_detail))
        else:
            checks.append(
                DoctorCheck(
                    "accessibility",
                    "warn",
                    f"{ax_detail} (needed for gmail_archive_strategy = \"ui_menu\")",
                    "Enable Accessibility for your terminal app in System Settings → Privacy & Security → Accessibility, or switch gmail_archive_strategy to \"tag_staging\" or \"two_hop\".",
                )
            )

    critical_ok = not any(c.status == "fail" for c in checks)
    return critical_ok, checks


def format_doctor_report(checks: list[DoctorCheck]) -> str:
    icons = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = ["Nollpost Preflight Check:"]
    for c in checks:
        lines.append(f"  [{icons.get(c.status, c.status.upper())}] {c.name}: {c.detail}")
        if c.remediation:
            lines.append(f"         Fix: {c.remediation}")
    return "\n".join(lines)


_toml_str = toml_str
_toml_str_list = toml_str_list
_render_config_toml = render_config_toml


def _install_chromium() -> int:
    proc = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    return int(proc.returncode)


def run_setup(
    config: TriageConfig,
    bridge: AppleMailBridge,
    *,
    yes: bool = False,
    skip_browser: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    chromium_checker: Callable[[], tuple[bool, str]] = _check_chromium_installed,
    browser_installer: Callable[[], int] = _install_chromium,
) -> int:
    """Interactive onboarding wizard writing validated config.toml."""
    output_fn("Nollpost Setup Wizard")
    output_fn(f"Config target: {config.config_path}\n")

    try:
        # 1. Playwright Chromium check/install
        if not skip_browser:
            chrome_ok, chrome_detail = chromium_checker()
            if chrome_ok:
                output_fn(f"[ok] Playwright Chromium already installed ({chrome_detail})")
            else:
                output_fn(f"[missing] Playwright Chromium not found ({chrome_detail})")
                do_install = (
                    True
                    if yes
                    else input_fn("Install Playwright Chromium now? [Y/n]: ").strip().lower() in ("", "y", "yes")
                )
                if do_install:
                    rc = browser_installer()
                    if rc != 0:
                        output_fn("Failed to install Playwright Chromium. Try `playwright install chromium` manually.")
                        return rc
                else:
                    output_fn("Skipped Chromium install. Run `playwright install chromium` before unsubscribing.")

        # Load baseline config preserving valid existing user settings (including legacy consulting keys)
        defaults = tomllib.loads(DEFAULT_CONFIG_TOML)
        if config.config_path.exists():
            try:
                existing = tomllib.loads(config.config_path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                output_fn(f"[warn] Existing config.toml could not be parsed ({exc}); starting from defaults.")
                existing = {}
            merged = _deep_merge(defaults, existing)
            ex_consult = existing.get("consulting", {}) if isinstance(existing.get("consulting"), dict) else {}
            ex_prof = existing.get("profile", {}) if isinstance(existing.get("profile"), dict) else {}
            ex_user = existing.get("user", {}) if isinstance(existing.get("user"), dict) else {}
            if not ex_consult.get("networks") and ex_prof.get("consulting_networks"):
                merged["consulting"]["networks"] = list(ex_prof["consulting_networks"])
            if not ex_consult.get("match_keywords") and ex_prof.get("consulting_match_keywords"):
                merged["consulting"]["match_keywords"] = list(ex_prof["consulting_match_keywords"])
            if not ex_consult.get("negative_keywords") and ex_prof.get("consulting_negative_keywords"):
                merged["consulting"]["negative_keywords"] = list(ex_prof["consulting_negative_keywords"])
            if not ex_consult.get("decline_boilerplate") and ex_user.get("consulting_decline_boilerplate"):
                merged["consulting"]["decline_boilerplate"] = str(ex_user["consulting_decline_boilerplate"])
            if "enabled" not in ex_consult and merged["consulting"].get("networks"):
                merged["consulting"]["enabled"] = True
        else:
            merged = defaults

        # Sanitize any pre-existing archive_map entries before querying Mail or persisting
        raw_archive_map = merged["accounts"].get("archive_map", {})
        archive_map: dict[str, str] = {}
        if isinstance(raw_archive_map, dict):
            for acct_name, box_name in raw_archive_map.items():
                try:
                    archive_map[str(acct_name)] = validate_archive_destination(str(box_name))
                except ValueError as err:
                    output_fn(f"[warn] Removed invalid archive_map entry '{acct_name}': {err}")
        if hasattr(bridge, "archive_overrides"):
            bridge.archive_overrides = dict(archive_map)

        # 2. Discover Apple Mail accounts & confirm Archive folders
        accounts: list[AccountInfo] = []
        try:
            accounts = bridge.list_accounts()
        except Exception as exc:
            output_fn(f"\n[warn] Could not query Apple Mail accounts: {exc}")
            output_fn("You can still finish setup now and re-run `nollpost setup` once Mail.app permissions are granted.")

        if accounts:
            output_fn("\nDiscovered Apple Mail accounts:")
            for acct in accounts:
                suggested = archive_map.get(acct.name) or acct.archive_mailbox
                if yes:
                    try:
                        chosen = validate_archive_destination(suggested)
                    except ValueError:
                        chosen = validate_archive_destination(acct.archive_mailbox)
                else:
                    while True:
                        ans = input_fn(f"  Archive mailbox for '{acct.name}' [{suggested}]: ").strip()
                        candidate = ans or suggested
                        try:
                            chosen = validate_archive_destination(candidate)
                            break
                        except ValueError as err:
                            output_fn(f"    Rejected: {err}")
                archive_map[acct.name] = chosen
        merged["accounts"]["archive_map"] = archive_map

        # 3. Gmail archive strategy
        has_gmail = any(is_gmail_all_mail_destination(box) for box in archive_map.values()) or any(
            is_gmail_all_mail_destination(a.archive_mailbox) for a in accounts
        )
        current_strategy = str(merged["accounts"].get("gmail_archive_strategy", "background")).strip().lower()
        if current_strategy not in VALID_GMAIL_ARCHIVE_STRATEGIES:
            current_strategy = "background"
            merged["accounts"]["gmail_archive_strategy"] = current_strategy
        if has_gmail and not yes:
            output_fn("\nGmail archive strategy:")
            output_fn("  1) ui_menu     : Clicks Mail.app's Archive toolbar/menu action (needs Accessibility)")
            output_fn("  2) two_hop     : Legacy stages via _ArchiveTmp and moves to All Mail")
            output_fn("  3) tag_staging : Tags _ArchiveTmp for a Google Apps Script trigger")
            output_fn("  4) background  : Silent headless Archive via Gmail Trash -> All Mail (recommended default)")
            choice = input_fn(f"Choose Gmail strategy (1-4 or name) [{current_strategy}]: ").strip().lower()
            mapping = {"1": "ui_menu", "2": "two_hop", "3": "tag_staging", "4": "background"}
            chosen_strategy = mapping.get(choice, choice or current_strategy)
            if chosen_strategy not in VALID_GMAIL_ARCHIVE_STRATEGIES:
                output_fn(f"Unknown strategy '{chosen_strategy}', keeping '{current_strategy}'.")
                chosen_strategy = current_strategy
            merged["accounts"]["gmail_archive_strategy"] = chosen_strategy
            if chosen_strategy == "tag_staging":
                output_fn("\nGoogle Apps Script setup for tag_staging:")
                output_fn("  1. Open https://script.google.com and create a New project.")
                output_fn("  2. Paste and save:\n")
                output_fn(APPS_SCRIPT_SNIPPET)
                output_fn("\n  3. Click Run once to authorize Gmail access, then add a 5-minute time-driven trigger.")

        # 4. Optional rule packs & consulting
        if not yes:
            cur_packs = list(merged["rules"].get("enabled_packs", []))
            se_default = "se" in cur_packs
            se_prompt = (
                "Enable Swedish billing/Kivra/delivery pack ('se')? [Y/n]: "
                if se_default
                else "Enable Swedish billing/Kivra/delivery pack ('se')? [y/N]: "
            )
            se_ans = input_fn(f"\n{se_prompt}").strip().lower()
            enable_se = se_default if se_ans == "" else se_ans in ("y", "yes")
            next_packs = [p for p in cur_packs if p != "se"]
            if enable_se:
                next_packs.append("se")
            merged["rules"]["enabled_packs"] = next_packs

            cur_consult = bool(merged["consulting"].get("enabled", False))
            c_prompt = (
                "Enable expert-network consulting classification? [Y/n]: "
                if cur_consult
                else "Enable expert-network consulting classification? [y/N]: "
            )
            c_ans = input_fn(c_prompt).strip().lower()
            merged["consulting"]["enabled"] = cur_consult if c_ans == "" else c_ans in ("y", "yes")

            cur_wanted = list(merged["profile"].get("wanted_domains", []))
            wanted_default = ", ".join(cur_wanted)
            w_ans = input_fn(f"Comma-separated wanted domains to keep/surface [{wanted_default or 'none'}]: ").strip()
            if w_ans:
                if w_ans.lower() == "none":
                    merged["profile"]["wanted_domains"] = []
                else:
                    merged["profile"]["wanted_domains"] = [d.strip().lower() for d in w_ans.split(",") if d.strip()]
    except (EOFError, KeyboardInterrupt):
        output_fn("\nSetup cancelled.")
        return 1

    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render_config_toml(merged)
    config.config_path.write_text(rendered, encoding="utf-8")
    output_fn(f"\nSaved configuration to {config.config_path}")
    output_fn("Next: run `nollpost doctor` and `nollpost serve`.")
    return 0
