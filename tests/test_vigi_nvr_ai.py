import json
import shutil
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import JPEG
from vigi_bridge import nvr as nvr_module
from vigi_bridge.ai import AnalysisError, SnapshotAnalyzer, ai_enabled
from vigi_bridge.config import load_config
from vigi_bridge.nvr import NvrClient


def cfg(**overrides):
    return load_config(path="", env={}, overrides=overrides)


def test_replay_url_uses_utc_window_and_escapes_password():
    client = NvrClient(cfg(nvr={"host": "192.168.1.50", "password": "p@ss:word"}))
    when = datetime(2026, 10, 30, 19, 14, 3, tzinfo=timezone.utc)
    url = client.replay_url(2, 2, when.replace(second=0), when.replace(minute=15, second=0))
    assert url == ("rtsp://admin:p%40ss%3Aword@192.168.1.50:554/replay/2/2/avm"
                   "?starttime=20261030t191400z&endtime=20261030t191500z")
    assert client.live_url(1, 1) == "rtsp://admin:p%40ss%3Aword@192.168.1.50:554/live/1/1/avm"


def test_ffmpeg_cmd_options():
    client = NvrClient(cfg(nvr={"host": "h", "password": "p"}))
    rtsp = client.ffmpeg_cmd("rtsp://x", 15, "/tmp/out.mp4")
    assert rtsp[rtsp.index("-rtsp_transport") + 1] == "tcp"
    assert rtsp[rtsp.index("-c:v") + 1] == "copy"
    assert "-an" in rtsp
    assert "-rtsp_transport" not in client.ffmpeg_cmd("/tmp/in.mp4", 15, "/tmp/out.mp4")
    client.transcode = True
    assert "libx264" in client.ffmpeg_cmd("rtsp://x", 15, "/tmp/out.mp4")


def test_transcode_auto_follows_whatsapp():
    assert NvrClient(cfg(nvr={"host": "h", "password": "p"})).transcode is False
    wa = {"token": "t", "phone_number_id": "1", "recipients": ["27820000000"]}
    assert NvrClient(cfg(notify={"whatsapp": wa})).transcode is True
    assert NvrClient(cfg(notify={"whatsapp": wa}, clips={"transcode": False})).transcode is False


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_ffmpeg_cuts_a_real_clip(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=4:size=320x240:rate=10", "-c:v", "libx264", str(source)], check=True)
    client = NvrClient(cfg(nvr={"host": "h", "password": "p"}))
    out = tmp_path / "clips" / "clip.mp4"
    assert client.run_ffmpeg(str(source), 2, str(out)) is True
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True)
    assert 1.5 <= float(probe.stdout) <= 2.5


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_whatsapp_transcode_produces_baseline_h264(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=3:size=1920x1080:rate=10", "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                    str(source)], check=True)
    client = NvrClient(cfg(nvr={"host": "h", "password": "p"}, clips={"transcode": True}))
    out = tmp_path / "clip.mp4"
    assert client.run_ffmpeg(str(source), 2, str(out)) is True
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,profile,width,has_b_frames", "-of", "json", str(out)],
                           capture_output=True, text=True)
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "h264"
    assert "Baseline" in stream["profile"]
    assert stream["width"] == 1280
    assert stream["has_b_frames"] == 0


def test_snapshot_auth_failure_pauses_requests(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["params"]))
        return SimpleNamespace(status_code=401, content=b"")

    monkeypatch.setattr(nvr_module.requests, "get", fake_get)
    client = NvrClient(cfg(nvr={"host": "nvr.local", "password": "wrong"}))
    assert client.snapshot(1) is None
    assert client.snapshot(1) is None
    assert calls == [("https://nvr.local/snapshot", {"channel": 1})]


def test_snapshot_success(monkeypatch):
    monkeypatch.setattr(nvr_module.requests, "get",
                        lambda url, **kw: SimpleNamespace(status_code=200, content=JPEG))
    client = NvrClient(cfg(nvr={"host": "nvr.local", "https_port": 8443, "password": "pw"}))
    assert client.snapshot(3) == JPEG


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(text, stop_reason="end_turn"):
    response = SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5",
                               content=[SimpleNamespace(type="text", text=text)])
    messages = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_analyzer_request_shape_and_parsing():
    verdict = {"people": 1, "vehicles": 0, "animals": 0, "description": "Person at gate.",
               "threat_level": "medium", "alarm": True, "reason": "person present"}
    client, messages = fake_client(json.dumps(verdict))
    analyzer = SnapshotAnalyzer(cfg(ai={"enabled": True, "site_notes": "Two dogs roam the yard."}), client=client)
    result = analyzer.analyze(JPEG, {"camera": "Gate", "event": "intrusion"})

    assert result["alarm"] is True and result["model"] == "claude-opus-5-5"
    kw = messages.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "Two dogs roam the yard." in kw["system"]
    image, text = kw["messages"][0]["content"]
    assert image["source"]["media_type"] == "image/jpeg"
    assert "camera: Gate" in text["text"]


def test_analyzer_errors():
    client, _ = fake_client("", stop_reason="refusal")
    with pytest.raises(AnalysisError, match="declined"):
        SnapshotAnalyzer(cfg(ai={"enabled": True}), client=client).analyze(JPEG, {})
    client, _ = fake_client("{not json")
    with pytest.raises(AnalysisError, match="invalid JSON"):
        SnapshotAnalyzer(cfg(ai={"enabled": True}), client=client).analyze(JPEG, {})
    with pytest.raises(AnalysisError, match="larger than 5 MB"):
        SnapshotAnalyzer(cfg(ai={"enabled": True}), client=client).analyze(b"x" * (5 * 1024 * 1024 + 1), {})


def test_ai_enabled_auto(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert ai_enabled(cfg()) is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert ai_enabled(cfg()) is True
    assert ai_enabled(cfg(ai={"enabled": False})) is False
