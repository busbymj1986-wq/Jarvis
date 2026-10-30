import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from flask import Flask

from conftest import JPEG, PERSON, Clock, FakeAnalyzer, FakeNvr, nvr_push
from vigi_bridge import init_vigi, whatsapp
from vigi_bridge.service import VigiBridge

ME, PARTNER = "27820000001", "27820000002"
WA = {"token": "tok", "phone_number_id": "PNID", "recipients": ["+27 82 000 0001", PARTNER],
      "app_secret": "appsecret", "verify_token": "verify-me", "alarm_template": "security_alarm"}


class FakeGraph:
    """Stands in for graph.facebook.com."""

    def __init__(self):
        self.uploads, self.messages = [], []
        self.fail = {}

    def __call__(self, url, headers=None, data=None, files=None, json=None, timeout=None):
        assert headers["Authorization"] == "Bearer tok"
        if url.endswith("/media"):
            assert url == "https://graph.facebook.com/v23.0/PNID/media"
            self.uploads.append((data["type"], files["file"][0]))
            return self._resp(200, {"id": f"MEDIA{len(self.uploads)}"})
        assert url == "https://graph.facebook.com/v23.0/PNID/messages"
        self.messages.append(json)
        if json["to"] in self.fail:
            code, msg = self.fail[json["to"]]
            return self._resp(400, {"error": {"code": code, "message": msg}})
        return self._resp(200, {"messages": [{"id": "wamid.1"}]})

    @staticmethod
    def _resp(status, body):
        return SimpleNamespace(status_code=status, json=lambda: body, text=str(body))

    def to(self, number):
        return [m for m in self.messages if m["to"] == number]


@pytest.fixture
def graph(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(whatsapp.requests, "post", g)
    return g


@pytest.fixture
def wa_bridge(make_cfg, graph):
    def _make(clock="2026-10-30 23:00:00", analyzer=None, **wa):
        cfg = make_cfg(notify={"whatsapp": {**WA, **wa}})
        bridge = VigiBridge(cfg, nvr=FakeNvr(), analyzer=analyzer or FakeAnalyzer(enabled=False),
                            clock=Clock(clock))
        bridge.whatsapp.dispatch = bridge.whatsapp.handle  # run commands inline in tests
        return bridge
    return _make


def inbound(text, sender=ME, msg_id="wamid.in1"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"from": sender, "id": msg_id, "type": "text", "text": {"body": text}}]}}]}]}


def raise_alarm(bridge, **push):
    [event_id] = bridge.ingest(nvr_push(**push), JPEG)
    return bridge.process(event_id)


# --- alerts out --------------------------------------------------------------

def test_alarm_uses_template_outside_the_24h_window(wa_bridge, graph):
    bridge = wa_bridge(analyzer=FakeAnalyzer(verdict=PERSON))
    assert [n.name for n in bridge.notifiers] == ["whatsapp"]
    event = raise_alarm(bridge, channel=1)
    assert event["notified_via"] == ["whatsapp"]
    assert graph.uploads == [("image/jpeg", f"event_{event['id']}.jpg")]  # uploaded once, sent to both
    for number in (ME, PARTNER):
        [msg] = graph.to(number)
        tpl = msg["template"]
        assert msg["type"] == "template" and tpl["name"] == "security_alarm"
        assert tpl["language"] == {"code": "en"}
        header, body = tpl["components"]
        assert header == {"type": "header", "parameters": [{"type": "image", "image": {"id": "MEDIA1"}}]}
        assert [p["text"] for p in body["parameters"]] == [
            "Cam 1", "Intrusion at 23:00:00 on 30 Oct", "One person climbing the gate"]


def test_alarm_is_free_form_inside_the_window(wa_bridge, graph):
    bridge = wa_bridge()
    bridge.whatsapp_windows.touch(ME)
    raise_alarm(bridge, channel=3)
    [mine] = graph.to(ME)
    assert mine["type"] == "image" and mine["image"]["id"] == "MEDIA1"
    assert mine["image"]["caption"].startswith("🚨 ALARM - Cam 3")
    assert "Reply *snap 3* for a live photo" in mine["image"]["caption"]
    assert graph.to(PARTNER)[0]["type"] == "template"


def test_window_expires_after_a_day(wa_bridge, graph):
    bridge = wa_bridge()
    bridge.whatsapp_windows.touch(ME)
    bridge.now.set("2026-10-31 22:55:00")
    assert bridge.whatsapp_windows.is_open(ME) is False


def test_without_template_failures_are_reported(wa_bridge, graph, caplog):
    bridge = wa_bridge(alarm_template="")
    graph.fail = {ME: (131047, "Re-engagement message"), PARTNER: (131047, "Re-engagement message")}
    with caplog.at_level("WARNING"):
        event = raise_alarm(bridge)
    assert event["status"] == "alarm"
    assert event["notified_via"] == []
    assert "haven't messaged the bot in 24 hours" in caplog.text


def test_one_failed_recipient_does_not_block_the_other(wa_bridge, graph):
    bridge = wa_bridge()
    graph.fail = {PARTNER: (131026, "Message undeliverable")}
    assert raise_alarm(bridge)["notified_via"] == ["whatsapp"]
    assert len(graph.to(ME)) == 1


def test_clip_goes_as_video_inside_window_only_without_template(wa_bridge, graph):
    bridge = wa_bridge()
    bridge.whatsapp_windows.touch(ME)
    event = raise_alarm(bridge)
    graph.messages.clear()
    bridge.capture_clip(event["id"])
    [video] = graph.to(ME)
    assert video["type"] == "video" and video["video"]["caption"].startswith("🎥 Clip - Cam 1")
    assert graph.to(PARTNER) == []  # no clip_template, window closed


def test_clip_template_outside_window(wa_bridge, graph):
    bridge = wa_bridge(clip_template="security_clip")
    event = raise_alarm(bridge)
    graph.messages.clear()
    bridge.capture_clip(event["id"])
    tpl = graph.to(PARTNER)[0]["template"]
    assert tpl["name"] == "security_clip"
    assert tpl["components"][0]["parameters"][0]["type"] == "video"
    assert len(tpl["components"][1]["parameters"]) == 2


def test_report_text_in_window_and_single_line_template_outside(wa_bridge, graph):
    bridge = wa_bridge(report_template="security_report")
    bridge.whatsapp_windows.touch(ME)
    raise_alarm(bridge)
    graph.messages.clear()
    bridge.now.set("2026-10-31 07:01:00")
    assert bridge.send_daily_report_if_due() is True
    [mine] = graph.to(ME)
    assert mine["text"]["body"].startswith("*Jarvis security report 30 Oct 07:00 - 31 Oct 07:00*")
    params = graph.to(PARTNER)[0]["template"]["components"][0]["parameters"]
    assert all("\n" not in p["text"] for p in params)
    assert params[1]["text"].startswith("Events: 1 | Alarms sent: 1")


# --- webhook and commands ----------------------------------------------------

@pytest.fixture
def wa_client(make_cfg, graph):
    app = Flask(__name__)
    bridge = init_vigi(app, make_cfg(notify={"whatsapp": WA}), start=False, nvr=FakeNvr(),
                       analyzer=FakeAnalyzer(enabled=False), clock=Clock("2026-10-30 23:00:00"))
    bridge.whatsapp.dispatch = bridge.whatsapp.handle
    client = app.test_client()
    client.bridge = bridge
    return client


def signed_post(client, payload, secret="appsecret"):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/vigi/whatsapp/webhook", data=raw, content_type="application/json",
                       headers={"X-Hub-Signature-256": sig})


def test_webhook_verification(wa_client):
    ok = wa_client.get("/vigi/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=12345")
    assert ok.status_code == 200 and ok.get_data(as_text=True) == "12345"
    bad = wa_client.get("/vigi/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=1")
    assert bad.status_code == 403


def test_webhook_rejects_bad_signature(wa_client, graph):
    assert signed_post(wa_client, inbound("disarm"), secret="wrong").status_code == 401
    assert wa_client.bridge.rules.arm_mode() == "schedule"
    assert graph.messages == []


def test_disarm_and_arm_from_whatsapp(wa_client, graph):
    assert signed_post(wa_client, inbound("Disarm")).status_code == 200
    assert wa_client.bridge.rules.arm_mode() == "disarmed"
    [reply] = graph.to(ME)
    assert reply["text"]["body"].startswith("🟢 *Disarmed* - system disarmed")
    assert wa_client.bridge.whatsapp_windows.is_open(ME)

    signed_post(wa_client, inbound("jarvis arm", msg_id="wamid.in2"))
    assert wa_client.bridge.rules.arm_mode() == "armed"
    assert graph.to(ME)[-1]["text"]["body"].startswith("🔴 *ARMED*")


def test_unlisted_numbers_and_redeliveries_are_ignored(wa_client, graph):
    signed_post(wa_client, inbound("disarm", sender="27829999999", msg_id="wamid.stranger"))
    assert wa_client.bridge.rules.arm_mode() == "schedule"
    assert graph.messages == []
    signed_post(wa_client, inbound("status"))
    signed_post(wa_client, inbound("status"))  # same message id redelivered
    assert len(graph.to(ME)) == 1


def test_snap_and_live_clip_commands(wa_bridge, graph):
    bridge = wa_bridge()
    bridge.cfg["rules"]["channels"] = {"2": {"name": "Driveway"}}
    bridge.whatsapp.handle(inbound("snap 2"))
    assert bridge.nvr.snapshot_calls == [2]
    [photo] = graph.to(ME)
    assert photo["type"] == "image" and photo["image"]["caption"] == "📷 Driveway - live at 23:00:00"

    bridge.whatsapp.handle(inbound("clip 2 45", msg_id="wamid.in2"))
    assert bridge.nvr.clip_calls == [(2, 30)]  # capped at 30 s
    texts = [m for m in graph.to(ME) if m["type"] == "text"]
    assert texts[-1]["text"]["body"] == "Recording 30 s from Driveway..."
    assert graph.to(ME)[-1]["type"] == "video"
    assert graph.uploads[-1] == ("video/mp4", "live_2.mp4")

    bridge.whatsapp.handle(inbound("snap", msg_id="wamid.in3"))
    assert graph.to(ME)[-1]["text"]["body"].startswith("Which camera?")


def test_alarms_event_report_and_help_commands(wa_bridge, graph):
    bridge = wa_bridge(analyzer=FakeAnalyzer(verdict=PERSON))
    event = raise_alarm(bridge, channel=1)
    bridge.capture_clip(event["id"])
    graph.messages.clear()

    bridge.whatsapp.handle(inbound("alarms"))
    body = graph.to(ME)[-1]["text"]["body"]
    assert "Fri 30 Oct 23:00 Cam 1: One person climbing the gate. (event 1)" in body

    bridge.whatsapp.handle(inbound("event 1", msg_id="wamid.in2"))
    photo, video = graph.to(ME)[-2:]
    assert photo["type"] == "image" and "Event 1: intrusion at Cam 1" in photo["image"]["caption"]
    assert video["type"] == "video"

    bridge.now.set("2026-10-30 23:05:00")
    bridge.whatsapp.handle(inbound("report 7d", msg_id="wamid.in3"))
    assert "Alarms sent: 1" in graph.to(ME)[-1]["text"]["body"]
    bridge.whatsapp.handle(inbound("report 1y", msg_id="wamid.in4"))
    assert graph.to(ME)[-1]["text"]["body"].startswith("Use *report 24h*")

    bridge.whatsapp.handle(inbound("what can you do", msg_id="wamid.in5"))
    assert graph.to(ME)[-1]["text"]["body"] == whatsapp.HELP


def test_failed_delivery_status_is_logged_with_hint(wa_bridge, caplog):
    bridge = wa_bridge()
    payload = {"entry": [{"changes": [{"value": {"statuses": [{
        "id": "wamid.1", "status": "failed", "recipient_id": PARTNER,
        "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
    with caplog.at_level("WARNING"):
        bridge.whatsapp.handle(payload)
    assert "outside the 24-hour window" in caplog.text


def test_status_endpoint_reports_whatsapp(wa_client):
    status = wa_client.get("/vigi/status", headers={"X-API-Key": "secret-key"}).get_json()
    assert status["notifiers"] == ["whatsapp"]
    assert status["whatsapp_commands"] is True
