"""SQLite store for crew (staff), jobs and the call log.

Mirrors the phone app's store.ts (crew, jobs, group link) so the backend can be
the single source of truth that Claude, Grok and the SIP voice line all share.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT DEFAULT 'crew',
    phone TEXT,
    email TEXT,
    sip_ext TEXT,
    is_manager INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    customer TEXT,
    address TEXT,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    staff_id INTEGER REFERENCES staff(id),
    status TEXT DEFAULT 'scheduled',
    notes TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS email_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    cc TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    reply_to_id TEXT,
    status TEXT DEFAULT 'draft',
    sent_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    call_sid TEXT,
    caller TEXT,
    heard TEXT,
    reply TEXT,
    provider TEXT
);
"""


def db_path():
    return os.environ.get("JARVIS_DB", os.path.join(os.path.dirname(__file__), "..", "jarvis.db"))


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(db_path())
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        if "outlook_event_id" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN outlook_event_id TEXT")


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------- time helpers ----------

def parse_time(value):
    """Accept ISO strings like 2026-11-02T08:00 or '2026-11-02 08:00'."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).strip().replace("Z", ""))


def overlaps(a_start, a_end, b_start, b_end):
    return parse_time(a_start) < parse_time(b_end) and parse_time(b_start) < parse_time(a_end)


# ---------- staff ----------

def list_staff(include_inactive=False):
    with conn() as c:
        q = "SELECT * FROM staff" + ("" if include_inactive else " WHERE active=1") + " ORDER BY name"
        return _rows(c.execute(q))


def get_staff(staff_id):
    with conn() as c:
        r = c.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
        return dict(r) if r else None


def find_staff(name_or_id):
    """Find one active staff member by id or (case-insensitive, partial) name."""
    if name_or_id is None:
        return None
    s = str(name_or_id).strip()
    if s.isdigit():
        return get_staff(int(s))
    with conn() as c:
        exact = c.execute("SELECT * FROM staff WHERE active=1 AND lower(name)=lower(?)", (s,)).fetchone()
        if exact:
            return dict(exact)
        like = _rows(c.execute("SELECT * FROM staff WHERE active=1 AND lower(name) LIKE lower(?)", (f"%{s}%",)))
        return like[0] if len(like) == 1 else None


def staff_by_phone(phone):
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())[-9:]
    if not digits:
        return None
    for s in list_staff():
        if s.get("phone") and "".join(ch for ch in s["phone"] if ch.isdigit()).endswith(digits):
            return s
    return None


def add_staff(name, role="crew", phone=None, email=None, sip_ext=None, is_manager=False):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO staff (name, role, phone, email, sip_ext, is_manager) VALUES (?,?,?,?,?,?)",
            (name, role, phone, email, sip_ext, 1 if is_manager else 0),
        )
        sid = cur.lastrowid
    return get_staff(sid)


STAFF_FIELDS = {"name", "role", "phone", "email", "sip_ext", "is_manager", "active"}


def update_staff(staff_id, **fields):
    fields = {k: v for k, v in fields.items() if k in STAFF_FIELDS and v is not None}
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        with conn() as c:
            c.execute(f"UPDATE staff SET {sets} WHERE id=?", (*fields.values(), staff_id))
    return get_staff(staff_id)


# ---------- jobs ----------

def list_jobs(day=None, staff_id=None, status=None):
    q, args = "SELECT * FROM jobs WHERE 1=1", []
    if day:
        q += " AND substr(start,1,10)=?"
        args.append(str(day)[:10])
    if staff_id is not None:
        q += " AND staff_id=?"
        args.append(staff_id)
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY start"
    with conn() as c:
        return _rows(c.execute(q, args))


def get_job(job_id):
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None


def clashes(staff_id, start, end, ignore_job_id=None):
    """Jobs already booked for this staff member that overlap [start, end)."""
    if staff_id is None:
        return []
    out = []
    for j in list_jobs(staff_id=staff_id):
        if j["id"] == ignore_job_id or j["status"] == "cancelled":
            continue
        if overlaps(start, end, j["start"], j["end"]):
            out.append(j)
    return out


def add_job(title, start, end, customer=None, address=None, staff_id=None, notes=None):
    if parse_time(end) <= parse_time(start):
        raise ValueError("Job end must be after start")
    with conn() as c:
        cur = c.execute(
            "INSERT INTO jobs (title, customer, address, start, end, staff_id, notes) VALUES (?,?,?,?,?,?,?)",
            (title, customer, address, parse_time(start).isoformat(timespec="minutes"),
             parse_time(end).isoformat(timespec="minutes"), staff_id, notes),
        )
        jid = cur.lastrowid
    return get_job(jid)


JOB_FIELDS = {"title", "customer", "address", "start", "end", "staff_id", "status", "notes", "outlook_event_id"}


def update_job(job_id, **fields):
    clear = [k for k, v in fields.items() if k == "outlook_event_id" and v == ""]
    fields = {k: v for k, v in fields.items() if k in JOB_FIELDS and v is not None}
    for k in clear:
        fields[k] = None
    for k in ("start", "end"):
        if k in fields:
            fields[k] = parse_time(fields[k]).isoformat(timespec="minutes")
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        with conn() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
    return get_job(job_id)


# ---------- settings + call log ----------

def get_setting(key, default=None):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with conn() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def log_call(call_sid, caller, heard, reply, provider):
    with conn() as c:
        c.execute("INSERT INTO call_log (at, call_sid, caller, heard, reply, provider) VALUES (?,?,?,?,?,?)",
                  (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"), call_sid, caller, heard, reply, provider))


def recent_calls(limit=50):
    with conn() as c:
        return _rows(c.execute("SELECT * FROM call_log ORDER BY id DESC LIMIT ?", (limit,)))


# ---------- email drafts (Jarvis drafts, a human approves the send) ----------

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def add_draft(to_addr, subject, body, cc=None, reply_to_id=None):
    with conn() as c:
        cur = c.execute("INSERT INTO email_drafts (created, to_addr, cc, subject, body, reply_to_id) "
                        "VALUES (?,?,?,?,?,?)", (_now(), to_addr, cc, subject, body, reply_to_id))
        did = cur.lastrowid
    return get_draft(did)


def get_draft(draft_id):
    with conn() as c:
        r = c.execute("SELECT * FROM email_drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(r) if r else None


def list_drafts(status="draft"):
    with conn() as c:
        return _rows(c.execute("SELECT * FROM email_drafts WHERE status=? ORDER BY id DESC", (status,)))


def mark_draft(draft_id, status, error=None):
    with conn() as c:
        c.execute("UPDATE email_drafts SET status=?, error=?, sent_at=? WHERE id=?",
                  (status, error, _now() if status == "sent" else None, draft_id))
    return get_draft(draft_id)


# ---------- activity feed (what changed, for the owner's updates) ----------

def log_activity(kind, text):
    with conn() as c:
        c.execute("INSERT INTO activity (at, kind, text) VALUES (?,?,?)", (_now(), kind, text))


def activity_since(last_id=0):
    with conn() as c:
        return _rows(c.execute("SELECT * FROM activity WHERE id>? ORDER BY id", (last_id,)))
