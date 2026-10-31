import json

import anyio

from jarvis import mcp_server, store, updates


def _list(monkeypatch, who=None):
    if who:
        monkeypatch.setenv("JARVIS_MCP_AS", who)
    else:
        monkeypatch.delenv("JARVIS_MCP_AS", raising=False)
    return [t.name for t in anyio.run(mcp_server.list_tools, None, None).tools]


def test_owner_gets_everything_plus_brief(client, monkeypatch):
    names = _list(monkeypatch)
    assert "add_staff" in names and "daily_brief" in names and not any("send" in n for n in names)


def test_scheduler_and_unknown_are_limited(client, monkeypatch):
    store.add_staff("Lerato", access="scheduler")
    assert "add_staff" not in _list(monkeypatch, "Lerato") and "create_job" in _list(monkeypatch, "Lerato")
    unknown = _list(monkeypatch, "Somebody Else")
    assert "create_job" not in unknown and "daily_brief" not in unknown
    out, err = mcp_server.run_tool("add_staff", {"name": "Hacker"})
    assert err and not store.find_staff("Hacker")


def test_call_tool_round_trip(client, monkeypatch):
    monkeypatch.delenv("JARVIS_MCP_AS", raising=False)

    class P:
        name, arguments = "add_staff", {"name": "Sipho"}
    res = anyio.run(mcp_server.call_tool, None, P())
    assert not res.is_error and json.loads(res.content[0].text)["staff"]["name"] == "Sipho"


def test_plain_brief_without_ai(client, monkeypatch):
    monkeypatch.delenv("MAIL_BACKEND", raising=False)
    monkeypatch.delenv("MS_CLIENT_ID", raising=False)
    s = store.add_staff("Sipho")
    store.add_staff("Thandi")
    store.add_job("Kerbs", "2026-11-10T07:00", "2026-11-10T15:00", address="Main Rd", staff_id=s["id"])
    store.add_job("Survey", "2026-11-10T13:00", "2026-11-10T14:00")
    text = updates.plain_brief("2026-11-10")
    assert "07:00–15:00 Sipho: Kerbs @ Main Rd" in text and "NO CREW: Survey" in text
    assert "Nothing booked for: Thandi" in text


def test_morning_falls_back_when_no_api_key(client, monkeypatch):
    from jarvis import ai, notify
    sent = []
    monkeypatch.setattr(notify, "send_owner", lambda subj, text: sent.append(text) or {})
    monkeypatch.setattr(ai, "desk_note", lambda day: (_ for _ in ()).throw(ai.AgentError(ai.NO_KEY_MSG)))
    updates.morning()
    assert sent and "Crew plan" in sent[0]
