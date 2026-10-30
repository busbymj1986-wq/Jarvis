import os
import sys
from datetime import datetime, timezone

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vigi_bridge import init_vigi, load_config  # noqa: E402
from vigi_bridge.ai import AnalysisError  # noqa: E402
from vigi_bridge.service import VigiBridge  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"
TZ = "Africa/Johannesburg"  # UTC+2, no DST


class Clock:
    def __init__(self, local: str):
        self.set(local)

    def set(self, local: str):
        from zoneinfo import ZoneInfo
        self.value = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc)

    def __call__(self):
        return self.value


class FakeNvr:
    configured = True
    ffmpeg = "/usr/bin/ffmpeg"

    def __init__(self, snapshot=JPEG, clip_ok=True):
        self.snapshot_bytes = snapshot
        self.clip_ok = clip_ok
        self.snapshot_calls = []
        self.clip_calls = []

    def snapshot(self, channel):
        self.snapshot_calls.append(channel)
        return self.snapshot_bytes

    def record_live(self, channel, seconds, out_path):
        self.clip_calls.append((channel, seconds))
        with open(out_path, "wb") as f:
            f.write(b"live-mp4-bytes")
        return self.clip_ok

    def record_clip(self, channel, when, out_path):
        self.clip_calls.append((channel, when))
        if not self.clip_ok:
            return False
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(b"mp4-bytes")
        return True


class FakeAnalyzer:
    def __init__(self, verdict=None, error=None, enabled=True):
        self.verdict = verdict
        self.error = error
        self.enabled = enabled
        self.calls = []

    def analyze(self, image, context):
        self.calls.append(context)
        if self.error:
            raise AnalysisError(self.error)
        return dict(self.verdict)


class RecordingNotifier:
    name = "recorder"

    def __init__(self):
        self.alarms, self.clips, self.reports = [], [], []

    def send_alarm(self, alert, snapshot):
        self.alarms.append((alert, snapshot))

    def send_clip(self, alert, clip_path):
        self.clips.append((alert, clip_path))

    def send_report(self, subject, text, html):
        self.reports.append((subject, text, html))


PERSON = {"people": 1, "vehicles": 0, "animals": 0, "description": "One person climbing the gate.",
          "threat_level": "high", "alarm": True, "reason": "person climbing gate"}
NOBODY = {"people": 0, "vehicles": 0, "animals": 0, "description": "Empty driveway, moving shadows.",
          "threat_level": "none", "alarm": False, "reason": "no person visible"}


def nvr_push(channel=1, etype=1, sub=5, local="2026-10-30 23:00:00", **extra):
    msg = {"type": etype, "sub_type": sub, "localtime": local, "channel": channel,
           "channel_name": f"Cam {channel}", **extra}
    return {"device_id": "NVR-1", "device_name": "Home NVR", "messages": [msg]}


@pytest.fixture
def make_cfg(tmp_path):
    def _make(**overrides):
        base = {"data_dir": str(tmp_path / "data"), "timezone": TZ, "api_key": "secret-key",
                "ingest_token": "nvr-token", "report": {"daily_at": "07:00"}}
        from vigi_bridge.config import deep_merge
        return load_config(path="", overrides=deep_merge(base, overrides), env={})
    return _make


@pytest.fixture
def make_bridge(make_cfg):
    def _make(analyzer=None, nvr=None, clock="2026-10-30 23:00:00", **cfg_overrides):
        notifier = RecordingNotifier()
        bridge = VigiBridge(
            make_cfg(**cfg_overrides),
            nvr=nvr or FakeNvr(),
            analyzer=analyzer or FakeAnalyzer(enabled=False),
            notifiers=[notifier],
            clock=Clock(clock),
        )
        bridge.notifier = notifier
        return bridge
    return _make


@pytest.fixture
def client(make_cfg):
    app = Flask(__name__)
    notifier = RecordingNotifier()
    bridge = init_vigi(app, make_cfg(), start=False, nvr=FakeNvr(), analyzer=FakeAnalyzer(enabled=False),
                       notifiers=[notifier], clock=Clock("2026-10-30 23:00:00"))
    bridge.notifier = notifier
    test_client = app.test_client()
    test_client.bridge = bridge
    return test_client
