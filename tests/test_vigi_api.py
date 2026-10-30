import io
import json

from conftest import JPEG, nvr_push

KEY = {"X-API-Key": "secret-key"}


def post_alarm(client, payload, token="nvr-token"):
    return client.post(f"/vigi/alarm/{token}", data=json.dumps(payload), content_type="application/json")


def test_ingest_requires_token(client):
    assert post_alarm(client, nvr_push(), token="wrong").status_code == 401
    resp = post_alarm(client, nvr_push())
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "events": [1]}
    assert client.post("/vigi/alarm?token=nvr-token", json=nvr_push()).status_code == 200


def test_nvr_test_button_and_bad_body(client):
    assert post_alarm(client, {"device_id": "x", "messages": []}).get_json() == {"ok": True, "events": []}
    resp = client.post("/vigi/alarm/nvr-token", data="not json", content_type="application/json")
    assert resp.status_code == 400


def test_multipart_push_with_snapshot(client):
    data = {
        "event": (io.BytesIO(json.dumps(nvr_push(channel=3)).encode()), "event.json", "application/json"),
        "snapshot": (io.BytesIO(JPEG), "snapshot.jpg", "image/jpeg"),
    }
    resp = client.post("/vigi/alarm/nvr-token", data=data, content_type="multipart/form-data")
    [event_id] = resp.get_json()["events"]
    client.bridge.process(event_id)

    assert client.get(f"/vigi/events/{event_id}/snapshot.jpg").status_code == 401
    img = client.get(f"/vigi/events/{event_id}/snapshot.jpg", headers=KEY)
    assert img.status_code == 200 and img.data == JPEG
    detail = client.get(f"/vigi/events/{event_id}", headers=KEY).get_json()
    assert detail["status"] == "alarm" and detail["channel"] == 3
    assert detail["raw"]["channel"] == 3


def test_multipart_event_as_form_field(client):
    data = {"event": json.dumps(nvr_push(channel=5)), "snapshot": (io.BytesIO(JPEG), "s.jpg", "image/jpeg")}
    resp = client.post("/vigi/alarm/nvr-token", data=data, content_type="multipart/form-data")
    [event_id] = resp.get_json()["events"]
    assert client.bridge.store.get_event(event_id)["snapshot"]


def test_management_endpoints_need_configured_key(client):
    assert client.get("/vigi/status").status_code == 401
    assert client.get("/vigi/status?key=secret-key").status_code == 200
    client.bridge.cfg["api_key"] = ""
    assert client.get("/vigi/status", headers=KEY).status_code == 503


def test_arm_and_status(client):
    assert client.post("/vigi/arm", json={"mode": "sometimes"}, headers=KEY).status_code == 400
    status = client.post("/vigi/arm", json={"mode": "disarmed"}, headers=KEY).get_json()
    assert status["arm_mode"] == "disarmed" and status["armed_now"] is False
    status = client.post("/vigi/arm", json={"mode": "armed"}, headers=KEY).get_json()
    assert status["armed_now"] is True
    assert status["notifiers"] == ["recorder"]


def test_events_list_csv_and_reports(client):
    for ch, sub in ((1, 5), (2, 2)):
        [event_id] = post_alarm(client, nvr_push(channel=ch, sub=sub)).get_json()["events"]
        client.bridge.process(event_id)

    rows = client.get("/vigi/events", headers=KEY).get_json()
    assert [r["status"] for r in rows] == ["logged", "alarm"]
    assert "raw" not in rows[0]
    assert len(client.get("/vigi/events?status=alarm", headers=KEY).get_json()) == 1
    assert len(client.get("/vigi/events?channel=2", headers=KEY).get_json()) == 1

    csv_text = client.get("/vigi/events.csv?from=2026-10-30&to=2026-10-31", headers=KEY).get_data(as_text=True)
    assert csv_text.splitlines()[0].startswith("id,event_time,channel")
    assert len(csv_text.strip().splitlines()) == 3

    rep = client.get("/vigi/report?from=2026-10-30&to=2026-10-31", headers=KEY).get_json()
    assert rep["total_events"] == 2 and rep["alarms_sent"] == 1
    assert rep["by_event"] == {"intrusion": 1, "motion": 1}
    assert rep["alarms"][0]["camera"] == "Cam 1"

    text = client.get("/vigi/report?format=text&from=2026-10-30&to=2026-10-31", headers=KEY).get_data(as_text=True)
    assert "Alarms sent: 1" in text
    html = client.get("/vigi/report?format=html&from=2026-10-30&to=2026-10-31&key=secret-key").get_data(as_text=True)
    assert "/vigi/events/1/snapshot.jpg?key=secret-key" in html

    assert client.get("/vigi/report?period=1y", headers=KEY).status_code == 400
    sent = client.post("/vigi/report/send?from=2026-10-30&to=2026-10-31", headers=KEY).get_json()
    assert sent == {"ok": True, "sent_via": ["recorder"]}


def test_on_demand_clip(client):
    [event_id] = post_alarm(client, nvr_push()).get_json()["events"]
    assert client.post("/vigi/events/999/clip", headers=KEY).status_code == 404
    assert client.post(f"/vigi/events/{event_id}/clip", headers=KEY).status_code == 202
    assert client.bridge._clips.qsize() == 1
