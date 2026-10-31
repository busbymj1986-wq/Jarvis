"""Outlook calendar (Microsoft Graph), the backend version of the app's outlook.functions.ts.

Write: every crew job is mirrored as an event in the owner's Outlook calendar (category
"Jarvis", shown as Free so it doesn't block the owner's own time). Moves, reassignments and
cancellations update or delete the event.
Pull: `pull_changes` reads those events back, so a job dragged to a new time in Outlook moves
in Jarvis too. `my_events` reads the owner's own meetings for the brief and clash checks.

Needs the same Microsoft app as email, plus the Calendars.ReadWrite application permission.
"""
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import store
from .mail import MailError, _graph

CATEGORY = "Jarvis"


def configured():
    if os.environ.get("OUTLOOK_CALENDAR", "1") == "0":
        return False
    return all(os.environ.get(k) for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_MAILBOX"))


def outlook_tz():
    return os.environ.get("OUTLOOK_TIMEZONE", "South Africa Standard Time")


def local_tz():
    return ZoneInfo(os.environ.get("CALENDAR_TZ", "Africa/Johannesburg"))


def _prefer():
    return {"Prefer": f'outlook.timezone="{outlook_tz()}"'}


def _dt(value):
    return {"dateTime": store.parse_time(value).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": outlook_tz()}


def _from_graph(dt):
    # Graph returns e.g. 2026-11-02T08:00:00.0000000 in the Prefer'd zone
    return datetime.fromisoformat(dt["dateTime"][:19]).isoformat(timespec="minutes")


def _event_body(job):
    staff = store.get_staff(job["staff_id"]) if job.get("staff_id") else None
    who = staff["name"] if staff else "UNASSIGNED"
    lines = [f"Jarvis job #{job['id']} · {job['status']}", f"Crew: {who}"]
    if job.get("customer"):
        lines.append(f"Customer: {job['customer']}")
    if job.get("address"):
        lines.append(f"Site: {job['address']}")
    if job.get("notes"):
        lines.append(f"Notes: {job['notes']}")
    event = {
        "subject": f"{job['title']} – {who}",
        "body": {"contentType": "Text", "content": "\n".join(lines)},
        "start": _dt(job["start"]),
        "end": _dt(job["end"]),
        "location": {"displayName": job.get("address") or ""},
        "categories": [CATEGORY],
        "showAs": "free",
        "isReminderOn": False,
    }
    if os.environ.get("CALENDAR_INVITE_STAFF") == "1" and staff and staff.get("email"):
        event["attendees"] = [{"emailAddress": {"address": staff["email"], "name": staff["name"]},
                               "type": "required"}]
    return event


def push_job(job):
    """Create, update or delete the Outlook event for a job. Returns a short status string."""
    if not configured() or not job:
        return None
    try:
        if job["status"] == "cancelled":
            if job.get("outlook_event_id"):
                try:
                    _graph("DELETE", f"/events/{job['outlook_event_id']}")
                except MailError as e:
                    if e.status != 404:
                        raise
                store.update_job(job["id"], outlook_event_id="")
            return "removed from Outlook"
        if job.get("outlook_event_id"):
            try:
                _graph("PATCH", f"/events/{job['outlook_event_id']}", json=_event_body(job))
                return "updated in Outlook"
            except MailError as e:
                if e.status != 404:
                    raise  # 404: event was deleted in Outlook, so create it again below
        ev = _graph("POST", "/events", json=_event_body(job))
        store.update_job(job["id"], outlook_event_id=ev["id"])
        return "added to Outlook"
    except MailError as e:
        return f"Outlook sync failed: {e}"


def _day_bounds(day=None, days=1):
    d = date.today() if not day or str(day).lower() == "today" else date.fromisoformat(str(day)[:10])
    start = datetime.combine(d, datetime.min.time(), tzinfo=local_tz())
    return start, start + timedelta(days=days)


def _view(start, end):
    params = {"startDateTime": start.isoformat(), "endDateTime": end.isoformat(), "$top": 100,
              "$orderby": "start/dateTime",
              "$select": "id,subject,start,end,location,categories,showAs,isAllDay,isCancelled,organizer"}
    return [e for e in _graph("GET", "/calendarView", params=params, headers=_prefer()).get("value", [])
            if not e.get("isCancelled")]


def my_events(day=None, days=1):
    """The owner's own meetings (Jarvis job events left out)."""
    start, end = _day_bounds(day, days)
    return [{"subject": e.get("subject"), "start": _from_graph(e["start"]), "end": _from_graph(e["end"]),
             "location": (e.get("location") or {}).get("displayName") or "", "all_day": e.get("isAllDay", False),
             "show_as": e.get("showAs")}
            for e in _view(start, end) if CATEGORY not in (e.get("categories") or [])]


def busy_between(start, end):
    """Owner meetings (not Free) overlapping [start, end)."""
    s, e = store.parse_time(start).replace(tzinfo=local_tz()), store.parse_time(end).replace(tzinfo=local_tz())
    out = []
    for ev in my_events(s.date().isoformat(), days=(e.date() - s.date()).days + 1):
        if ev["show_as"] != "free" and store.overlaps(start, end, ev["start"], ev["end"]):
            out.append(ev)
    return out


def create_meeting(subject, start, end, location=None, notes=None):
    """Put an entry in the owner's own calendar. No attendees, so no invites go out."""
    ev = _graph("POST", "/events", json={
        "subject": subject, "start": _dt(start), "end": _dt(end),
        "location": {"displayName": location or ""},
        "body": {"contentType": "Text", "content": notes or ""}})
    return {"id": ev.get("id"), "subject": subject, "start": start, "end": end}


def pull_changes(day=None, days=14):
    """Bring Outlook-side moves and deletions of Jarvis job events back into the jobs table."""
    if not configured():
        raise MailError("Outlook calendar is not configured.")
    changes = []
    start, end = _day_bounds(day, days)
    for job in store.list_jobs():
        if not job.get("outlook_event_id") or job["status"] == "cancelled":
            continue
        js = store.parse_time(job["start"]).replace(tzinfo=local_tz())
        if not (start <= js < end):
            continue
        try:
            ev = _graph("GET", f"/events/{job['outlook_event_id']}", headers=_prefer(),
                        params={"$select": "id,start,end,isCancelled"})
        except MailError as e:
            if e.status == 404:
                store.update_job(job["id"], outlook_event_id="")
                changes.append({"job_id": job["id"], "change": "event deleted in Outlook (job kept, will re-add on next edit)"})
                continue
            raise
        if ev.get("isCancelled"):
            continue
        new_start, new_end = _from_graph(ev["start"]), _from_graph(ev["end"])
        if (new_start, new_end) != (job["start"], job["end"]):
            store.update_job(job["id"], start=new_start, end=new_end)
            clash = store.clashes(job["staff_id"], new_start, new_end, ignore_job_id=job["id"])
            changes.append({"job_id": job["id"], "change": f"moved to {new_start}–{new_end[11:]}",
                            "clash_with": [c["id"] for c in clash]})
    return changes
