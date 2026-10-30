from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from vigi_bridge.events import parse_push, parse_time, tags_for

TZ = ZoneInfo("Africa/Johannesburg")
RECEIVED = datetime(2026, 10, 30, 21, 0, 0, tzinfo=timezone.utc)


def test_nvr_intrusion_message():
    payload = {"device_id": "ABC", "device_name": "NVR", "messages": [
        {"type": 1, "sub_type": 5, "localtime": "2026-10-30 21:14:03", "channel": 2, "channel_name": "Gate"}]}
    [ev] = parse_push(payload, TZ, RECEIVED)
    assert ev.source == "nvr"
    assert ev.label == "intrusion"
    assert ev.channel == 2 and ev.channel_name == "Gate"
    assert ev.camera_key == "ch2"
    assert ev.event_time == datetime(2026, 10, 30, 19, 14, 3, tzinfo=timezone.utc)


def test_line_crossing_target_and_tags():
    payload = {"messages": [{"type": 1, "sub_type": 4, "target": 101, "port": 3}]}
    [ev] = parse_push(payload, TZ, RECEIVED)
    assert ev.label == "line_crossing_person"
    assert ev.channel == 3
    assert ev.channel_name == "Channel 3"
    assert ev.event_time == RECEIVED
    assert tags_for(ev.labels) == {"line_crossing_person", "line_crossing", "person"}


def test_unknown_codes_and_custom_labels():
    payload = {"messages": [{"type": 1, "sub_type": 9, "channel": 1}]}
    [ev] = parse_push(payload, TZ, RECEIVED)
    assert ev.label == "type1_sub9"
    [ev] = parse_push(payload, TZ, RECEIVED, {"1:9": "region_entering"})
    assert ev.label == "region_entering"


def test_enhanced_field_names():
    payload = {"messages": [{"Event Type": "1", "Event Sub Type": "2", "EVENT TIME": "2026-10-30 21:00:00",
                             "Camera": "Camera 4", "Camera Name": "Back yard"}]}
    [ev] = parse_push(payload, TZ, RECEIVED)
    assert ev.label == "motion"
    assert ev.channel == 4 and ev.channel_name == "Back yard"


def test_camera_event_list():
    payload = {"ip": "192.168.1.20", "mac": "AA-BB", "event_list": [
        {"dateTime": "2026-10-30T21:00:00", "event_type": ["motion", "People Detection"]}]}
    [ev] = parse_push(payload, TZ, RECEIVED)
    assert ev.source == "camera"
    assert ev.labels == ["person", "motion"]
    assert ev.channel is None
    assert ev.camera_key == "dev:AA-BB"
    assert ev.channel_name == "192.168.1.20"


def test_empty_test_push_and_bad_payload():
    assert parse_push({"device_id": "x", "messages": []}, TZ, RECEIVED) == []
    with pytest.raises(ValueError):
        parse_push("nope", TZ, RECEIVED)


@pytest.mark.parametrize("value, expected", [
    (1793393100, datetime(2026, 10, 30, 20, 45, tzinfo=timezone.utc)),
    ("1793393100000", datetime(2026, 10, 30, 20, 45, tzinfo=timezone.utc)),
    ("20261030t191500z", datetime(2026, 10, 30, 19, 15, tzinfo=timezone.utc)),
    ("2026-10-30T21:15:00+02:00", datetime(2026, 10, 30, 19, 15, tzinfo=timezone.utc)),
    ("2026/10/30 21:15:00", datetime(2026, 10, 30, 19, 15, tzinfo=timezone.utc)),
    ("not a time", RECEIVED),
    (None, RECEIVED),
])
def test_parse_time(value, expected):
    assert parse_time(value, TZ, RECEIVED) == expected
