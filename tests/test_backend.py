import json

from jarvis import ai, store, tools
from tests.conftest import AUTH


def seed():
    boss = store.add_staff("Mia Manager", "manager", phone="+27821110000", sip_ext="200", is_manager=True)
    sipho = store.add_staff("Sipho Dlamini", phone="+27822220000", sip_ext="201")
    return boss, sipho


def test_api_requires_key(client):
    assert client.get("/api/staff").status_code == 401
    assert client.get("/api/staff", headers=AUTH).status_code == 200


def test_clash_blocks_booking_until_forced(client):
    _, sipho = seed()
    r = client.post("/api/jobs", headers=AUTH, json={"title": "Install", "start": "2026-11-02T08:00",
                                                     "end": "2026-11-02T10:00", "staff": "sipho"})
    assert r.status_code == 200
    clash = {"title": "Repair", "start": "2026-11-02T09:30", "end": "2026-11-02T11:00", "staff": "Sipho"}
    r = client.post("/api/jobs", headers=AUTH, json=clash)
    assert r.status_code == 409 and r.json["clash"]
    assert client.post("/api/jobs", headers=AUTH, json={**clash, "force": True}).status_code == 200
    # back-to-back is not a clash
    r = client.post("/api/jobs", headers=AUTH, json={"title": "Survey", "start": "2026-11-02T11:00",
                                                     "end": "2026-11-02T12:00", "staff": str(sipho["id"])})
    assert r.status_code == 200


def test_reassign_and_status(client):
    boss, sipho = seed()
    job = client.post("/api/jobs", headers=AUTH, json={"title": "Fibre", "start": "2026-11-03T08:00",
                                                       "end": "2026-11-03T09:00"}).json["job"]
    r = client.patch(f"/api/jobs/{job['id']}", headers=AUTH, json={"staff": "Sipho", "status": "en_route"})
    assert r.status_code == 200 and r.json["staff_id"] == sipho["id"] and r.json["status"] == "en_route"



def test_crew_cannot_manage(client):
    boss, sipho = seed()
    mine = store.add_job("Mine", "2026-11-04T08:00", "2026-11-04T09:00", staff_id=sipho["id"])
    other = store.add_job("Other", "2026-11-04T10:00", "2026-11-04T11:00", staff_id=boss["id"])
    out, err = tools.execute_tool("add_staff", {"name": "X"}, caller=sipho)
    assert err and "managers" in out
    out, err = tools.execute_tool("update_job_status", {"job_id": other["id"], "status": "done"}, caller=sipho)
    assert err
    out, err = tools.execute_tool("update_job_status", {"job_id": mine["id"], "status": "done"}, caller=sipho)
    assert not err and json.loads(out)["job"]["status"] == "done"
    assert {t["name"] for t in tools.claude_tools("crew")} < {t["name"] for t in tools.claude_tools("manager")}


def test_day_sheet(client):
    _, sipho = seed()
    store.add_job("Install", "2026-11-05T08:00", "2026-11-05T10:00", address="12 Main Rd", staff_id=sipho["id"])
    store.add_job("Unassigned", "2026-11-05T13:00", "2026-11-05T14:00")
    client.put("/api/settings/whatsapp-group", headers=AUTH, json={"link": "https://chat.whatsapp.com/abc"})
    r = client.get("/api/day-sheet?day=2026-11-05", headers=AUTH).json
    assert "08:00–10:00 Sipho Dlamini: Install @ 12 Main Rd" in r["text"]
    assert "UNASSIGNED" in r["text"] and r["group_link"].endswith("abc")


def test_voice_rejects_unknown_caller(client):
    seed()
    r = client.post("/voice/incoming", data={"From": "+27839999999", "CallSid": "CA1"})
    assert b"registered staff only" in r.data and b"<Hangup/>" in r.data


def test_voice_sip_caller_and_agent(client, monkeypatch):
    seed()
    r = client.post("/voice/incoming", data={"From": "sip:201@jarvis.sip.twilio.com", "CallSid": "CA2"})
    assert b"Hi Sipho" in r.data and b"<Gather" in r.data

    seen = {}

    def fake_agent(message, provider=None, history=None, caller=None, voice=False):
        seen.update(message=message, caller=caller["name"], voice=voice, history=list(history))
        return {"reply": "You have one job at eight.", "actions": [], "provider": "claude"}

    monkeypatch.setattr(ai, "run_agent", fake_agent)
    r = client.post("/voice/gather", data={"From": "sip:201@jarvis.sip.twilio.com", "CallSid": "CA2",
                                           "SpeechResult": "What am I doing today?"})
    assert b"You have one job at eight." in r.data
    assert seen == {"message": "What am I doing today?", "caller": "Sipho Dlamini", "voice": True, "history": []}
    client.post("/voice/gather", data={"From": "+27822220000", "CallSid": "CA2", "SpeechResult": "And tomorrow?"})
    assert len(seen["history"]) == 2
    assert store.recent_calls()[0]["heard"] == "And tomorrow?"


def test_voice_transfer_to_sip_extension(client):
    seed()
    r = client.post("/voice/gather", data={"From": "+27822220000", "CallSid": "CA3",
                                           "SpeechResult": "Put me through to Mia"})
    assert b"<Sip>sip:200@jarvis.sip.twilio.com</Sip>" in r.data


def test_voice_signature_enforced(client, monkeypatch):
    seed()
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    assert client.post("/voice/incoming", data={"From": "+27822220000"}).status_code == 403


def test_sip_info(client):
    seed()
    r = client.get("/api/sip", headers=AUTH).json
    assert {"name": "Sipho Dlamini", "ext": "201", "uri": "sip:201@jarvis.sip.twilio.com"} in r["extensions"]


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_claude_loop_executes_tools(client, monkeypatch):
    seed()
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                return _Block(stop_reason="tool_use", content=[
                    _Block(type="tool_use", id="tu1", name="add_staff", input={"name": "Thandi"})])
            return _Block(stop_reason="end_turn", content=[_Block(type="text", text="Added Thandi.")])

    monkeypatch.setattr(ai, "_claude", lambda: _Block(beta=_Block(messages=FakeMessages())))
    out = ai.run_agent("Add Thandi to the crew", provider="claude")
    assert out["reply"] == "Added Thandi." and out["actions"][0]["result"]["ok"]
    assert store.find_staff("Thandi")
    assert calls[0]["model"] == "claude-opus-5-5"
    tr = calls[1]["messages"][-1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "tu1"


def test_grok_loop_executes_tools(client, monkeypatch):
    seed()
    monkeypatch.setenv("XAI_API_KEY", "x")
    replies = [
        {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_staff", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "Mia and Sipho are on."}}]},
    ]
    sent = []

    class R:
        status_code = 200

        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    def fake_post(url, json=None, **kw):
        sent.append(json)
        return R(replies[len(sent) - 1])

    monkeypatch.setattr(ai.requests, "post", fake_post)
    out = ai.run_agent("Who's working?", provider="grok")
    assert out["reply"] == "Mia and Sipho are on."
    assert sent[1]["messages"][-1]["role"] == "tool" and "Sipho" in sent[1]["messages"][-1]["content"]


def test_demo_routes_still_work(client):
    assert client.post("/api/next").status_code == 200
    assert client.get("/api/status").json["current_line"] == 1
