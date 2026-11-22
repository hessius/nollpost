"""Batched Apple Mail AppleScript bridge with optional apple-mail-mcp advisory lock interop.

Hard invariant: NEVER deletes messages or moves them to Trash/Deleted/Junk folders.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from contextlib import contextmanager
from email.utils import parseaddr
from pathlib import Path
from typing import Iterator, Optional

from nollpost.models import AccountInfo, MailHeader

LOCK_WAIT_SECONDS = 30.0


def resolve_lock_path() -> Path:
    """Resolve Apple Mail advisory lock path (sharing apple-mail-mcp's lock when present)."""
    override = os.environ.get("NOLLPOST_LOCK_PATH")
    if override:
        return Path(override).expanduser()
    mcp_dir = Path("~/Library/Application Support/apple-mail-mcp").expanduser()
    if mcp_dir.exists():
        return mcp_dir / "mail_automation.lock"
    return Path("~/Library/Application Support/nollpost/mail_automation.lock").expanduser()


LOCK_PATH = resolve_lock_path()
_INITIAL_LOCK_PATH = LOCK_PATH

FORBIDDEN_DESTINATIONS = frozenset(
    {
        "trash",
        "deleted messages",
        "deleted items",
        "papperskorgen",
        "skräppost",
        "junk",
        "junk email",
        "spam",
        "bin",
        "papierkorb",
        "corbeille",
        "papelera",
    }
)

ARCHIVE_CANDIDATES = [
    "[Gmail]/All e-post",
    "[Gmail]/Alla brev",
    "[Gmail]/All Mail",
    "[Gmail]/Arkiv",
    "Archive",
    "All e-post",
    "All Mail",
    "Arkiv",
    "Arkiverade",
]

INBOX_CANDIDATES = [
    "INBOX",
    "Inbox",
    "Inkorgen",
    "Boîte de réception",
    "Posteingang",
    "Bandeja de entrada",
]

SENT_CANDIDATES = [
    "Sent Messages",
    "Sent",
    "Skickat",
    "[Gmail]/Skickat",
    "[Gmail]/Sent Mail",
    "Sent Items",
]

GMAIL_ARCHIVE_STAGING = "_ArchiveTmp"
GMAIL_TRASH_LEAF_NAMES = (
    "trash",
    "bin",
    "papperskorgen",
    "papierkorb",
    "corbeille",
    "papelera",
    "cestino",
    "prullenbak",
    "lixeira",
    "kosz",
    "papirkurv",
    "roskakori",
    "kuka",
    "skraldespand",
)
# Default Gmail archive path: silent background two-hop via Gmail Trash -> All Mail (strips \Inbox cleanly).
GMAIL_TAG_ARCHIVE_ENABLED = False
GMAIL_TWO_HOP_ENABLED = False
GMAIL_UI_ARCHIVE_ENABLED = True


def is_gmail_all_mail_destination(mailbox_name: str) -> bool:
    """Return True for Gmail All Mail special-use targets requiring two-hop \\Inbox removal."""
    low = mailbox_name.strip().lower()
    return (
        low.startswith("[gmail]/all")
        or low.startswith("[google mail]/all")
        or low in ("all e-post", "all mail", "alla brev", "[gmail]/arkiv", "[google mail]/arkiv")
    )

_AS_HELPERS = """
on encodeField(val)
    if val is missing value then set val to ""
    try
        set valText to val as text
    on error
        set valText to ""
    end try
    set oldDelims to AppleScript's text item delimiters
    set AppleScript's text item delimiters to {return, linefeed, tab, (character id 8232), (character id 8233)}
    set parts to text items of valText
    set AppleScript's text item delimiters to " "
    set valText to parts as text
    set AppleScript's text item delimiters to "%"
    set parts to text items of valText
    set AppleScript's text item delimiters to "%25"
    set valText to parts as text
    set AppleScript's text item delimiters to "|"
    set parts to text items of valText
    set AppleScript's text item delimiters to "%7C"
    set valText to parts as text
    set AppleScript's text item delimiters to oldDelims
    return valText
end encodeField

on pad2(n)
    if n < 10 then return "0" & (n as string)
    return n as string
end pad2

on monthNum(m)
    set monthsList to {January, February, March, April, May, June, July, August, September, October, November, December}
    repeat with idx from 1 to 12
        if item idx of monthsList is m then return idx
    end repeat
    return 0
end monthNum

on isoDate(d)
    try
        set y to year of d as integer
        set m to my monthNum(month of d)
        set dy to day of d as integer
        set h to hours of d
        set mn to minutes of d
        set s to seconds of d
        return (y as string) & "-" & my pad2(m) & "-" & my pad2(dy) & "T" & my pad2(h) & ":" & my pad2(mn) & ":" & my pad2(s)
    on error
        return ""
    end try
end isoDate

on resolveMailbox(targetAccount, boxPath)
    tell application "Mail"
        try
            return mailbox boxPath of targetAccount
        end try
        set oldDelims to AppleScript's text item delimiters
        set AppleScript's text item delimiters to "/"
        set pathParts to text items of boxPath
        set AppleScript's text item delimiters to oldDelims
        set leafName to item -1 of pathParts
        if (count of pathParts) > 1 then
            try
                set curBox to mailbox (item 1 of pathParts) of targetAccount
                repeat with idx from 2 to (count of pathParts)
                    set curBox to mailbox (item idx of pathParts) of curBox
                end repeat
                return curBox
            end try
        end if
        repeat with mb in every mailbox of targetAccount
            if (name of mb) is leafName then return mb
        end repeat
        return mailbox boxPath of targetAccount
    end tell
end resolveMailbox
"""


def escape_applescript(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\u2028", "\\n")
        .replace("\u2029", "\\n")
    )


def decode_field(value: str) -> str:
    return value.replace("%7C", "|").replace("%25", "%")


def validate_archive_destination(mailbox_name: str) -> str:
    """Enforce the hard non-deletion invariant across every path segment."""
    cleaned = mailbox_name.strip()
    if not cleaned:
        raise ValueError("Archive destination cannot be empty")
    segments = [seg.strip().lower() for seg in cleaned.split("/") if seg.strip()]
    if cleaned.lower() in FORBIDDEN_DESTINATIONS or any(seg in FORBIDDEN_DESTINATIONS for seg in segments):
        raise ValueError(
            f"Refusing forbidden destination mailbox '{mailbox_name}': deletion/trash is strictly prohibited"
        )
    return cleaned


def mailbox_ref_applescript(mailbox_path: str, account_var: str = "targetAccount", out_var: str = "targetMailbox") -> str:
    safe_path = escape_applescript(mailbox_path.strip())
    return f'set {out_var} to my resolveMailbox({account_var}, "{safe_path}")'


def parse_sender_parts(sender_raw: str) -> tuple[str, str]:
    """Return (sender_email, sender_domain) lowercased."""
    _, addr = parseaddr(sender_raw)
    if not addr and "@" in sender_raw:
        m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", sender_raw)
        if m:
            addr = m.group(0)
    addr = addr.strip().lower()
    domain = addr.partition("@")[2] if "@" in addr else ""
    return addr, domain


@contextmanager
def mail_automation_lock(timeout: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
    lock_path = resolve_lock_path() if LOCK_PATH == _INITIAL_LOCK_PATH else LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as err:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Mail automation lock busy after {timeout}s") from err
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run_osascript(script: str, timeout: int = 120) -> str:
    with mail_automation_lock():
        proc = subprocess.Popen(
            ["osascript", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = proc.communicate(input=script.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired as err:
            proc.kill()
            proc.wait()
            raise TimeoutError(f"osascript timed out after {timeout}s") from err
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"AppleScript error: {err}")
        return stdout.decode("utf-8", errors="replace").strip()


class AppleMailBridge:
    """High-speed batched Apple Mail bridge with strict Archive-only semantics."""

    def __init__(
        self,
        archive_overrides: Optional[dict[str, str]] = None,
        gmail_archive_strategy: str = "background",
    ):
        self.archive_overrides = dict(archive_overrides or {})
        self.gmail_archive_strategy = gmail_archive_strategy

    def list_accounts(self) -> list[AccountInfo]:
        script = _AS_HELPERS + """
        tell application "Mail"
            with timeout of 25 seconds
                set outLines to {}
                repeat with anAccount in every account
                    set acctName to name of anAccount
                    set addrList to {}
                    try
                        set addrList to email addresses of anAccount
                    end try
                    if addrList is missing value then set addrList to {}
                    set AppleScript's text item delimiters to ","
                    set addrStr to addrList as string
                    set AppleScript's text item delimiters to ""

                    set boxNames to {}
                    try
                        repeat with mb in every mailbox of anAccount
                            set mbPath to name of mb
                            set curBox to mb
                            repeat 4 times
                                try
                                    set parentObj to container of curBox
                                    if parentObj is anAccount or (name of parentObj) is acctName then exit repeat
                                    set mbPath to (name of parentObj) & "/" & mbPath
                                    set curBox to parentObj
                                on error
                                    exit repeat
                                end try
                            end repeat
                            set end of boxNames to mbPath
                        end repeat
                    end try
                    set AppleScript's text item delimiters to ";"
                    set boxStr to boxNames as string
                    set AppleScript's text item delimiters to ""

                    set end of outLines to my encodeField(acctName) & "|" & my encodeField(addrStr) & "|" & my encodeField(boxStr)
                end repeat
                set AppleScript's text item delimiters to linefeed
                set joined to outLines as string
                set AppleScript's text item delimiters to ""
                return joined
            end timeout
        end tell
        """
        raw = run_osascript(script, timeout=35)
        accounts: list[AccountInfo] = []
        for line in raw.split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            name = decode_field(parts[0]).strip()
            addrs = [a.strip() for a in decode_field(parts[1]).split(",") if a.strip()]
            boxes = [b.strip() for b in decode_field(parts[2]).split(";") if b.strip()]
            box_map = {b.lower(): b for b in boxes}

            inbox_box = next((box_map[c.lower()] for c in INBOX_CANDIDATES if c.lower() in box_map), "INBOX")

            override = self.archive_overrides.get(name)
            if override:
                archive_box = validate_archive_destination(override)
            else:
                detected = next(
                    (box_map[c.lower()] for c in ARCHIVE_CANDIDATES if c.lower() in box_map),
                    "Archive",
                )
                archive_box = validate_archive_destination(detected)

            sent_box = next((box_map[c.lower()] for c in SENT_CANDIDATES if c.lower() in box_map), None)
            accounts.append(
                AccountInfo(
                    name=name,
                    addresses=addrs,
                    inbox_mailbox=inbox_box,
                    archive_mailbox=archive_box,
                    sent_mailbox=sent_box,
                )
            )
        return accounts

    def scan_mailbox_metadata(
        self,
        account: str,
        mailbox: str = "INBOX",
        limit: int = 500,
        include_attachments: bool = True,
        days_back: int = 0,
    ) -> list[MailHeader]:
        safe_acct = escape_applescript(account)
        box_ref = mailbox_ref_applescript(mailbox, account_var="targetAccount", out_var="targetMailbox")
        att_snippet = (
            "try\nset attCount to count of mail attachments of aMessage\non error\nset attCount to 0\nend try"
            if include_attachments
            else "set attCount to 0"
        )
        py_timeout = 300 if days_back > 0 else 180
        as_timeout = py_timeout - 10
        row_handler = f"""
on extractMessageLine(aMessage)
    tell application "Mail"
        set msgId to id of aMessage as string
        set rfcId to ""
        try
            set rfcId to message id of aMessage
        end try
        set msgSubj to subject of aMessage
        set msgSender to sender of aMessage
        set msgDate to my isoDate(date received of aMessage)
        set msgRead to read status of aMessage
        set msgFlag to flagged status of aMessage
        {att_snippet}
        return msgId & "|" & my encodeField(rfcId) & "|" & my encodeField(msgSubj) & "|" & my encodeField(msgSender) & "|" & my encodeField(msgDate) & "|" & (msgRead as string) & "|" & (msgFlag as string) & "|" & (attCount as string)
    end tell
end extractMessageLine
"""
        if days_back > 0:
            # Bounded date predicate pushes filtering into Mail.app (verified ~13s on 101k Gmail
            # All Mail vs >180s O(N) timeouts with per-index message lookups); clamp window <=60d.
            safe_days = min(int(days_back), 60)
            pool_block = f"""
                set cutoffDate to (current date) - ({safe_days} * days)
                set targetMessages to (every message of targetMailbox whose date received >= cutoffDate)
                set totalRecent to count of targetMessages
                if totalRecent > scanLimit then
                    set targetMessages to items 1 thru scanLimit of targetMessages
                end if
                repeat with aMessage in targetMessages
                    try
                        set end of outLines to my extractMessageLine(aMessage)
                    end try
                end repeat"""
        else:
            pool_block = """
                set totalCount to count of messages of targetMailbox
                if totalCount > 5000 then
                    set maxIdx to scanLimit
                    if totalCount < maxIdx then set maxIdx to totalCount
                    repeat with msgIdx from 1 to maxIdx
                        try
                            set end of outLines to my extractMessageLine(message msgIdx of targetMailbox)
                        end try
                    end repeat
                else
                    set allMessages to every message of targetMailbox
                    set scannedCount to 0
                    repeat with aMessage in allMessages
                        set scannedCount to scannedCount + 1
                        if scannedCount > scanLimit then exit repeat
                        try
                            set end of outLines to my extractMessageLine(aMessage)
                        end try
                    end repeat
                end if"""
        script = _AS_HELPERS + row_handler + f"""
        tell application "Mail"
            with timeout of {as_timeout} seconds
                set targetAccount to account "{safe_acct}"
                {box_ref}
                set scanLimit to {int(limit)}
                set outLines to {{}}
{pool_block}
                set AppleScript's text item delimiters to linefeed
                set joined to outLines as string
                set AppleScript's text item delimiters to ""
                return joined
            end timeout
        end tell
        """
        raw = run_osascript(script, timeout=py_timeout)
        headers: list[MailHeader] = []
        for line in raw.split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) < 8:
                continue
            internal_id = parts[0].strip()
            rfc_id = decode_field(parts[1]).strip("<> ").strip()
            subject = decode_field(parts[2]).strip()
            sender = decode_field(parts[3]).strip()
            date_received = decode_field(parts[4]).strip()
            is_read = parts[5].strip().lower() == "true"
            is_flagged = parts[6].strip().lower() == "true"
            try:
                attachment_count = int(parts[7].strip())
            except ValueError:
                attachment_count = 0
            sender_email, sender_domain = parse_sender_parts(sender)
            headers.append(
                MailHeader(
                    account=account,
                    mailbox=mailbox,
                    internal_id=internal_id,
                    rfc_message_id=rfc_id,
                    subject=subject,
                    sender=sender,
                    sender_email=sender_email,
                    sender_domain=sender_domain,
                    date_received=date_received,
                    is_read=is_read,
                    is_flagged=is_flagged,
                    attachment_count=attachment_count,
                )
            )
        return headers

    def get_raw_source(self, account: str, mailbox: str, internal_id: str) -> bytes:
        if not re.fullmatch(r"\d+", str(internal_id).strip()):
            raise ValueError(f"internal_id must be digits only, got {internal_id!r}")
        safe_acct = escape_applescript(account)
        box_ref = mailbox_ref_applescript(mailbox, account_var="targetAccount", out_var="targetMailbox")
        script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 50 seconds
                set targetAccount to account "{safe_acct}"
                {box_ref}
                set targetMsg to «class mssg» id {int(internal_id)} of targetMailbox
                return source of targetMsg
            end timeout
        end tell
        """
        raw_str = run_osascript(script, timeout=60)
        return raw_str.encode("utf-8", errors="replace")

    def list_mailbox_ids(self, account: str, mailbox: str = "INBOX") -> Optional[set[str]]:
        """Return numeric Mail.app IDs present in a small mailbox, or None if >5000 msgs."""
        safe_acct = escape_applescript(account)
        box_ref = mailbox_ref_applescript(mailbox, account_var="targetAccount", out_var="targetMailbox")
        script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 25 seconds
                set targetAccount to account "{safe_acct}"
                {box_ref}
                set msgCount to count of messages of targetMailbox
                if msgCount = 0 then return "__EMPTY__"
                if msgCount > 5000 then return "__TOO_LARGE__"
                set allIds to id of every message of targetMailbox
                set AppleScript's text item delimiters to ","
                set joined to allIds as string
                set AppleScript's text item delimiters to ""
                return joined
            end timeout
        end tell
        """
        raw = run_osascript(script, timeout=30).strip()
        if raw == "__TOO_LARGE__" or not raw:
            return None
        if raw == "__EMPTY__":
            return set()
        return {x.strip() for x in raw.split(",") if x.strip()}

    def synchronize_account(self, account: str, timeout: int = 8) -> bool:
        """Best-effort bidirectional IMAP sync; returns False cleanly on timeout."""
        safe_acct = escape_applescript(account)
        script = f"""
        tell application "Mail"
            with timeout of {int(timeout)} seconds
                synchronize with account "{safe_acct}"
            end timeout
        end tell
        """
        try:
            run_osascript(script, timeout=int(timeout) + 5)
            return True
        except Exception:
            return False

    def mark_read(self, account: str, mailbox: str, internal_ids: list[str]) -> list[str]:
        clean_ids = [str(i).strip() for i in internal_ids if re.fullmatch(r"\d+", str(i).strip())]
        if not clean_ids:
            return []
        safe_acct = escape_applescript(account)
        box_ref = mailbox_ref_applescript(mailbox, account_var="targetAccount", out_var="targetMailbox")
        id_list = ", ".join(clean_ids)
        script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 110 seconds
                set targetAccount to account "{safe_acct}"
                {box_ref}
                set doneIds to {{}}
                repeat with msgId in {{{id_list}}}
                    try
                        set targetMsg to «class mssg» id (msgId as integer) of targetMailbox
                        set read status of targetMsg to true
                        set end of doneIds to (msgId as string)
                    end try
                end repeat
                set AppleScript's text item delimiters to ","
                set joined to doneIds as string
                set AppleScript's text item delimiters to ""
                return joined
            end timeout
        end tell
        """
        raw = run_osascript(script, timeout=120)
        return [x.strip() for x in raw.split(",") if x.strip()]

    def _archive_gmail_via_tag(
        self,
        account: str,
        from_mailbox: str,
        clean_ids: list[str],
        mark_read: bool = True,
    ) -> list[str]:
        """Primary Gmail archive: apply the _ArchiveTmp label (by moving to the _ArchiveTmp mailbox in
        Apple Mail) and sync IMAP so the server-side Google Apps Script (`in:inbox label:_ArchiveTmp`
        -> `moveToArchive()` + `removeLabel()`) archives the thread cleanly on its 5-minute schedule.
        """
        stage_name = validate_archive_destination(GMAIL_ARCHIVE_STAGING)
        safe_acct = escape_applescript(account)
        safe_stage = escape_applescript(stage_name)
        src_ref = mailbox_ref_applescript(from_mailbox, account_var="targetAccount", out_var="sourceMailbox")
        id_list = ", ".join(clean_ids)
        read_line = "set read status of targetMsg to true" if mark_read else ""

        tag_script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 110 seconds
                set targetAccount to account "{safe_acct}"
                {src_ref}
                set stageBox to missing value
                try
                    set stageBox to mailbox "{safe_stage}" of targetAccount
                on error
                    try
                        make new mailbox at targetAccount with properties {{name:"{safe_stage}"}}
                    end try
                    try
                        set stageBox to mailbox "{safe_stage}" of targetAccount
                    end try
                end try
                if stageBox is missing value then
                    repeat with mb in every mailbox of targetAccount
                        if (name of mb) is "{safe_stage}" then
                            set stageBox to mb
                            exit repeat
                        end if
                    end repeat
                end if
                if stageBox is missing value then error "Unable to resolve or create staging mailbox {safe_stage}"
                set taggedIds to {{}}
                repeat with msgId in {{{id_list}}}
                    try
                        set targetMsg to «class mssg» id (msgId as integer) of sourceMailbox
                        {read_line}
                        move targetMsg to stageBox
                        set end of taggedIds to (msgId as string)
                    end try
                end repeat
                try
                    with timeout of 12 seconds
                        synchronize with targetAccount
                    end timeout
                end try
                set AppleScript's text item delimiters to ","
                set joined to taggedIds as string
                set AppleScript's text item delimiters to ""
                return joined
            end timeout
        end tell
        """
        raw = run_osascript(tag_script, timeout=120)
        return [x.strip() for x in raw.split(",") if x.strip()]

    def _archive_gmail_two_hop(
        self,
        account: str,
        from_mailbox: str,
        dest_mailbox: str,
        clean_ids: list[str],
        mark_read: bool = True,
    ) -> list[str]:
        """Archive Gmail messages via staging folder so \\Inbox is stripped before moving to All Mail."""
        stage_name = validate_archive_destination(GMAIL_ARCHIVE_STAGING)
        safe_acct = escape_applescript(account)
        safe_stage = escape_applescript(stage_name)
        src_ref = mailbox_ref_applescript(from_mailbox, account_var="targetAccount", out_var="sourceMailbox")
        dest_ref = mailbox_ref_applescript(dest_mailbox, account_var="targetAccount", out_var="destMailbox")
        id_list = ", ".join(clean_ids)
        read_line = "set read status of targetMsg to true" if mark_read else ""

        hop1_script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 110 seconds
                set targetAccount to account "{safe_acct}"
                {src_ref}
                try
                    set stageBox to mailbox "{safe_stage}" of targetAccount
                on error
                    make new mailbox at targetAccount with properties {{name:"{safe_stage}"}}
                    set stageBox to mailbox "{safe_stage}" of targetAccount
                end try
                set movedInfo to {{}}
                repeat with msgId in {{{id_list}}}
                    try
                        set targetMsg to «class mssg» id (msgId as integer) of sourceMailbox
                        set rfcId to ""
                        try
                            set rfcId to message id of targetMsg
                        end try
                        {read_line}
                        move targetMsg to stageBox
                        set end of movedInfo to (msgId as string) & "|" & my encodeField(rfcId)
                    end try
                end repeat
                try
                    with timeout of 12 seconds
                        synchronize with targetAccount
                    end timeout
                end try
                set AppleScript's text item delimiters to linefeed
                set joined to movedInfo as string
                set AppleScript's text item delimiters to ""
                return joined
            end timeout
        end tell
        """
        raw_hop1 = run_osascript(hop1_script, timeout=120)
        hop1_pairs: list[tuple[str, str]] = []
        for line in raw_hop1.split("\n"):
            if "|" not in line:
                continue
            mid, _, enc_rfc = line.partition("|")
            mid = mid.strip()
            rfc = decode_field(enc_rfc).strip("<> ").strip()
            if mid:
                hop1_pairs.append((mid, rfc))

        if not hop1_pairs:
            return []

        hop2_script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 110 seconds
                set targetAccount to account "{safe_acct}"
                set stageBox to mailbox "{safe_stage}" of targetAccount
                {dest_ref}
                set stageMsgs to every message of stageBox
                set settledRfcs to {{}}
                repeat with m in stageMsgs
                    try
                        set mRfc to ""
                        try
                            set mRfc to message id of m
                        end try
                        move m to destMailbox
                        set end of settledRfcs to my encodeField(mRfc)
                    end try
                end repeat
                try
                    with timeout of 10 seconds
                        synchronize with targetAccount
                    end timeout
                end try
                set AppleScript's text item delimiters to linefeed
                set joined to settledRfcs as string
                set AppleScript's text item delimiters to ""
                return joined
            end timeout
        end tell
        """
        try:
            run_osascript(hop2_script, timeout=120)
        except Exception:
            # Hop 1 already stripped \Inbox into safe non-Trash _ArchiveTmp; let next pass flush to All Mail
            pass

        return [mid for mid, _ in hop1_pairs]

    def _archive_gmail_via_ui(
        self,
        account: str,
        from_mailbox: str,
        clean_ids: list[str],
        mark_read: bool = True,
    ) -> list[str]:
        """Archive Gmail messages via Mail.app's native Archive action (toolbar button first,
        validated Archive menu item fallback). Never deletes or touches Trash.

        Safety: opens the specific target message id, verifies the focused/main window title
        matches the target message subject before clicking Archive, and verifies afterwards that
        only the targeted id left the source mailbox.
        """
        before = self.list_mailbox_ids(account, from_mailbox)
        if before is None:
            return []
        targets = [i for i in clean_ids if i in before]
        if not targets:
            return []
        if mark_read:
            try:
                self.mark_read(account, from_mailbox, targets)
            except Exception:
                pass

        safe_acct = escape_applescript(account)
        box_ref = mailbox_ref_applescript(from_mailbox, account_var="targetAccount", out_var="targetMailbox")
        cands = '{"Archive", "Arkivera", "Archivieren", "Archiver", "Archivar"}'
        moved: list[str] = []
        for mid in targets:
            script = (
                _AS_HELPERS
                + f"set cands to {cands}\n"
                + 'set subj to ""\n'
                + 'set subjPrefix to ""\n'
                + 'tell application "Mail"\n'
                + f'  set targetAccount to account "{safe_acct}"\n'
                + f"  {box_ref}\n"
                + f"  set tgt to (every message of targetMailbox whose id is {int(mid)})\n"
                + '  if (count of tgt) is not 1 then return "ABORT: target missing"\n'
                + "  set subj to subject of item 1 of tgt\n"
                + '  if subj is missing value then set subj to ""\n'
                + '  if subj is "" then return "ABORT: empty subject unsafe for UI archive"\n'
                + "  if (length of subj) > 60 then\n"
                + "    set subjPrefix to text 1 thru 60 of subj\n"
                + "  else\n"
                + "    set subjPrefix to subj\n"
                + "  end if\n"
                + "  open item 1 of tgt\n"
                + "end tell\n"
                + "set didActivate to false\n"
                + 'set prevApp to ""\n'
                + "set targetWin to missing value\n"
                + 'tell application "System Events"\n'
                + '  tell process "Mail"\n'
                + "    -- Try locating the opened window without stealing focus first\n"
                + "    repeat 8 times\n"
                + "      delay 0.1\n"
                + "      repeat with attrName in {\"AXFocusedWindow\", \"AXMainWindow\"}\n"
                + "        try\n"
                + "          set candWin to value of attribute (attrName as string)\n"
                + '          set candTitle to value of attribute "AXTitle" of candWin\n'
                + "          if candTitle starts with subjPrefix then\n"
                + "            set targetWin to candWin\n"
                + "            exit repeat\n"
                + "          end if\n"
                + "        end try\n"
                + "      end repeat\n"
                + "      if targetWin is not missing value then exit repeat\n"
                + "    end repeat\n"
                + "    if targetWin is missing value then\n"
                + "      try\n"
                + "        tell application \"System Events\" to set prevApp to name of first application process whose frontmost is true\n"
                + "      end try\n"
                + "      set didActivate to true\n"
                + '      tell application "Mail" to activate\n'
                + "      repeat 20 times\n"
                + "        delay 0.1\n"
                + "        repeat with attrName in {\"AXFocusedWindow\", \"AXMainWindow\"}\n"
                + "          try\n"
                + "            set candWin to value of attribute (attrName as string)\n"
                + '            set candTitle to value of attribute "AXTitle" of candWin\n'
                + "            if candTitle starts with subjPrefix then\n"
                + "              set targetWin to candWin\n"
                + "              exit repeat\n"
                + "            end if\n"
                + "          end try\n"
                + "        end repeat\n"
                + "        if targetWin is not missing value then exit repeat\n"
                + "      end repeat\n"
                + "    end if\n"
                + "    if targetWin is missing value then\n"
                + "      try\n"
                + '        tell application "Mail" to if (name of front window) starts with subjPrefix then close front window\n'
                + "      end try\n"
                + '      if didActivate and prevApp is not "" and prevApp is not "Mail" then\n'
                + "        try\n"
                + '          tell application "System Events" to set frontmost of process prevApp to true\n'
                + "        end try\n"
                + "      end if\n"
                + '      return "ABORT: front window mismatch"\n'
                + "    end if\n"
                + "    -- Pass 1: Click native toolbar Archive button directly (avoids lazy NSMenu validation)\n"
                + "    try\n"
                + "      set tb to toolbar 1 of targetWin\n"
                + "      repeat with b in UI elements of tb\n"
                + "        try\n"
                + '          if (role of b) is "AXButton" then\n'
                + '            set bDesc to ""\n'
                + '            set bTitle to ""\n'
                + "            try\n"
                + "              set bDesc to (description of b as string)\n"
                + "            end try\n"
                + "            try\n"
                + "              set bTitle to (title of b as string)\n"
                + "            end try\n"
                + "            if ((bDesc is in cands) or (bTitle is in cands)) and (enabled of b) then\n"
                + "              click b\n"
                + '              if didActivate and prevApp is not "" and prevApp is not "Mail" then\n'
                + "                try\n"
                + '                  tell application "System Events" to set frontmost of process prevApp to true\n'
                + "                end try\n"
                + "              end if\n"
                + '              return "ok"\n'
                + "            end if\n"
                + '          else if (role of b) is "AXGroup" then\n'
                + "            repeat with gb in UI elements of b\n"
                + "              try\n"
                + '                if (role of gb) is "AXButton" then\n'
                + '                  set gbDesc to ""\n'
                + '                  set gbTitle to ""\n'
                + "                  try\n"
                + "                    set gbDesc to (description of gb as string)\n"
                + "                  end try\n"
                + "                  try\n"
                + "                    set gbTitle to (title of gb as string)\n"
                + "                  end try\n"
                + "                  if ((gbDesc is in cands) or (gbTitle is in cands)) and (enabled of gb) then\n"
                + "                    click gb\n"
                + '                    if didActivate and prevApp is not "" and prevApp is not "Mail" then\n'
                + "                      try\n"
                + '                        tell application "System Events" to set frontmost of process prevApp to true\n'
                + "                      end try\n"
                + "                    end if\n"
                + '                    return "ok"\n'
                + "                  end if\n"
                + "                end if\n"
                + "              end try\n"
                + "            end repeat\n"
                + "          end if\n"
                + "        end try\n"
                + "      end repeat\n"
                + "    end try\n"
                + "    -- Pass 2: Fallback to Message menu (forcing NSMenuDelegate validation via menu bar click)\n"
                + "    if not didActivate then\n"
                + "      try\n"
                + '        tell application "System Events" to set prevApp to name of first application process whose frontmost is true\n'
                + "      end try\n"
                + "      set didActivate to true\n"
                + '      tell application "Mail" to activate\n'
                + "      delay 0.2\n"
                + "    end if\n"
                + "    repeat with mbi in menu bar items of menu bar 1\n"
                + "      try\n"
                + "        repeat with mi in menu items of menu 1 of mbi\n"
                + "          try\n"
                + "            if (name of mi) is in cands then\n"
                + "              if not (enabled of mi) then\n"
                + "                click mbi\n"
                + "                delay 0.15\n"
                + "              end if\n"
                + "              if not (enabled of mi) then\n"
                + "                key code 53\n"
                + "                try\n"
                + '                  tell application "Mail" to if (name of front window) starts with subjPrefix then close front window\n'
                + "                end try\n"
                + '                if prevApp is not "" and prevApp is not "Mail" then\n'
                + "                  try\n"
                + '                    tell application "System Events" to set frontmost of process prevApp to true\n'
                + "                  end try\n"
                + "                end if\n"
                + '                return "ABORT: archive menu disabled"\n'
                + "              end if\n"
                + "              click mi\n"
                + '              if prevApp is not "" and prevApp is not "Mail" then\n'
                + "                try\n"
                + '                  tell application "System Events" to set frontmost of process prevApp to true\n'
                + "                end try\n"
                + "              end if\n"
                + '              return "ok"\n'
                + "            end if\n"
                + "          end try\n"
                + "        end repeat\n"
                + "      end try\n"
                + "    end repeat\n"
                + "  end tell\n"
                + "end tell\n"
                + "try\n"
                + '  tell application "Mail" to if (name of front window) starts with subjPrefix then close front window\n'
                + "end try\n"
                + 'if didActivate and prevApp is not "" and prevApp is not "Mail" then\n'
                + "  try\n"
                + '    tell application "System Events" to set frontmost of process prevApp to true\n'
                + "  end try\n"
                + "end if\n"
                + 'return "ABORT: archive menu item not found"\n'
            )
            try:
                out = run_osascript(script, timeout=45).strip()
            except Exception:
                break
            if not out.startswith("ok"):
                continue

            left: set[str] = set()
            after: Optional[set[str]] = before
            for delay_s in (0.6, 1.0, 1.5, 2.0):
                time.sleep(delay_s)
                after = self.list_mailbox_ids(account, from_mailbox)
                if after is None:
                    break
                left = before - after
                if left:
                    break
            if after is None:
                break
            if mid in left:
                moved.append(mid)
            before = after
            if left - {mid}:
                # Something other than the target was archived: stop immediately rather than risk more.
                break
        return moved

    def _archive_gmail_background(
        self,
        account: str,
        from_mailbox: str,
        dest_mailbox: str,
        clean_ids: list[str],
        mark_read: bool = True,
    ) -> list[str]:
        """Archive Gmail messages silently in the background with zero windows or focus stealing.

        Moving directly to ``[Gmail]/All Mail`` over IMAP retains ``\\Inbox``; routing each
        message atomically through Gmail's ``[Gmail]/<Trash>`` folder causes Gmail's server
        to strip ``\\Inbox``, after which moving it immediately by RFC ``Message-ID`` to
        ``[Gmail]/All Mail`` clears ``\\Trash`` while preserving the archived copy.
        Requires a non-empty RFC ``Message-ID`` per message and rolls back to ``from_mailbox``
        if moving out of Trash fails so no message is ever abandoned in Trash.
        """
        dest = validate_archive_destination(dest_mailbox)
        safe_acct = escape_applescript(account)
        src_ref = mailbox_ref_applescript(from_mailbox, account_var="targetAccount", out_var="sourceMailbox")
        dest_ref = mailbox_ref_applescript(dest, account_var="targetAccount", out_var="archMailbox")
        id_list = ", ".join(clean_ids)
        trash_cands = "{" + ", ".join(f'"{escape_applescript(name)}"' for name in GMAIL_TRASH_LEAF_NAMES) + "}"
        read_target = "set read status of targetMsg to true" if mark_read else ""
        read_moved = "try\nset read status of newMsg to true\nend try" if mark_read else ""

        script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 110 seconds
                set targetAccount to account "{safe_acct}"
                {src_ref}
                {dest_ref}
                set gmailContainer to missing value
                repeat with rootName in {{"[Gmail]", "[Google Mail]"}}
                    try
                        set gmailContainer to mailbox (rootName as string) of targetAccount
                        exit repeat
                    end try
                end repeat
                if gmailContainer is missing value then return ""

                set trashNames to {trash_cands}
                set trashBox to missing value
                repeat with mb in every mailbox of gmailContainer
                    if (name of mb) is in trashNames then
                        set trashBox to mb
                        exit repeat
                    end if
                end repeat
                if trashBox is missing value then return ""

                set movedIds to {{}}
                repeat with msgId in {{{id_list}}}
                    try
                        set targetMsg to «class mssg» id (msgId as integer) of sourceMailbox
                        set msgRfc to ""
                        try
                            set msgRfc to message id of targetMsg
                        end try
                        if msgRfc is not missing value and msgRfc is not "" then
                            set prevTrashIds to {{}}
                            try
                                set existingHits to (every message of trashBox whose message id is msgRfc)
                                repeat with em in existingHits
                                    set end of prevTrashIds to (id of em)
                                end repeat
                            end try
                            {read_target}
                            move targetMsg to trashBox

                            set newMsg to missing value
                            repeat 15 times
                                try
                                    set hits to (every message of trashBox whose message id is msgRfc)
                                    repeat with candMsg in hits
                                        if (id of candMsg) is not in prevTrashIds then
                                            set newMsg to candMsg
                                            exit repeat
                                        end if
                                    end repeat
                                    if newMsg is not missing value then exit repeat
                                end try
                                delay 0.2
                            end repeat

                            if newMsg is not missing value then
                                try
                                    {read_moved}
                                    move newMsg to archMailbox
                                    set end of movedIds to (msgId as string)
                                on error
                                    try
                                        move newMsg to sourceMailbox
                                    end try
                                end try
                            end if
                        end if
                    end try
                end repeat

                try
                    with timeout of 12 seconds
                        synchronize with targetAccount
                    end timeout
                end try

                set AppleScript's text item delimiters to ","
                set joined to movedIds as string
                set AppleScript's text item delimiters to ""
                return joined
            end timeout
        end tell
        """
        raw = run_osascript(script, timeout=120)
        return [x.strip() for x in raw.split(",") if x.strip()]

    def mark_read_and_archive(
        self,
        account: str,
        from_mailbox: str,
        archive_mailbox: str,
        internal_ids: list[str],
        mark_read: bool = True,
    ) -> list[str]:
        dest = validate_archive_destination(archive_mailbox)
        clean_ids = [str(i).strip() for i in internal_ids if re.fullmatch(r"\d+", str(i).strip())]
        if not clean_ids:
            return []
        is_gmail_arch = from_mailbox.strip().lower() != dest.lower() and is_gmail_all_mail_destination(dest)
        if is_gmail_arch:
            if self.gmail_archive_strategy == "background":
                return self._archive_gmail_background(
                    account=account,
                    from_mailbox=from_mailbox,
                    dest_mailbox=dest,
                    clean_ids=clean_ids,
                    mark_read=mark_read,
                )
            moved: list[str] = []
            remaining = list(clean_ids)
            use_tag = self.gmail_archive_strategy == "tag_staging" or GMAIL_TAG_ARCHIVE_ENABLED
            use_two_hop = self.gmail_archive_strategy == "two_hop" or GMAIL_TWO_HOP_ENABLED
            if use_tag:
                try:
                    tagged = self._archive_gmail_via_tag(account, from_mailbox, remaining, mark_read=mark_read)
                except Exception:
                    tagged = []
                tagged_set = set(tagged)
                moved.extend(i for i in remaining if i in tagged_set)
                remaining = [i for i in remaining if i not in tagged_set]
            if remaining and use_two_hop and self.gmail_archive_strategy == "two_hop":
                two_hop = self._archive_gmail_two_hop(
                    account=account,
                    from_mailbox=from_mailbox,
                    dest_mailbox=dest,
                    clean_ids=remaining,
                    mark_read=mark_read,
                )
                th_set = set(two_hop)
                moved.extend(i for i in remaining if i in th_set)
                remaining = [i for i in remaining if i not in th_set]
            if remaining and GMAIL_UI_ARCHIVE_ENABLED:
                ui_moved = self._archive_gmail_via_ui(account, from_mailbox, remaining, mark_read=mark_read)
                ui_set = set(ui_moved)
                moved.extend(i for i in remaining if i in ui_set)
                remaining = [i for i in remaining if i not in ui_set]
            if remaining and use_two_hop:
                two_hop = self._archive_gmail_two_hop(
                    account=account,
                    from_mailbox=from_mailbox,
                    dest_mailbox=dest,
                    clean_ids=remaining,
                    mark_read=mark_read,
                )
                th_set = set(two_hop)
                moved.extend(i for i in remaining if i in th_set)
            return moved
        return self._move_to_mailbox(account, from_mailbox, dest, clean_ids, mark_read)

    def _move_to_mailbox(
        self,
        account: str,
        from_mailbox: str,
        dest: str,
        clean_ids: list[str],
        mark_read: bool,
    ) -> list[str]:
        safe_acct = escape_applescript(account)
        src_ref = mailbox_ref_applescript(from_mailbox, account_var="targetAccount", out_var="sourceMailbox")
        dest_ref = mailbox_ref_applescript(dest, account_var="targetAccount", out_var="destMailbox")
        id_list = ", ".join(clean_ids)
        read_line = "set read status of targetMsg to true" if mark_read else ""
        script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 110 seconds
                set targetAccount to account "{safe_acct}"
                {src_ref}
                {dest_ref}
                set movedIds to {{}}
                repeat with msgId in {{{id_list}}}
                    try
                        set targetMsg to «class mssg» id (msgId as integer) of sourceMailbox
                        {read_line}
                        move targetMsg to destMailbox
                        set end of movedIds to (msgId as string)
                    end try
                end repeat
                set AppleScript's text item delimiters to ","
                set joined to movedIds as string
                set AppleScript's text item delimiters to ""
                return joined
            end timeout
        end tell
        """
        raw = run_osascript(script, timeout=120)
        return [x.strip() for x in raw.split(",") if x.strip()]

    def restore_to_inbox(
        self,
        account: str,
        from_mailbox: str,
        internal_id: str,
        *,
        inbox_mailbox: str = "INBOX",
        rfc_message_id: str = "",
    ) -> bool:
        """Restore a previously archived message back into the account's INBOX (Archive-only safety preserved)."""
        dest_inbox = validate_archive_destination(inbox_mailbox)
        src_box = validate_archive_destination(from_mailbox)
        cleaned_id = str(internal_id).strip()
        if cleaned_id and not re.fullmatch(r"\d+", cleaned_id):
            raise ValueError(f"internal_id must be digits only, got {internal_id!r}")
        if not cleaned_id and not rfc_message_id.strip():
            raise ValueError("Either internal_id or rfc_message_id is required to restore a message")

        safe_acct = escape_applescript(account)
        src_ref = mailbox_ref_applescript(src_box, account_var="targetAccount", out_var="sourceMailbox")
        dest_ref = mailbox_ref_applescript(dest_inbox, account_var="targetAccount", out_var="destMailbox")
        id_val = int(cleaned_id) if cleaned_id else 0
        safe_rfc = escape_applescript(rfc_message_id.strip())
        script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 60 seconds
                set targetAccount to account "{safe_acct}"
                {src_ref}
                {dest_ref}
                set targetMsg to missing value
                if {id_val} > 0 then
                    try
                        set targetMsg to «class mssg» id {id_val} of sourceMailbox
                    end try
                end if
                if targetMsg is missing value and "{safe_rfc}" is not "" then
                    try
                        set matched to (every message of sourceMailbox whose message id is "{safe_rfc}")
                        if (count of matched) > 0 then set targetMsg to item 1 of matched
                    end try
                end if
                if targetMsg is not missing value then
                    move targetMsg to destMailbox
                    return "moved"
                end if
                if {id_val} > 0 then
                    try
                        set alreadyMsg to «class mssg» id {id_val} of destMailbox
                        if alreadyMsg is not missing value then return "already_in_inbox"
                    end try
                end if
                return "not_found"
            end timeout
        end tell
        """
        out = run_osascript(script, timeout=70).strip()
        return out in ("moved", "already_in_inbox")

    def create_or_send_reply(
        self,
        account: str,
        mailbox: str,
        internal_id: str,
        reply_body: str,
        send_now: bool = False,
        open_window: bool = True,
    ) -> str:
        if not re.fullmatch(r"\d+", str(internal_id).strip()):
            raise ValueError(f"internal_id must be digits only, got {internal_id!r}")
        safe_acct = escape_applescript(account)
        safe_body = escape_applescript(reply_body)
        box_ref = mailbox_ref_applescript(mailbox, account_var="targetAccount", out_var="targetMailbox")
        if send_now:
            tail = "send replyMsg\nreturn \"sent\""
        elif open_window:
            tail = "save replyMsg\nset visible of replyMsg to true\nactivate\nreturn \"drafted_open\""
        else:
            tail = "save replyMsg\nclose replyMsg saving yes\nreturn \"drafted\""
        script = _AS_HELPERS + f"""
        tell application "Mail"
            with timeout of 50 seconds
                set targetAccount to account "{safe_acct}"
                {box_ref}
                set targetMsg to «class mssg» id {int(internal_id)} of targetMailbox
                set replyMsg to reply targetMsg with opening window
                set existingBody to content of replyMsg
                set content of replyMsg to "{safe_body}" & return & return & existingBody
                set read status of targetMsg to true
                {tail}
            end timeout
        end tell
        """
        return run_osascript(script, timeout=60)
