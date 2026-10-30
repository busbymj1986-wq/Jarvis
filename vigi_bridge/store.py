"""SQLite storage for events, settings and the snapshot/clip files on disk."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    event_time TEXT NOT NULL,
    source TEXT,
    device_id TEXT,
    device_name TEXT,
    channel INTEGER,
    channel_name TEXT,
    camera_key TEXT,
    event_type INTEGER,
    event_subtype INTEGER,
    label TEXT,
    labels TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    armed INTEGER,
    reason TEXT,
    ai TEXT,
    snapshot TEXT,
    clip TEXT,
    notified_at TEXT,
    notified_via TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time);
CREATE INDEX IF NOT EXISTS idx_events_camera ON events(camera_key, event_time);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

JSON_FIELDS = ("labels", "ai", "raw", "notified_via")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.media_dir = os.path.join(self.data_dir, "media")
        os.makedirs(self.media_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(os.path.join(self.data_dir, "vigi.db"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    # --- events -------------------------------------------------------------

    def add_event(self, ev, received_at: datetime) -> int:
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO events (received_at, event_time, source, device_id, device_name,
                   channel, channel_name, camera_key, event_type, event_subtype, label, labels, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    iso(received_at), iso(ev.event_time), ev.source, ev.device_id, ev.device_name,
                    ev.channel, ev.channel_name, ev.camera_key, ev.event_type, ev.event_subtype,
                    ev.label, json.dumps(ev.labels), json.dumps(ev.raw, default=str),
                ),
            )
            self._db.commit()
            return cur.lastrowid

    def update_event(self, event_id: int, **fields):
        if not fields:
            return
        values = [json.dumps(v) if k in JSON_FIELDS and v is not None else v for k, v in fields.items()]
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE events SET {cols} WHERE id = ?", (*values, event_id))
            self._db.commit()

    def _row(self, row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for key in JSON_FIELDS:
            if d.get(key):
                d[key] = json.loads(d[key])
        return d

    def get_event(self, event_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._row(row)

    def list_events(self, since=None, until=None, channel=None, status=None, label=None,
                    limit: int | None = 100, offset: int = 0, newest_first: bool = True) -> list[dict]:
        where, args = [], []
        if since is not None:
            where.append("event_time >= ?")
            args.append(iso(since))
        if until is not None:
            where.append("event_time < ?")
            args.append(iso(until))
        if channel is not None:
            where.append("channel = ?")
            args.append(channel)
        if status:
            where.append("status = ?")
            args.append(status)
        if label:
            where.append("(label = ? OR labels LIKE ?)")
            args.extend([label, f'%"{label}%'])
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY event_time {0}, id {0}".format("DESC" if newest_first else "ASC")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args.extend([limit, offset])
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def last_alarm(self, camera_key: str, since: datetime, exclude_id: int | None = None) -> dict | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM events WHERE camera_key = ? AND status = 'alarm'
                   AND event_time >= ? AND id != ? ORDER BY event_time DESC LIMIT 1""",
                (camera_key, iso(since), exclude_id or -1),
            ).fetchone()
        return self._row(row)

    def armed_activity(self, since: datetime, until: datetime, exclude_id: int | None = None) -> tuple[int, set]:
        """Count armed events (any type) and distinct cameras in a time window."""
        with self._lock:
            rows = self._db.execute(
                """SELECT camera_key FROM events WHERE armed = 1 AND event_time >= ?
                   AND event_time <= ? AND id != ?""",
                (iso(since), iso(until), exclude_id or -1),
            ).fetchall()
        return len(rows), {r["camera_key"] for r in rows}

    def purge_before(self, cutoff: datetime) -> int:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, snapshot, clip FROM events WHERE event_time < ?", (iso(cutoff),)
            ).fetchall()
            for r in rows:
                for rel in (r["snapshot"], r["clip"]):
                    path = self.abspath(rel)
                    if path and os.path.exists(path):
                        os.remove(path)
            self._db.execute("DELETE FROM events WHERE event_time < ?", (iso(cutoff),))
            self._db.commit()
        return len(rows)

    # --- settings -----------------------------------------------------------

    def get_setting(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self._lock:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    def claim_setting(self, key: str, value: str) -> bool:
        """Set key to value only if it differs; True if this call changed it.
        Lets several worker processes agree on who sends the daily report."""
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key)
                   DO UPDATE SET value = excluded.value WHERE settings.value IS NOT excluded.value""",
                (key, value),
            )
            self._db.commit()
            return cur.rowcount > 0

    # --- media --------------------------------------------------------------

    def media_relpath(self, event_id: int, when: datetime, ext: str) -> str:
        return os.path.join(when.strftime("%Y/%m/%d"), f"event_{event_id}.{ext}")

    def abspath(self, rel: str | None) -> str | None:
        if not rel:
            return None
        path = os.path.abspath(os.path.join(self.media_dir, rel))
        return path if path.startswith(self.media_dir + os.sep) else None

    def save_media(self, event_id: int, when: datetime, ext: str, data: bytes) -> str:
        rel = self.media_relpath(event_id, when, ext)
        path = self.abspath(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return rel

    def read_media(self, rel: str | None) -> bytes | None:
        path = self.abspath(rel)
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        return None
