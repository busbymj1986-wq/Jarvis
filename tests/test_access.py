import json

from jarvis import ai, store, tools
from tests.conftest import AUTH


def setup_people(client):
    lerato = client.post("/api/staff", headers=AUTH, json={"name": "Lerato Scheduler", "access": "scheduler",
                                                           "phone": "+27823330000", "sip_ext": "210"}).json["staff"]
    sipho = client.post("/api/staff", headers=AUTH, json={"name": "Sipho Crew", "phone": "+27822220000"}).json["staff"]
    key = client.post("/api/keys", headers=AUTH, json={"staff": "Lerato", "label": "office pc"}).json
    crew_key = client.post("/api/keys", headers=AUTH, json={"staff": "Sipho"}).json
    return lerato, sipho, {"Authorization": f"Bearer {key['key']}"}, {"Authorization": f"Bearer {crew_key['key']}"}, key


def test_scheduler_can_schedule(client):
    lerato, sipho, S, _, key = setup_people(client)
    assert key["access"] == "scheduler" and key["key"].startswith("jv_") and lerato["role"] == "scheduler"
    r = client.post("/api/jobs", headers=S, json={"title": "Trench", "start": "2026-11-10T07:00",
                                                  "end": "2026-11-10T12:00", "staff": "Sipho"})
    assert r.status_code == 200
    jid = r.json["job"]["id"]
    assert client.patch(f"/api/jobs/{jid}", headers=S, json={"start": "2026-11-10T08:00",
                                                             "end": "2026-11-10T13:00"}).status_code == 200
    assert client.patch(f"/api/jobs/{jid}", headers=S, json={"status": "done"}).status_code == 200  # any job
    assert client.get("/api/staff", headers=S).status_code == 200
    assert client.get("/api/day-sheet", headers=S).status_code == 200
    acts = client.get("/api/activity", headers=S).json
    assert acts and all(a["kind"] == "job" for a in acts) and "by Lerato Scheduler" in acts[0]["text"]


def test_scheduler_blocked_from_admin_things(client):
    lerato, sipho, S, _, _ = setup_people(client)
    blocked = [("post", "/api/staff", {"name": "X"}), ("patch", f"/api/staff/{sipho['id']}", {"access": "admin"}),
               ("delete", f"/api/staff/{sipho['id']}", None), ("get", "/api/email/inbox", None),
               ("post", "/api/email/drafts/1/send", None), ("get", "/api/calendar", None),
               ("post", "/api/calendar/sync", None), ("post", "/api/updates/run", {"kind": "check"}),
               ("post", "/api/desk-note", {}), ("post", "/api/keys", {"staff": "Lerato"}), ("get", "/api/keys", None),
               ("put", "/api/settings/whatsapp-group", {"link": "x"}), ("get", "/api/calls", None),
               ("get", "/api/sip", None)]
    for method, url, body in blocked:
        r = getattr(client, method)(url, headers=S, json=body)
        assert r.status_code == 403, (method, url, r.status_code)
    # cannot promote herself through the agent's tools either
    out, err = tools.execute_tool("update_staff", {"staff": "Lerato", "access": "admin"}, caller=lerato)
    assert err and store.access_of(store.get_staff(lerato["id"])) == "scheduler"
    r = client.post("/api/agent", headers=S, json={"message": "send draft 1"})
    assert r.status_code == 403


def test_scheduler_agent_gets_only_scheduling_tools(client, monkeypatch):
    lerato, *_ = setup_people(client)
    names = {t["name"] for t in tools.claude_tools("scheduler")}
    assert names == {"list_staff", "list_jobs", "check_clashes", "create_job", "assign_job", "reschedule_job",
                     "update_job_status", "notify_staff", "day_sheet"}
    assert "scheduling rights only" in ai.system_prompt(lerato)
    seen = {}
    monkeypatch.setattr(ai, "run_claude", lambda m, history=None, caller=None, voice=False:
                        seen.update(caller=caller) or {"reply": "ok", "actions": []})
    S = {"Authorization": "Bearer " + client.post("/api/keys", headers=AUTH, json={"staff": "Lerato"}).json["key"]}
    client.post("/api/agent", headers=S, json={"message": "Who's free Thursday?"})
    assert seen["caller"]["name"] == "Lerato Scheduler"


def test_crew_key_is_limited(client):
    _, sipho, _, C, _ = setup_people(client)
    assert client.get("/api/jobs", headers=C).status_code == 200
    assert client.post("/api/jobs", headers=C, json={"title": "x", "start": "2026-11-10T07:00",
                                                     "end": "2026-11-10T08:00"}).status_code == 403
    assert client.get("/api/staff", headers=C).status_code == 403


def test_revoked_or_deactivated_key_stops_working(client):
    lerato, _, S, _, key = setup_people(client)
    assert client.get("/api/jobs", headers=S).status_code == 200
    client.delete(f"/api/keys/{key['id']}", headers=AUTH)
    assert client.get("/api/jobs", headers=S).status_code == 401
    S2 = {"Authorization": "Bearer " + client.post("/api/keys", headers=AUTH, json={"staff": "Lerato"}).json["key"]}
    client.delete(f"/api/staff/{lerato['id']}", headers=AUTH)  # deactivate
    assert client.get("/api/jobs", headers=S2).status_code == 401
    assert all(k.get("key") is None for k in client.get("/api/keys", headers=AUTH).json)  # never re-shown


def test_scheduler_on_the_phone(client, monkeypatch):
    setup_people(client)
    seen = {}
    monkeypatch.setattr(ai, "run_agent", lambda m, history=None, caller=None, voice=False:
                        seen.update(level=store.access_of(caller)) or {"reply": "Done.", "actions": []})
    client.post("/voice/gather", data={"From": "sip:210@x", "CallSid": "C1", "SpeechResult": "Move job 1 to 9"})
    assert seen["level"] == "scheduler"
    d = store.add_draft("a@b.com", "s", "b")
    r = client.post("/voice/gather", data={"From": "sip:210@x", "CallSid": "C1", "SpeechResult": f"send draft {d['id']}"})
    assert b"Only a manager" in r.data


def test_existing_managers_still_admin(client):
    old = store.add_staff("Old Boss", is_manager=True)
    assert store.access_of(old) == "admin"
    legacy = dict(old, access=None)  # rows from before the access column
    assert store.access_of(legacy) == "admin"
    assert json.loads(tools.execute_tool("add_staff", {"name": "New"}, caller=old)[0])["ok"]


def test_chat_page_and_me(client):
    r = client.get("/")
    assert r.status_code == 200 and b"<title>Jarvis</title>" in r.data
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert client.get("/health").json["status"] == "online"
    assert client.get("/", headers={"Accept": "application/json"}).json["status"] == "online"
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers=AUTH).json["access"] == "admin"
    lerato, _, S, C, _ = setup_people(client)
    me = client.get("/api/me", headers=S).json
    assert (me["name"], me["access"], me["email"], me["calendar"]) == ("Lerato Scheduler", "scheduler", False, False)
    assert client.get("/api/me", headers=C).json["access"] == "crew"


def test_page_never_injects_html():
    import re
    src = open("jarvis/static/index.html").read()
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src and not re.search(r"\beval\(", src)


def test_page_knows_when_chat_is_unavailable(client, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert client.get("/api/me", headers=AUTH).json["chat"] is False
    from jarvis import ai
    monkeypatch.setattr(ai, "desk_note", lambda day: (_ for _ in ()).throw(ai.AgentError("no key")))
    r = client.post("/api/desk-note", headers=AUTH, json={})
    assert r.status_code == 200 and "Crew plan" in r.json["reply"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert client.get("/api/me", headers=AUTH).json["chat"] is True
