"""CLI entrypoint for nollpost."""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import sys

import uvicorn

from nollpost.config import TriageConfig, get_cache_dir, get_config_path, get_data_dir, load_config
from nollpost.mail_bridge import AppleMailBridge, parse_sender_parts
from nollpost.onboarding import format_doctor_report, run_doctor, run_setup
from nollpost.scanner import run_scan
from nollpost.store import TriageStore
from nollpost.web import create_app


def _find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _print_briefing_text(snapshot_dict: dict) -> None:
    stats = snapshot_dict["stats"]
    brief = snapshot_dict["briefing"]
    print(
        f"Lifetime Tallies: {stats['total_unsubscribed']} unsubscribed | "
        f"{stats['total_archived']} archived | {stats['total_declined']} declined | "
        f"0 deleted | {stats['missed_unsub_pending']} missed-unsub queued"
    )
    print("\nAccounts:")
    for acct, counts in brief["per_account"].items():
        print(f"  - {acct}: {counts['total']} total ({counts['unread']} unread)")
    warnings = snapshot_dict.get("warnings") or []
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  ! {w}")
    print("\nCategories:")
    for cat, cnt in brief["by_category"].items():
        print(f"  - {cat}: {cnt}")
    highlights = brief["attention_highlights"]
    if highlights:
        print("\nTop Attention / Updates / Matches:")
        for h in highlights[:15]:
            read_mark = "[read]" if h["is_read"] else "[NEW]"
            print(f"  {read_mark} [{h['account']} | {h['subcategory']}] {h['subject']} — {h['sender']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nollpost", description="Multi-inbox Apple Mail triage and unsubscribe runner")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="Scan all configured Apple Mail accounts")
    scan_p.add_argument("--mode", choices=["inbox", "catchup"], default="inbox")
    scan_p.add_argument("--json", action="store_true", dest="as_json")

    serve_p = sub.add_parser("serve", help="Start the local 127.0.0.1 Review Board web app")
    serve_p.add_argument("--port", type=int, default=0, help="Port to bind (default: random free port)")
    serve_p.add_argument("--no-open", action="store_true", help="Do not auto-open browser")
    serve_p.add_argument("--no-preload", action="store_true", help="Defer initial Mail scan until browser opens")

    sub.add_parser("stats", help="Show lifetime triage tallies")

    missed_p = sub.add_parser("missed", help="List missed/failed unsubscribe snapshots for parser improvement")
    missed_p.add_argument("--all", action="store_true", help="Include resolved items")
    missed_p.add_argument("--json", action="store_true", dest="as_json")

    resolve_p = sub.add_parser("resolve-missed", help="Mark a missed-unsub queue entry as resolved")
    resolve_p.add_argument("id", type=int)
    resolve_p.add_argument("--note", default="Resolved")

    mark_p = sub.add_parser(
        "mark-unsubscribed",
        help="Confirm a missed/failed entry was actually unsubscribed, update tallies, and optionally archive",
    )
    mark_p.add_argument("id", type=int)
    mark_p.add_argument("--no-archive", action="store_true", help="Do not archive the message from INBOX")
    mark_p.add_argument("--note", default="Marked as unsubscribed by user")

    doctor_p = sub.add_parser("doctor", help="Run preflight checks for macOS, Mail.app, permissions, and Chromium")
    doctor_p.add_argument("--json", action="store_true", dest="as_json")

    setup_p = sub.add_parser("setup", help="Interactive first-run onboarding wizard")
    setup_p.add_argument("--yes", action="store_true", help="Accept defaults without interactive prompts")
    setup_p.add_argument("--skip-browser", action="store_true", help="Skip Playwright Chromium install check")

    args = parser.parse_args(argv)
    if args.command in ("doctor", "setup"):
        config_error = ""
        try:
            config = load_config(ensure_files=False)
        except Exception as exc:
            config_error = str(exc)
            cfg_path = get_config_path()
            data_dir = get_data_dir()
            cache_dir = get_cache_dir()
            config = TriageConfig(
                config_path=cfg_path,
                data_dir=data_dir,
                cache_dir=cache_dir,
                db_path=data_dir / "state.db",
                invoices_dir=cache_dir / "invoices",
                screenshots_dir=data_dir / "screenshots",
            )
        bridge = AppleMailBridge(
            archive_overrides=config.archive_map,
            gmail_archive_strategy=config.gmail_archive_strategy,
        )
        if args.command == "doctor":
            ok, checks = run_doctor(config, bridge, config_error=config_error)
            if args.as_json:
                print(json.dumps({"ok": ok, "checks": [c.to_dict() for c in checks]}, ensure_ascii=False, indent=2))
            else:
                print(format_doctor_report(checks))
            return 0 if ok else 1
        return run_setup(config, bridge, yes=args.yes, skip_browser=args.skip_browser)

    config = load_config(ensure_files=True)
    bridge = AppleMailBridge(
        archive_overrides=config.archive_map,
        gmail_archive_strategy=config.gmail_archive_strategy,
    )
    store = TriageStore(config.db_path)

    if args.command == "scan":
        snap = run_scan(config, store, bridge, mode=args.mode)
        data = snap.to_dict()
        if args.as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            _print_briefing_text(data)
        return 0

    if args.command == "stats":
        print(json.dumps(store.get_stats(), indent=2))
        return 0

    if args.command == "missed":
        entries = store.list_missed_unsubscribes(include_resolved=args.all)
        if args.as_json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("No unresolved missed-unsub items.")
            for e in entries:
                print(f"#{e['id']} [{e['stage']}] {e['account']}/{e['mailbox']} id={e['internal_id']} | {e['sender']} — {e['subject']}")
                print(f"   reason: {e['failure_reason']}")
        return 0

    if args.command == "resolve-missed":
        store.resolve_missed_unsubscribe(args.id, args.note)
        print(f"Resolved missed-unsub #{args.id}")
        return 0

    if args.command == "mark-unsubscribed":
        entry = store.get_missed_unsubscribe(args.id)
        if not entry:
            print(f"Missed-unsub #{args.id} not found", file=sys.stderr)
            return 1
        sender_email, sender_domain = parse_sender_parts(entry["sender"])
        eff_domain = sender_domain or entry["sender_domain"]
        store.record_unsubscribed(
            sender_email=sender_email,
            display_name=entry["sender"],
            sender_domain=eff_domain,
        )
        store.resolve_missed_unsubscribe(args.id, args.note)
        store.resolve_missed_for_message(
            account=entry.get("account", ""),
            internal_id=str(entry.get("internal_id", "")),
            sender_domain=eff_domain,
            note=args.note,
        )
        archived_msg = ""
        if not args.no_archive and str(entry.get("mailbox", "")).upper() == "INBOX":
            accounts = {a.name: a.archive_mailbox for a in bridge.list_accounts()}
            arch_box = accounts.get(entry["account"]) or config.archive_map.get(entry["account"], "Archive")
            moved = bridge.mark_read_and_archive(
                entry["account"],
                entry["mailbox"],
                arch_box,
                [str(entry["internal_id"])],
                mark_read=config.mark_read_on_archive,
            )
            if str(entry["internal_id"]) in moved:
                store.increment_stat("total_archived", 1)
                archived_msg = f" and archived to {entry['account']}/{arch_box}"
        store.log_action(
            run_id="manual-cli",
            account=entry.get("account", "unknown"),
            internal_id=str(entry.get("internal_id", "")),
            sender=entry.get("sender", ""),
            subject=entry.get("subject", ""),
            action="mark_unsubscribed",
            status="ok",
            detail=f"{args.note}{archived_msg}",
        )
        print(f"Marked missed-unsub #{args.id} ({entry['sender']}) as unsubscribed{archived_msg}")
        return 0

    if args.command == "serve":
        port = args.port or _find_free_port("127.0.0.1")
        token = secrets.token_urlsafe(24)
        url = f"http://127.0.0.1:{port}/?key={token}"
        app, _ = create_app(
            config,
            store,
            bridge,
            token=token,
            preload=not args.no_preload,
            open_browser_url=None if args.no_open else url,
        )
        print(f"Review Board URL: {url}", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
