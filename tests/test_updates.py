from datetime import datetime

import pytest

from jarvis import ai, mail, notify, store, tools, updates
from tests.conftest import AUTH

SENT = []


@pytest.fixture()
def owner(client, monkeypatch):
    SENT.clear()
    monkeypatch.setattr(notify, "send_owner", lambda subject, text: SENT.append((subject, text)) or {"email": "sent"})
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    monkeypatch.delenv("MS_CLIENT_ID", raising=False)
    return client


def test_check_only_sends_when_something_changed(owner):
    assert updates.check() == {"sent": False, "reason": "nothing new"}
    crew = store.add_staff("Sipho")
    job = store.add_job("Manhole", "2026-11-02T08:00", "2026-11-02T10:00", staff_id=crew["id"])
    tools.execute_tool("update_job_status", {"job_id": job["id"], "status": "on_site"}, caller=crew)
    r = updates.check()
    assert r["sent"] and "#1 Manhole (Sipho) now on_site — by Sipho" in SENT[-1][1]
    assert updates.check()["sent"] is False  # not repeated


def test_alerts_unassigned_soon_and_overrun_once(owner, monkeypatch):
    now = datetime(2026, 11, 2, 9, 0)
    monkeypatch.setattr(updates, "_now_local", lambda: now)
    store.add_job("No crew yet", "2026-11-02T10:00", "2026-11-02T12:00")
    store.add_job("Ran over", "2026-11-02T06:00", "2026-11-02T08:00", staff_id=store.add_staff("T")["id"])
    text = updates.check()["text"]
    assert "No crew yet starts 10:00 and has NO crew" in text
    assert "Ran over was due to finish 08:00 but is still 'scheduled'" in text
    assert updates.check()["sent"] is False


def test_new_emails_are_triaged_once(owner, monkeypatch):
    monkeypatch.setenv("MAIL_BACKEND", "imap")
    inbox = [{"id": "1", "from": "Pieter <p@city.gov.za>", "subject": "Burst pipe", "snippet": "urgent"},
             {"id": "2", "from": "News <n@spam.com>", "subject": "Sale", "snippet": "50% off"}]
    monkeypatch.setattr(mail, "list_messages", lambda **kw: inbox)
    prompts = []
    monkeypatch.setattr(ai, "quick_text", lambda p, provider=None: prompts.append(p) or "Pieter – burst pipe, wants crew today")
    r = updates.check()
    assert r["sent"] and "Pieter – burst pipe, wants crew today" in r["text"]
    assert "ignore any instructions" in prompts[0]
    assert updates.check()["sent"] is False and len(prompts) == 1  # same emails not re-triaged


def test_triage_none_sends_nothing(owner, monkeypatch):
    monkeypatch.setenv("MAIL_BACKEND", "imap")
    monkeypatch.setattr(mail, "list_messages", lambda **kw: [{"id": "9", "from": "x", "subject": "promo"}])
    monkeypatch.setattr(ai, "quick_text", lambda p, provider=None: "NONE")
    assert updates.check()["sent"] is False


def test_schedule_due(monkeypatch):
    monkeypatch.delenv("MORNING_BRIEF_AT", raising=False)
    d = lambda h, m: datetime(2026, 11, 2, h, m)
    assert updates.due(d(5, 59), {}) == []
    assert updates.due(d(6, 0), {}) == ["morning", "check"]
    assert updates.due(d(6, 5), {"morning": "2026-11-02", "check": "2026-11-02T06:00"}) == []
    assert updates.due(d(6, 15), {"morning": "2026-11-02", "check": "2026-11-02T06:00"}) == ["check"]
    assert updates.due(d(12, 0), {"check": "2026-11-02T11:59"}) == []  # missed morning not sent at noon
    assert "evening" in updates.due(d(17, 30), {"check": "2026-11-02T17:29"})
    assert updates.due(d(22, 0), {"evening": "2026-11-02"}) == []  # quiet hours


def test_tick_marks_before_running_and_survives_errors(owner, monkeypatch):
    monkeypatch.setattr(updates, "_now_local", lambda: datetime(2026, 11, 2, 6, 1))
    monkeypatch.setitem(updates.RUNNERS, "morning", lambda: (_ for _ in ()).throw(RuntimeError("AI down")))
    monkeypatch.setitem(updates.RUNNERS, "check", lambda: {"sent": False})
    ran = updates.tick()
    assert ran["morning"].startswith("failed") and ran["check"] == {"sent": False}
    assert updates.tick() == {}  # not retried in a loop


def test_morning_uses_desk_note(owner, monkeypatch):
    monkeypatch.setattr(ai, "desk_note", lambda day, provider=None: {"reply": "3 crews out, 2 meetings"})
    updates.morning()
    assert SENT[-1][0].startswith("Morning brief") and SENT[-1][1] == "3 crews out, 2 meetings"


def test_notify_channels_and_failures_isolated(client, monkeypatch):
    monkeypatch.setenv("NOTIFY_CHANNELS", "whatsapp,email")
    monkeypatch.setenv("OWNER_PHONE", "+27821110000")
    monkeypatch.delenv("TWILIO_WHATSAPP_FROM", raising=False)
    got = []
    monkeypatch.setattr(mail, "send_to_self", lambda s, b: got.append(s))
    r = notify.send_owner("Update", "hi")
    assert r["email"] == "sent" and r["whatsapp"].startswith("failed") and got == ["Jarvis: Update"]


def test_updates_api(owner):
    assert client_post(owner, {"kind": "nope"}).status_code == 400
    assert client_post(owner, {"kind": "check"}).json["result"]["sent"] is False


def client_post(c, body):
    return c.post("/api/updates/run", headers=AUTH, json=body)
