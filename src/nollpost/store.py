"""SQLite persistence for tallies, sender memory, safety tray, and missed unsubscribes."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

STAT_KEYS = (
    "total_unsubscribed",
    "total_archived",
    "total_declined",
    "total_opened",
    "unique_senders_unsubscribed",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def days_since_iso(iso_ts: Optional[str]) -> Optional[int]:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0, int(delta.total_seconds() // 86400))
    except ValueError:
        return None


def hours_since_iso(iso_ts: Optional[str]) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0.0, delta.total_seconds() / 3600.0)
    except ValueError:
        return None


@dataclass
class SenderMemoryRecord:
    sender_key: str
    display_name: str
    preferred_action: Optional[str]
    keep_subscribed: bool
    unsubscribed_at: Optional[str]
    unsub_count: int
    last_seen_at: str


class TriageStore:
    """Thread-safe SQLite store backing nollpost state."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS sender_memory (
                    sender_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    preferred_action TEXT,
                    keep_subscribed INTEGER NOT NULL DEFAULT 0,
                    unsubscribed_at TEXT,
                    unsub_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS opened_tray (
                    message_uid TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    internal_id TEXT NOT NULL,
                    rfc_message_id TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    opened_target TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    swept_at TEXT,
                    dismissed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS missed_unsubscribes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    internal_id TEXT NOT NULL,
                    rfc_message_id TEXT NOT NULL DEFAULT '',
                    sender TEXT NOT NULL,
                    sender_domain TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    failure_reason TEXT NOT NULL,
                    candidate_links_json TEXT NOT NULL,
                    headers_snapshot TEXT NOT NULL,
                    footer_html_snapshot TEXT NOT NULL,
                    final_url TEXT NOT NULL DEFAULT '',
                    screenshot_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    resolved_note TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS action_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    account TEXT NOT NULL,
                    internal_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS extracted_cache (
                    cache_key TEXT PRIMARY KEY,
                    extracted_json TEXT NOT NULL,
                    cached_at TEXT NOT NULL
                );
                """
            )
            for key in STAT_KEYS:
                conn.execute(
                    "INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)",
                    (key,),
                )

    def get_stats(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM stats").fetchall()
            result = {k: 0 for k in STAT_KEYS}
            for row in rows:
                result[row["key"]] = int(row["value"])
            missed_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM missed_unsubscribes WHERE resolved = 0"
            ).fetchone()["cnt"]
            result["missed_unsub_pending"] = int(missed_count)
            return result

    def increment_stat(self, key: str, delta: int = 1) -> None:
        if delta <= 0:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO stats (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
                (key, delta),
            )

    @staticmethod
    def normalize_sender_key(sender_email: str, sender_domain: str = "") -> str:
        cleaned = sender_email.strip().lower()
        if cleaned:
            return cleaned
        return sender_domain.strip().lower()

    def get_sender_memory(self, sender_email: str, sender_domain: str = "") -> Optional[SenderMemoryRecord]:
        key = self.normalize_sender_key(sender_email, sender_domain)
        if not key:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sender_memory WHERE sender_key = ?",
                (key,),
            ).fetchone()
            if not row:
                return None
            return SenderMemoryRecord(
                sender_key=row["sender_key"],
                display_name=row["display_name"],
                preferred_action=row["preferred_action"],
                keep_subscribed=bool(row["keep_subscribed"]),
                unsubscribed_at=row["unsubscribed_at"],
                unsub_count=int(row["unsub_count"]),
                last_seen_at=row["last_seen_at"],
            )

    def record_sender_preference(
        self,
        sender_email: str,
        display_name: str,
        preferred_action: Optional[str],
        keep_subscribed: bool,
        sender_domain: str = "",
    ) -> None:
        key = self.normalize_sender_key(sender_email, sender_domain)
        if not key:
            return
        now = utc_now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sender_memory (
                    sender_key, display_name, preferred_action, keep_subscribed,
                    unsubscribed_at, unsub_count, last_seen_at
                ) VALUES (?, ?, ?, ?, NULL, 0, ?)
                ON CONFLICT(sender_key) DO UPDATE SET
                    display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE sender_memory.display_name END,
                    preferred_action = excluded.preferred_action,
                    keep_subscribed = excluded.keep_subscribed,
                    unsubscribed_at = CASE WHEN excluded.keep_subscribed = 1 THEN NULL ELSE sender_memory.unsubscribed_at END,
                    last_seen_at = excluded.last_seen_at
                """,
                (key, display_name, preferred_action, 1 if keep_subscribed else 0, now),
            )

    def record_unsubscribed(
        self,
        sender_email: str,
        display_name: str = "",
        sender_domain: str = "",
    ) -> bool:
        """Record successful unsub; returns True if this is the first time for sender."""
        key = self.normalize_sender_key(sender_email, sender_domain)
        now = utc_now_iso()
        with self._conn() as conn:
            existing = (
                conn.execute(
                    "SELECT unsub_count FROM sender_memory WHERE sender_key = ?",
                    (key,),
                ).fetchone()
                if key
                else None
            )
            first_time = existing is None or int(existing["unsub_count"]) == 0
            if key:
                conn.execute(
                    """
                    INSERT INTO sender_memory (
                        sender_key, display_name, preferred_action, keep_subscribed,
                        unsubscribed_at, unsub_count, last_seen_at
                    ) VALUES (?, ?, NULL, 0, ?, 1, ?)
                    ON CONFLICT(sender_key) DO UPDATE SET
                        display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE sender_memory.display_name END,
                        preferred_action = NULL,
                        keep_subscribed = 0,
                        unsubscribed_at = excluded.unsubscribed_at,
                        unsub_count = sender_memory.unsub_count + 1,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (key, display_name, now, now),
                )
            conn.execute(
                "UPDATE stats SET value = value + 1 WHERE key = 'total_unsubscribed'"
            )
            if first_time:
                conn.execute(
                    "UPDATE stats SET value = value + 1 WHERE key = 'unique_senders_unsubscribed'"
                )
            return first_time

    def list_sender_memory(
        self,
        *,
        search: str = "",
        filter_kind: str = "all",
    ) -> list[dict[str, Any]]:
        """Return learned sender rules and unsubscribe history ordered by last_seen_at DESC."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sender_memory ORDER BY last_seen_at DESC, sender_key ASC"
            ).fetchall()
        needle = search.strip().lower()
        out: list[dict[str, Any]] = []
        for row in rows:
            pref = row["preferred_action"]
            keep = bool(row["keep_subscribed"])
            unsub_at = row["unsubscribed_at"]
            unsub_cnt = int(row["unsub_count"])
            if filter_kind == "always_keep" and pref != "always_keep":
                continue
            elif filter_kind == "archive" and pref != "archive":
                continue
            elif filter_kind == "mark_read" and pref != "mark_read":
                continue
            elif filter_kind == "unsubscribed" and not (unsub_at or unsub_cnt > 0):
                continue
            elif filter_kind == "learned" and not (pref or keep):
                continue
            key = row["sender_key"]
            disp = row["display_name"]
            if needle and needle not in key.lower() and needle not in disp.lower():
                continue
            out.append({
                "sender_key": key,
                "display_name": disp,
                "preferred_action": pref,
                "keep_subscribed": keep,
                "unsubscribed_at": unsub_at,
                "unsub_count": unsub_cnt,
                "last_seen_at": row["last_seen_at"],
            })
        return out

    def delete_sender_memory(self, sender_keys: list[str]) -> int:
        """Delete entries from sender_memory and return number of deleted rows."""
        cleaned = [self.normalize_sender_key(k) for k in sender_keys if str(k).strip()]
        if not cleaned:
            return 0
        with self._conn() as conn:
            placeholders = ",".join("?" for _ in cleaned)
            cur = conn.execute(
                f"DELETE FROM sender_memory WHERE sender_key IN ({placeholders})",
                cleaned,
            )
            return int(cur.rowcount)

    def add_opened_tray_item(
        self,
        *,
        message_uid: str,
        account: str,
        mailbox: str,
        internal_id: str,
        rfc_message_id: str,
        subject: str,
        sender: str,
        opened_target: str,
    ) -> None:
        now = utc_now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO opened_tray (
                    message_uid, account, mailbox, internal_id, rfc_message_id,
                    subject, sender, opened_target, opened_at, swept_at, dismissed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                ON CONFLICT(message_uid) DO UPDATE SET
                    opened_target = excluded.opened_target,
                    opened_at = excluded.opened_at,
                    swept_at = NULL,
                    dismissed = 0
                """,
                (
                    message_uid,
                    account,
                    mailbox,
                    internal_id,
                    rfc_message_id,
                    subject,
                    sender,
                    opened_target,
                    now,
                ),
            )

    def list_active_tray(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM opened_tray
                WHERE swept_at IS NULL AND dismissed = 0
                ORDER BY opened_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_tray_swept(self, message_uids: list[str]) -> None:
        if not message_uids:
            return
        now = utc_now_iso()
        with self._conn() as conn:
            placeholders = ",".join("?" for _ in message_uids)
            conn.execute(
                f"UPDATE opened_tray SET swept_at = ? WHERE message_uid IN ({placeholders})",
                [now, *message_uids],
            )

    def dismiss_tray_items(self, message_uids: list[str]) -> None:
        if not message_uids:
            return
        with self._conn() as conn:
            placeholders = ",".join("?" for _ in message_uids)
            conn.execute(
                f"UPDATE opened_tray SET dismissed = 1 WHERE message_uid IN ({placeholders})",
                message_uids,
            )

    def prune_tray_not_in_inbox(self, active_inbox_uids: set[str]) -> None:
        """Auto-clear tray items that were already moved out of INBOX in Mail.app."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT message_uid FROM opened_tray WHERE swept_at IS NULL AND dismissed = 0"
            ).fetchall()
            gone = [r["message_uid"] for r in rows if r["message_uid"] not in active_inbox_uids]
            if gone:
                placeholders = ",".join("?" for _ in gone)
                conn.execute(
                    f"UPDATE opened_tray SET dismissed = 1 WHERE message_uid IN ({placeholders})",
                    gone,
                )

    def enqueue_missed_unsubscribe(
        self,
        *,
        account: str,
        mailbox: str,
        internal_id: str,
        rfc_message_id: str,
        sender: str,
        sender_domain: str,
        subject: str,
        stage: str,
        failure_reason: str,
        candidate_links: list[dict[str, str]],
        headers_snapshot: str,
        footer_html_snapshot: str,
        final_url: str = "",
        screenshot_path: str = "",
    ) -> int:
        """Store a missed/failed unsubscribe case for parser improvement, deduplicating unresolved repeats."""
        now = utc_now_iso()
        links_json = json.dumps(candidate_links, ensure_ascii=False)
        with self._conn() as conn:
            if rfc_message_id:
                dup = conn.execute(
                    "SELECT id FROM missed_unsubscribes "
                    "WHERE account = ? AND rfc_message_id = ? AND resolved = 0",
                    (account, rfc_message_id),
                ).fetchone()
            else:
                dup = conn.execute(
                    "SELECT id FROM missed_unsubscribes "
                    "WHERE account = ? AND sender_domain = ? AND subject = ? AND resolved = 0",
                    (account, sender_domain, subject),
                ).fetchone()
            if dup:
                row_id = int(dup["id"])
                conn.execute(
                    """
                    UPDATE missed_unsubscribes
                    SET stage = ?, failure_reason = ?, candidate_links_json = ?,
                        final_url = CASE WHEN ? != '' THEN ? ELSE final_url END,
                        screenshot_path = CASE WHEN ? != '' THEN ? ELSE screenshot_path END
                    WHERE id = ?
                    """,
                    (
                        stage,
                        failure_reason,
                        links_json,
                        final_url,
                        final_url,
                        screenshot_path,
                        screenshot_path,
                        row_id,
                    ),
                )
                return row_id

            cur = conn.execute(
                """
                INSERT INTO missed_unsubscribes (
                    account, mailbox, internal_id, rfc_message_id, sender, sender_domain,
                    subject, stage, failure_reason, candidate_links_json,
                    headers_snapshot, footer_html_snapshot, final_url, screenshot_path,
                    created_at, resolved, resolved_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')
                """,
                (
                    account,
                    mailbox,
                    internal_id,
                    rfc_message_id,
                    sender,
                    sender_domain,
                    subject,
                    stage,
                    failure_reason,
                    links_json,
                    headers_snapshot,
                    footer_html_snapshot,
                    final_url,
                    screenshot_path,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def list_missed_unsubscribes(self, include_resolved: bool = False) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if include_resolved:
                rows = conn.execute(
                    "SELECT * FROM missed_unsubscribes ORDER BY resolved ASC, id DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM missed_unsubscribes WHERE resolved = 0 ORDER BY id DESC"
                ).fetchall()
            out = []
            for row in rows:
                d = dict(row)
                try:
                    d["candidate_links"] = json.loads(d.pop("candidate_links_json", "[]"))
                except json.JSONDecodeError:
                    d["candidate_links"] = []
                out.append(d)
            return out

    def get_missed_unsubscribe(self, missed_id: int) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM missed_unsubscribes WHERE id = ?",
                (missed_id,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["candidate_links"] = json.loads(d.pop("candidate_links_json", "[]"))
            except json.JSONDecodeError:
                d["candidate_links"] = []
            return d

    def resolve_missed_unsubscribe(self, missed_id: int, note: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE missed_unsubscribes SET resolved = 1, resolved_note = ? WHERE id = ?",
                (note, missed_id),
            )

    def resolve_missed_for_message(
        self,
        *,
        account: str = "",
        internal_id: str = "",
        sender_domain: str = "",
        note: str = "Marked as unsubscribed by user",
    ) -> int:
        total = 0
        with self._conn() as conn:
            if account and internal_id:
                cur = conn.execute(
                    """
                    UPDATE missed_unsubscribes
                    SET resolved = 1, resolved_note = ?
                    WHERE resolved = 0 AND account = ? AND internal_id = ?
                    """,
                    (note, account, str(internal_id)),
                )
                total += max(0, int(cur.rowcount))
            if sender_domain:
                cur = conn.execute(
                    """
                    UPDATE missed_unsubscribes
                    SET resolved = 1, resolved_note = ?
                    WHERE resolved = 0 AND lower(sender_domain) = ?
                    """,
                    (note, sender_domain.strip().lower()),
                )
                total += max(0, int(cur.rowcount))
        return total

    def log_action(
        self,
        *,
        run_id: str,
        account: str,
        internal_id: str,
        sender: str,
        subject: str,
        action: str,
        status: str,
        detail: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO action_log (
                    run_id, account, internal_id, sender, subject, action, status, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, account, internal_id, sender, subject, action, status, detail, utc_now_iso()),
            )

    def get_cached_extraction(self, cache_key: str) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT extracted_json FROM extracted_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            try:
                return json.loads(row["extracted_json"])
            except json.JSONDecodeError:
                return None

    def set_cached_extraction(self, cache_key: str, extracted_dict: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO extracted_cache (cache_key, extracted_json, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    extracted_json = excluded.extracted_json,
                    cached_at = excluded.cached_at
                """,
                (cache_key, json.dumps(extracted_dict, ensure_ascii=False), utc_now_iso()),
            )
