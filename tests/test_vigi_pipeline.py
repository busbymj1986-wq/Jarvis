import os

from conftest import JPEG, NOBODY, PERSON, FakeAnalyzer, FakeNvr, nvr_push


def run(bridge, payload, snapshot=JPEG):
    [event_id] = bridge.ingest(payload, snapshot)
    return bridge.process(event_id)


def test_intrusion_inside_schedule_raises_alarm(make_bridge):
    bridge = make_bridge()
    event = run(bridge, nvr_push(channel=1, sub=5))
    assert event["status"] == "alarm"
    assert "inside armed schedule" in event["reason"]
    [(alert, snapshot)] = bridge.notifier.alarms
    assert snapshot == JPEG
    assert alert["subject"] == "[Jarvis] ALARM: Intrusion at Cam 1 (23:00)"
    assert event["notified_via"] == ["recorder"]


def test_outside_schedule_and_disarmed_are_logged(make_bridge):
    bridge = make_bridge(clock="2026-10-30 12:00:00")
    event = run(bridge, nvr_push(local="2026-10-30 12:00:00"))
    assert event["status"] == "logged"
    assert event["reason"] == "outside armed schedule"

    bridge.set_arm_mode("disarmed")
    event = run(bridge, nvr_push(local="2026-10-30 23:30:00"))
    assert (event["status"], event["reason"]) == ("logged", "system disarmed")
    assert bridge.notifier.alarms == []


def test_overnight_schedule_spans_midnight(make_bridge):
    bridge = make_bridge(rules={"schedule": [{"start": "22:00", "end": "05:00", "days": ["fri"]}]})
    assert run(bridge, nvr_push(local="2026-10-30 22:30:00"))["status"] == "alarm"   # Friday night
    assert run(bridge, nvr_push(channel=2, local="2026-10-31 04:59:00"))["status"] == "alarm"  # Sat early
    assert run(bridge, nvr_push(channel=3, local="2026-10-31 22:30:00"))["status"] == "logged"  # Sat night


def test_motion_is_log_only_until_it_escalates(make_bridge):
    bridge = make_bridge()
    first = run(bridge, nvr_push(channel=1, sub=2, local="2026-10-30 23:00:00"))
    second = run(bridge, nvr_push(channel=1, sub=2, local="2026-10-30 23:00:20"))
    assert first["status"] == second["status"] == "logged"
    assert "motion is log-only" in first["reason"]
    third = run(bridge, nvr_push(channel=2, sub=2, local="2026-10-30 23:00:40"))
    assert third["status"] == "alarm"
    assert "3 events on 2 cameras within 60s" in third["reason"]


def test_cooldown_groups_repeat_alarms_per_camera(make_bridge):
    bridge = make_bridge()
    assert run(bridge, nvr_push(channel=1, local="2026-10-30 23:00:00"))["status"] == "alarm"
    repeat = run(bridge, nvr_push(channel=1, local="2026-10-30 23:01:00"))
    assert repeat["status"] == "grouped"
    assert "60s earlier" in repeat["reason"]
    assert run(bridge, nvr_push(channel=2, local="2026-10-30 23:01:00"))["status"] == "alarm"
    assert run(bridge, nvr_push(channel=1, local="2026-10-30 23:03:00"))["status"] == "alarm"
    assert len(bridge.notifier.alarms) == 3


def test_ai_dismisses_false_trigger(make_bridge):
    analyzer = FakeAnalyzer(verdict=NOBODY)
    bridge = make_bridge(analyzer=analyzer)
    event = run(bridge, nvr_push())
    assert event["status"] == "dismissed"
    assert event["reason"] == "AI dismissed: no person visible"
    assert event["ai"]["threat_level"] == "none"
    assert analyzer.calls[0]["camera"] == "Cam 1"
    assert bridge.notifier.alarms == []


def test_ai_confirms_and_describes(make_bridge):
    bridge = make_bridge(analyzer=FakeAnalyzer(verdict=PERSON))
    event = run(bridge, nvr_push())
    assert event["status"] == "alarm"
    assert "AI confirmed: person climbing gate" in event["reason"]
    [(alert, _)] = bridge.notifier.alarms
    assert "One person climbing the gate. (threat: high)" in alert["text"]


def test_ai_advisory_mode_always_alarms(make_bridge):
    bridge = make_bridge(analyzer=FakeAnalyzer(verdict=NOBODY), ai={"mode": "advisory"})
    assert run(bridge, nvr_push())["status"] == "alarm"


def test_ai_failure_fails_safe_by_default(make_bridge):
    bridge = make_bridge(analyzer=FakeAnalyzer(error="could not reach the Claude API"))
    event = run(bridge, nvr_push())
    assert event["status"] == "alarm"
    assert "AI check failed: could not reach the Claude API" in event["reason"]

    bridge = make_bridge(analyzer=FakeAnalyzer(error="boom"), ai={"on_error": "dismiss"},
                         data_dir=bridge.cfg["data_dir"] + "2")
    assert run(bridge, nvr_push())["status"] == "dismissed"


def test_sensor_events_skip_ai(make_bridge):
    analyzer = FakeAnalyzer(verdict=NOBODY)
    bridge = make_bridge(analyzer=analyzer)
    event = run(bridge, nvr_push(etype=2, sub=13))
    assert event["label"] == "alarm_input"
    assert event["status"] == "alarm"
    assert analyzer.calls == []


def test_disabled_and_always_armed_channels(make_bridge):
    bridge = make_bridge(clock="2026-10-30 12:00:00",
                         rules={"channels": {"1": {"enabled": False}, "2": {"always_armed": True}}})
    assert run(bridge, nvr_push(channel=1, local="2026-10-30 12:00:00"))["status"] == "ignored"
    assert run(bridge, nvr_push(channel=2, local="2026-10-30 12:00:00"))["status"] == "alarm"


def test_channel_specific_alarm_events(make_bridge):
    bridge = make_bridge(rules={"channels": {"1": {"alarm_events": ["line_crossing_vehicle"]}}})
    person = nvr_push(channel=1, sub=4, target=101)
    assert run(bridge, person)["status"] == "logged"
    vehicle = nvr_push(channel=1, sub=4, target=102, local="2026-10-30 23:05:00")
    assert run(bridge, vehicle)["status"] == "alarm"


def test_missing_snapshot_is_fetched_from_nvr(make_bridge):
    nvr = FakeNvr()
    bridge = make_bridge(nvr=nvr, analyzer=FakeAnalyzer(verdict=PERSON))
    event = run(bridge, nvr_push(channel=4), snapshot=None)
    assert nvr.snapshot_calls == [4]
    assert event["snapshot"].endswith(f"event_{event['id']}.jpg")
    assert bridge.notifier.alarms[0][1] == JPEG


def test_clip_is_recorded_and_sent_for_alarms(make_bridge):
    nvr = FakeNvr()
    bridge = make_bridge(nvr=nvr)
    event = run(bridge, nvr_push(channel=2))
    assert bridge._clips.qsize() == 1
    rel = bridge.capture_clip(event["id"])
    assert os.path.exists(bridge.store.abspath(rel))
    assert bridge.store.get_event(event["id"])["clip"] == rel
    [(alert, path)] = bridge.notifier.clips
    assert path == bridge.store.abspath(rel)
    assert alert["payload"]["has_clip"] is True


def test_no_clip_for_logged_events_by_default(make_bridge):
    bridge = make_bridge(clock="2026-10-30 12:00:00")
    run(bridge, nvr_push(local="2026-10-30 12:00:00"))
    assert bridge._clips.qsize() == 0


def test_daily_report_sent_once(make_bridge):
    bridge = make_bridge(clock="2026-10-30 23:00:00")
    run(bridge, nvr_push())
    bridge.now.set("2026-10-31 06:59:00")
    assert bridge.send_daily_report_if_due() is False
    bridge.now.set("2026-10-31 07:00:30")
    assert bridge.send_daily_report_if_due() is True
    assert bridge.send_daily_report_if_due() is False
    [(subject, text, html)] = bridge.notifier.reports
    assert subject == "Jarvis security report 30 Oct 07:00 - 31 Oct 07:00"
    assert "Alarms sent: 1" in text
    assert "<h1>" in html


def test_retention_purge_removes_rows_and_files(make_bridge):
    bridge = make_bridge(retention_days=1)
    event = run(bridge, nvr_push())
    path = bridge.store.abspath(event["snapshot"])
    assert os.path.exists(path)
    bridge.now.set("2026-11-01 00:00:00")
    assert bridge.purge_old_events() == 1
    assert not os.path.exists(path)
    assert bridge.store.get_event(event["id"]) is None


def test_pending_events_are_requeued_on_start(make_bridge):
    bridge = make_bridge()
    bridge.ingest(nvr_push())
    bridge._events.get_nowait()
    bridge.start()
    try:
        import time
        for _ in range(50):
            if bridge.notifier.alarms:
                break
            time.sleep(0.05)
        assert len(bridge.notifier.alarms) == 1
    finally:
        bridge.stop()


def test_warns_once_when_nvr_clock_or_timezone_is_off(make_bridge, caplog):
    bridge = make_bridge(clock="2026-10-30 23:00:00")
    with caplog.at_level("WARNING", logger="vigi"):
        bridge.ingest(nvr_push(local="2026-10-30 21:00:00"))
        bridge.ingest(nvr_push(local="2026-10-30 21:00:05"))
    warnings = [r for r in caplog.records if "away from this server's clock" in r.getMessage()]
    assert len(warnings) == 1
    assert "120 minutes" in warnings[0].getMessage()


def test_channel_name_from_rules_overrides_payload(make_bridge):
    bridge = make_bridge(rules={"channels": {"2": {"name": "Driveway"}}})
    msg = {"type": 1, "sub_type": 5, "localtime": "2026-10-30 23:00:00", "channel": 2}
    event = run(bridge, {"device_id": "NVR-1", "messages": [msg]})
    assert event["channel_name"] == "Driveway"
    assert bridge.notifier.alarms[0][0]["subject"] == "[Jarvis] ALARM: Intrusion at Driveway (23:00)"
