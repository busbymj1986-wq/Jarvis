"""Pull snapshots and clips from a VIGI NVR.

Snapshot: https://<nvr>/snapshot?channel=N (HTTPS only, Digest auth).
Live:     rtsp://<nvr>:554/live/<channel>/<stream>/avm
Replay:   rtsp://<nvr>:554/replay/<channel>/<stream>/avm?starttime=YYYYMMDDtHHMMSSz&endtime=...
          (times in UTC; stream 1 = main, 2 = sub)

The NVR locks out an IP after 10 failed logins, so a rejected login pauses
all NVR requests for 30 minutes instead of retrying.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import urllib3
from requests.auth import HTTPDigestAuth

from .config import whatsapp_configured

log = logging.getLogger("vigi.nvr")

AUTH_BACKOFF_SECONDS = 30 * 60


class NvrClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg["nvr"]
        self.clips = cfg["clips"]
        transcode = self.clips.get("transcode", "auto")
        self.transcode = whatsapp_configured(cfg) if transcode == "auto" else bool(transcode)
        self._blocked_until = 0.0
        if not self.cfg.get("verify_tls", False):
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def configured(self) -> bool:
        return bool(self.cfg.get("host") and self.cfg.get("password"))

    @property
    def ffmpeg(self) -> str | None:
        return shutil.which("ffmpeg")

    def _usable(self) -> bool:
        if not self.configured:
            return False
        if time.time() < self._blocked_until:
            log.warning("NVR requests paused after a failed login; fix the NVR credentials")
            return False
        return True

    def _auth_failed(self, what: str):
        self._blocked_until = time.time() + AUTH_BACKOFF_SECONDS
        log.error("NVR rejected the login for %s; pausing NVR requests for 30 minutes", what)

    # --- snapshots ----------------------------------------------------------

    def snapshot(self, channel: int | None) -> bytes | None:
        if channel is None or not self._usable():
            return None
        port = self.cfg.get("https_port", 443)
        host = self.cfg["host"] if port == 443 else f"{self.cfg['host']}:{port}"
        try:
            resp = requests.get(
                f"https://{host}/snapshot",
                params={"channel": channel},
                auth=HTTPDigestAuth(self.cfg.get("username", "admin"), self.cfg["password"]),
                verify=self.cfg.get("verify_tls", False),
                timeout=10,
            )
        except requests.RequestException as exc:
            log.warning("snapshot request for channel %s failed: %s", channel, exc)
            return None
        if resp.status_code in (401, 403):
            self._auth_failed("snapshot")
            return None
        if resp.status_code != 200 or not resp.content.startswith(b"\xff\xd8"):
            log.warning("snapshot for channel %s returned HTTP %s", channel, resp.status_code)
            return None
        return resp.content

    # --- clips --------------------------------------------------------------

    def _rtsp_base(self) -> str:
        user = quote(self.cfg.get("username", "admin"), safe="")
        password = quote(self.cfg["password"], safe="")
        return f"rtsp://{user}:{password}@{self.cfg['host']}:{self.cfg.get('rtsp_port', 554)}"

    def live_url(self, channel: int, stream: int) -> str:
        return f"{self._rtsp_base()}/live/{channel}/{stream}/avm"

    def replay_url(self, channel: int, stream: int, start: datetime, end: datetime) -> str:
        fmt = "%Y%m%dt%H%M%Sz"
        start_s = start.astimezone(timezone.utc).strftime(fmt)
        end_s = end.astimezone(timezone.utc).strftime(fmt)
        return f"{self._rtsp_base()}/replay/{channel}/{stream}/avm?starttime={start_s}&endtime={end_s}"

    def ffmpeg_cmd(self, source: str, seconds: float, out_path: str) -> list[str]:
        cmd = [self.ffmpeg or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if source.startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp"]
        cmd += ["-i", source, "-t", f"{seconds:.0f}"]
        if self.transcode:
            # Baseline profile has no B-frames, which Android WhatsApp needs;
            # capping the width keeps clips well under WhatsApp's 16 MB limit.
            cmd += ["-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "28", "-profile:v", "baseline", "-pix_fmt", "yuv420p"]
        else:
            cmd += ["-c:v", "copy"]
        # NVR audio is usually G.711, which MP4 cannot carry; alerts don't need it.
        cmd += ["-an", "-movflags", "+faststart", "-f", "mp4", out_path]
        return cmd

    def run_ffmpeg(self, source: str, seconds: float, out_path: str) -> bool:
        if not self.ffmpeg:
            log.warning("ffmpeg is not installed; cannot record clips")
            return False
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        try:
            proc = subprocess.run(
                self.ffmpeg_cmd(source, seconds, out_path),
                capture_output=True, text=True, timeout=seconds + 60,
            )
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg timed out recording %s", out_path)
            return False
        if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            err = proc.stderr.strip()
            password = self.cfg.get("password") or ""
            for secret in {password, quote(password, safe="")} - {""}:
                err = err.replace(secret, "***")
            if "401" in err or "Unauthorized" in err:
                self._auth_failed("RTSP")
            log.warning("ffmpeg failed for %s: %s", out_path, err[-500:])
            return False
        return True

    def record_clip(self, channel: int | None, event_time: datetime, out_path: str) -> bool:
        if channel is None or not self._usable():
            return False
        pre, post = self.clips.get("pre_seconds", 5), self.clips.get("post_seconds", 10)
        stream = self.clips.get("stream", 2)
        if self.clips.get("source", "replay") == "live":
            return self.run_ffmpeg(self.live_url(channel, stream), pre + post, out_path)
        start = event_time.timestamp() - pre
        end = event_time.timestamp() + post
        url = self.replay_url(
            channel, stream,
            datetime.fromtimestamp(start, tz=timezone.utc),
            datetime.fromtimestamp(end, tz=timezone.utc),
        )
        return self.run_ffmpeg(url, pre + post, out_path)

    def record_live(self, channel: int, seconds: float, out_path: str) -> bool:
        if not self._usable():
            return False
        return self.run_ffmpeg(self.live_url(channel, self.clips.get("stream", 2)), seconds, out_path)
