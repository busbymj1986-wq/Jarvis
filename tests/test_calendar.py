import json

import pytest

from jarvis import calendar_sync, mail, store, tools
from tests.conftest import AUTH


class FakeGraph:
    """Tiny in-memory stand-in for the Graph calendar endpoints."""

    def __init__(self):
        self.events, self.n, self.calls = {}, 0, []

    def __call__(self, method, path, headers=None, **kw):
        self.calls.append((method, path))
        if path == "/calendarView":
            return {"value": list(self.events.values())}
        if method == "POST" and path == "/events":
            self.n += 1
            ev = {**kw["json"], "id": f"E{self.n}"}
            ev["start"] = {"dateTime": ev["start"]["dateTime"] + ".0000000"}
            ev["end"] = {"dateTime": ev["end"]["dateTime"] + ".0000000"}
            self.events[ev["id"]] = ev
            return ev
        eid = path.split("/")[-1]
        if eid not in self.events:
            raise mail.MailError("Outlook error 404", status=404)
        if method == "PATCH":
            self.events[eid].update(kw["json"])
            return self.events[eid]
        if method == "DELETE":
            del self.events[eid]
            return {}
        return self.events[eid]


@pytest.fixture()
def graph(client, monkeypatch):
    for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("MS_MAILBOX", "boss@civils.co.za")
    g = FakeGraph()
    monkeypatch.setattr(calendar_sync, "_graph", g)
    return g


def test_job_lifecycle_mirrors_to_outlook(graph):
    store.add_staff("Sipho Dlamini", email="sipho@civils.co.za")
    store.add_staff("Thandi")
    out = json.loads(tools.execute_tool("create_job", {"title": "Kerb install", "start": "2026-11-02T07:00",
                                                       "end": "2026-11-02T16:00", "staff": "Sipho",
                                                       "address": "Main Rd"})[0])
    assert out["outlook"] == "added to Outlook"
    eid = out["job"]["outlook_event_id"]
    ev = graph.events[eid]
    assert ev["subject"] == "Kerb install – Sipho Dlamini" and ev["showAs"] == "free"
    assert ev["categories"] == ["Jarvis"] and "attendees" not in ev
    assert ev["start"]["dateTime"].startswith("2026-11-02T07:00:00")

    out = json.loads(tools.execute_tool("assign_job", {"job_id": out["job"]["id"], "staff": "Thandi"})[0])
    assert out["outlook"] == "updated in Outlook" and graph.events[eid]["subject"].endswith("Thandi")

    out = json.loads(tools.execute_tool("update_job_status", {"job_id": out["job"]["id"], "status": "cancelled"})[0])
    assert out["outlook"] == "removed from Outlook" and eid not in graph.events
    assert store.get_job(out["job"]["id"])["outlook_event_id"] is None


def test_event_deleted_in_outlook_is_recreated_on_edit(graph):
    job = json.loads(tools.execute_tool("create_job", {"title": "Survey", "start": "2026-11-03T08:00",
                                                       "end": "2026-11-03T09:00"})[0])["job"]
    graph.events.clear()
    out = json.loads(tools.execute_tool("reschedule_job", {"job_id": job["id"], "start": "2026-11-03T10:00",
                                                           "end": "2026-11-03T11:00"})[0])
    assert out["outlook"] == "added to Outlook" and len(graph.events) == 1


def test_pull_moves_from_outlook(graph, monkeypatch):
    from datetime import date
    day = date.today().isoformat()
    s = store.add_staff("Sipho")
    job = json.loads(tools.execute_tool("create_job", {"title": "Pipe", "start": f"{day}T23:00",
                                                       "end": f"{day}T23:30", "staff": "Sipho"})[0])["job"]
    graph.events[job["outlook_event_id"]]["start"] = {"dateTime": f"{day}T22:00:00.0000000"}
    r = json.loads(tools.execute_tool("sync_outlook", {})[0])
    assert r["changes"][0]["change"].startswith(f"moved to {day}T22:00")
    assert store.get_job(job["id"])["start"] == f"{day}T22:00"


def test_my_calendar_hides_job_events_and_meeting_overlap(graph):
    tools.execute_tool("create_job", {"title": "Job", "start": "2026-11-04T08:00", "end": "2026-11-04T09:00"})
    out = json.loads(tools.execute_tool("create_meeting", {"subject": "Site meeting with engineer",
                                                           "start": "2026-11-04T10:00", "end": "2026-11-04T11:00",
                                                           "location": "Gate 3"})[0])
    assert out["ok"] and out["overlaps"] == []
    events = json.loads(tools.execute_tool("my_calendar", {"day": "2026-11-04"})[0])
    assert [e["subject"] for e in events] == ["Site meeting with engineer"]
    out = json.loads(tools.execute_tool("create_meeting", {"subject": "Bank", "start": "2026-11-04T10:30",
                                                           "end": "2026-11-04T11:30"})[0])
    assert [e["subject"] for e in out["overlaps"]] == ["Site meeting with engineer"]


def test_calendar_tools_hidden_when_not_configured_and_for_crew(client, monkeypatch):
    monkeypatch.delenv("MS_CLIENT_ID", raising=False)
    assert "my_calendar" not in {t["name"] for t in tools.claude_tools("manager")}


def test_crew_cannot_use_calendar(graph):
    crew = store.add_staff("Sipho")
    assert "my_calendar" not in {t["name"] for t in tools.claude_tools("crew")}
    assert tools.execute_tool("create_meeting", {"subject": "x", "start": "2026-11-04T10:00",
                                                 "end": "2026-11-04T11:00"}, caller=crew)[1]


def test_api_sync_pushes_unsynced_jobs(graph, client):
    store.add_job("Old import", "2099-01-01T08:00", "2099-01-01T09:00")  # created without Outlook
    r = client.post("/api/calendar/sync", headers=AUTH).json
    assert r["pushed"][0]["outlook"] == "added to Outlook"


def test_outlook_failure_does_not_lose_job(graph, monkeypatch):
    def boom(*a, **k):
        raise mail.MailError("Outlook error 403: Forbidden", status=403)
    monkeypatch.setattr(calendar_sync, "_graph", boom)
    out = json.loads(tools.execute_tool("create_job", {"title": "Still booked", "start": "2026-11-05T08:00",
                                                       "end": "2026-11-05T09:00"})[0])
    assert out["ok"] and out["outlook"].startswith("Outlook sync failed") and store.get_job(out["job"]["id"])
