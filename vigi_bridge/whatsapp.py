"""WhatsApp through Meta's Cloud API: alarms, clips and reports out; commands in.

WhatsApp only delivers free-form messages to someone who messaged the business
number in the last 24 hours. The bridge records when each recipient last wrote
in, sends free-form inside that window, and switches to the approved templates
outside it.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import requests

from .notifiers import Notifier
from .reports import PERIODS, render_text, report_title
from .store import from_iso, iso

log = logging.getLogger("vigi.whatsapp")

GRAPH = "https://graph.facebook.com"
WINDOW = timedelta(hours=23, minutes=50)  # a margin inside Meta's 24 hours
OUTSIDE_WINDOW = 131047
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
IMAGE_LIMIT = 5 * 1024 * 1024
VIDEO_LIMIT = 16 * 1024 * 1024
MAX_LIVE_CLIP = 30

HELP = """*Jarvis security* - send one of:
*status* - armed or not, today's events
*arm* / *disarm* / *auto* - arm, disarm, or follow the schedule
*snap 2* - live photo from camera 2
*clip 2* - 10 second live clip from camera 2 (*clip 2 20* for 20 s)
*alarms* - the last 5 alarms
*event 12* - photo and clip of event 12
*report* - last 24 hours (*report 7d* for a week)"""


def digits(number) -> str:
    return re.sub(r"\D", "", str(number))


def one_line(text, limit: int = 900) -> str:
    """Template parameters may not contain newlines, tabs or runs of spaces."""
    return " ".join(str(text).split())[:limit]


class WhatsAppError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{message} (code {code})")
        self.code = code


class WhatsAppClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = f"{GRAPH}/{cfg.get('api_version', 'v23.0')}/{cfg['phone_number_id']}"
        self.headers = {"Authorization": f"Bearer {cfg['token']}"}

    def _check(self, resp) -> dict:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 300 or "error" in body:
            err = body.get("error") or {}
            raise WhatsAppError(err.get("code", resp.status_code), err.get("message", resp.text[:200]))
        return body

    def upload(self, data: bytes, mime: str, filename: str) -> str:
        resp = requests.post(f"{self.base}/media", headers=self.headers,
                             data={"messaging_product": "whatsapp", "type": mime},
                             files={"file": (filename, data, mime)}, timeout=60)
        return self._check(resp)["id"]

    def send(self, to, message: dict) -> dict:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": digits(to), **message}
        return self._check(requests.post(f"{self.base}/messages", headers=self.headers, json=body, timeout=30))

    def text(self, to, body: str):
        return self.send(to, {"type": "text", "text": {"body": body[:TEXT_LIMIT], "preview_url": False}})

    def image(self, to, media_id: str, caption: str = ""):
        return self.send(to, {"type": "image", "image": {"id": media_id, "caption": caption[:CAPTION_LIMIT]}})

    def video(self, to, media_id: str, caption: str = ""):
        return self.send(to, {"type": "video", "video": {"id": media_id, "caption": caption[:CAPTION_LIMIT]}})

    def template(self, to, name: str, params: list, header: tuple[str, str] | None = None):
        components = []
        if header:
            kind, media_id = header
            components.append({"type": "header", "parameters": [{"type": kind, kind: {"id": media_id}}]})
        if params:
            components.append({"type": "body",
                               "parameters": [{"type": "text", "text": one_line(p) or "-"} for p in params]})
        return self.send(to, {"type": "template", "template": {
            "name": name, "language": {"code": self.cfg.get("language", "en")}, "components": components}})


class Windows:
    """Tracks each number's 24-hour customer service window."""

    def __init__(self, store, clock):
        self.store = store
        self.clock = clock

    def touch(self, number):
        self.store.set_setting(f"wa_inbound:{digits(number)}", iso(self.clock()))

    def is_open(self, number) -> bool:
        last = self.store.get_setting(f"wa_inbound:{digits(number)}")
        return bool(last) and self.clock() - from_iso(last) < WINDOW


class WhatsAppNotifier(Notifier):
    name = "whatsapp"

    def __init__(self, cfg: dict, client: WhatsAppClient, windows: Windows):
        self.cfg = cfg
        self.client = client
        self.windows = windows
        self.recipients = [digits(n) for n in cfg["recipients"]]

    def _each(self, what: str, targets: list[str], send):
        errors = []
        for to in targets:
            try:
                send(to)
            except (WhatsAppError, requests.RequestException) as exc:
                hint = ""
                if getattr(exc, "code", None) == OUTSIDE_WINDOW:
                    hint = " - they haven't messaged the bot in 24 hours; set up the WhatsApp templates"
                errors.append(f"{to}: {exc}{hint}")
        if errors and len(errors) == len(targets):
            raise RuntimeError("; ".join(errors))
        if errors:
            log.warning("WhatsApp %s failed for %s", what, "; ".join(errors))

    def _upload(self, data: bytes, mime: str, filename: str) -> str | None:
        try:
            return self.client.upload(data, mime, filename)
        except (WhatsAppError, requests.RequestException) as exc:
            log.warning("WhatsApp media upload failed: %s", exc)
            return None

    def _template_targets(self, template: str) -> list[str]:
        """Recipients reachable now: inside their window, or any if a template exists."""
        return [to for to in self.recipients if template or self.windows.is_open(to)]

    def send_alarm(self, alert, snapshot):
        media_id = None
        if snapshot and len(snapshot) <= IMAGE_LIMIT:
            media_id = self._upload(snapshot, "image/jpeg", f"event_{alert['id']}.jpg")
        caption = alert["text"]
        if self.cfg.get("commands", True) and alert.get("channel") is not None:
            caption += f"\n\nReply *snap {alert['channel']}* for a live photo or *help* for commands."
        template = self.cfg.get("alarm_template")

        def send(to):
            if template and not self.windows.is_open(to):
                header = ("image", media_id) if media_id else None
                self.client.template(to, template, alert["template_params"], header)
            elif media_id:
                self.client.image(to, media_id, caption)
            else:
                self.client.text(to, caption)

        self._each("alarm", self.recipients, send)

    def send_clip(self, alert, clip_path):
        if os.path.getsize(clip_path) > VIDEO_LIMIT:
            log.warning("clip %s is over WhatsApp's 16 MB limit; not sent", clip_path)
            return
        template = self.cfg.get("clip_template")
        targets = self._template_targets(template)
        if not targets:
            log.info("clip for event %s not sent on WhatsApp: no recipient messaged in 24 h and no clip_template",
                     alert["id"])
            return
        with open(clip_path, "rb") as f:
            media_id = self._upload(f.read(), "video/mp4", os.path.basename(clip_path))
        if media_id is None:
            raise RuntimeError("clip upload failed")

        def send(to):
            if self.windows.is_open(to):
                self.client.video(to, media_id, alert["clip_text"])
            else:
                self.client.template(to, template, alert["template_params"][:2], ("video", media_id))

        self._each("clip", targets, send)

    def send_report(self, subject, text, html):
        template = self.cfg.get("report_template")
        targets = self._template_targets(template)
        if not targets:
            log.info("report not sent on WhatsApp: no recipient messaged in 24 h and no report_template")
            return

        def send(to):
            if self.windows.is_open(to):
                self.client.text(to, f"*{subject}*\n\n{text}")
            else:
                self.client.template(to, template, [subject, text])

        self._each("report", targets, send)


class WhatsAppBot:
    """Answers commands sent to the business number by listed recipients."""

    def __init__(self, bridge, client: WhatsAppClient, windows: Windows):
        self.bridge = bridge
        self.cfg = bridge.cfg["notify"]["whatsapp"]
        self.client = client
        self.windows = windows
        self.allowed = {digits(n) for n in self.cfg["recipients"]}
        self._seen: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vigi-whatsapp")

    def verify_signature(self, raw: bytes, header: str | None) -> bool:
        secret = self.cfg.get("app_secret")
        if not secret or not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header[len("sha256="):])

    def dispatch(self, payload: dict):
        """Handle a webhook in the background so Meta gets its 200 straight away."""
        self._pool.submit(self._handle_safely, payload)

    def _handle_safely(self, payload):
        try:
            self.handle(payload)
        except Exception:
            log.exception("failed to handle WhatsApp webhook")

    def handle(self, payload: dict):
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for status in value.get("statuses", []):
                    self._status(status)
                for message in value.get("messages", []):
                    self._message(message)

    def _status(self, status: dict):
        if status.get("status") != "failed":
            return
        for err in status.get("errors") or [{}]:
            hint = ""
            if err.get("code") == OUTSIDE_WINDOW:
                hint = " - outside the 24-hour window; set up the WhatsApp templates"
            log.warning("WhatsApp message to %s failed: %s %s%s", status.get("recipient_id"), err.get("code"),
                        err.get("title") or err.get("message", ""), hint)

    def _message(self, message: dict):
        sender = digits(message.get("from", ""))
        with self._lock:
            if message.get("id") in self._seen:
                return  # Meta redelivers webhooks; answer each message once
            self._seen[message.get("id")] = True
            while len(self._seen) > 500:
                self._seen.popitem(last=False)
        if sender not in self.allowed:
            log.warning("ignored WhatsApp message from unlisted number %s", sender)
            return
        self.windows.touch(sender)
        if not self.cfg.get("commands", True):
            return
        text = ((message.get("text") or {}).get("body")
                or (message.get("button") or {}).get("text")
                or ((message.get("interactive") or {}).get("button_reply") or {}).get("title")
                or "")
        try:
            self.run(sender, text)
        except (WhatsAppError, requests.RequestException) as exc:
            log.warning("WhatsApp reply to %s failed: %s", sender, exc)

    # --- commands -----------------------------------------------------------

    def run(self, sender: str, text: str):
        words = text.lower().replace("*", "").split()
        if words and words[0] == "jarvis":
            words = words[1:]
        cmd, args = (words[0], words[1:]) if words else ("help", [])

        if cmd in ("arm", "disarm", "auto", "schedule"):
            mode = {"arm": "armed", "disarm": "disarmed"}.get(cmd, "schedule")
            self.bridge.set_arm_mode(mode)
            log.info("%s set arm mode to %s via WhatsApp", sender, mode)
            self.client.text(sender, self.status_text())
        elif cmd == "status":
            self.client.text(sender, self.status_text())
        elif cmd in ("snap", "snapshot", "photo", "pic"):
            self.send_snapshot(sender, args)
        elif cmd in ("clip", "video"):
            self.send_live_clip(sender, args)
        elif cmd in ("alarms", "last"):
            self.client.text(sender, self.alarms_text())
        elif cmd in ("event", "show"):
            self.send_event(sender, args)
        elif cmd == "report":
            self.client.text(sender, self.report_text(args[0] if args else "24h"))
        else:
            self.client.text(sender, HELP)

    def status_text(self) -> str:
        s = self.bridge.status()
        t = s["today"]
        state = "🔴 *ARMED*" if s["armed_now"] else "🟢 *Disarmed*"
        mode = {"armed": "armed", "disarmed": "disarmed", "schedule": "following the schedule"}[s["arm_mode"]]
        return (f"{state} - {s['armed_reason']} (mode: {mode})\n"
                f"Today: {t['total_events']} events, {t['alarms_sent']} alarms sent, "
                f"{t['dismissed_by_ai']} dismissed by AI")

    def alarms_text(self) -> str:
        rows = self.bridge.store.list_events(status="alarm", limit=5)
        if not rows:
            return "No alarms recorded yet."
        lines = ["*Last alarms*"]
        for e in rows:
            local = from_iso(e["event_time"]).astimezone(self.bridge.tz)
            desc = (e.get("ai") or {}).get("description") or e["label"].replace("_", " ")
            lines.append(f"• {local:%a %d %b %H:%M} {e['channel_name']}: {desc} (event {e['id']})")
        lines.append("Send *event <number>* for its photo and clip.")
        return "\n".join(lines)

    def report_text(self, period: str) -> str:
        if period not in PERIODS:
            return "Use *report 24h*, *report 7d* or *report 30d*."
        end = self.bridge.now()
        rep = self.bridge.report(end - PERIODS[period], end)
        return f"*{report_title(rep, self.bridge.tz)}*\n\n{render_text(rep)}"

    def _channel(self, sender: str, args: list[str]) -> int | None:
        if not args or not args[0].isdigit():
            self.client.text(sender, "Which camera? For example *snap 2* or *clip 2*.")
            return None
        if not self.bridge.nvr.configured:
            self.client.text(sender, "The NVR isn't set up on the bridge yet (VIGI_NVR_HOST and VIGI_NVR_PASSWORD).")
            return None
        return int(args[0])

    def _camera_name(self, channel: int) -> str:
        return self.bridge.rules.channel_rule({"channel": channel}).get("name") or f"Camera {channel}"

    def send_snapshot(self, sender: str, args: list[str]):
        channel = self._channel(sender, args)
        if channel is None:
            return
        data = self.bridge.nvr.snapshot(channel)
        if not data:
            self.client.text(sender, f"Couldn't get a photo from {self._camera_name(channel)}.")
            return
        local = self.bridge.now().astimezone(self.bridge.tz)
        media_id = self.client.upload(data, "image/jpeg", f"live_{channel}.jpg")
        self.client.image(sender, media_id, f"📷 {self._camera_name(channel)} - live at {local:%H:%M:%S}")

    def send_live_clip(self, sender: str, args: list[str]):
        channel = self._channel(sender, args)
        if channel is None:
            return
        seconds = min(int(args[1]), MAX_LIVE_CLIP) if len(args) > 1 and args[1].isdigit() else 10
        name = self._camera_name(channel)
        self.client.text(sender, f"Recording {seconds} s from {name}...")
        tmp_dir = os.path.join(self.bridge.store.data_dir, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".mp4", dir=tmp_dir)
        os.close(fd)
        try:
            if not self.bridge.nvr.record_live(channel, seconds, path):
                self.client.text(sender, f"Couldn't record from {name}.")
                return
            if os.path.getsize(path) > VIDEO_LIMIT:
                self.client.text(sender, "That clip is too big for WhatsApp; try a shorter one.")
                return
            with open(path, "rb") as f:
                media_id = self.client.upload(f.read(), "video/mp4", f"live_{channel}.mp4")
            self.client.video(sender, media_id, f"🎥 {name} - live, {seconds} s")
        finally:
            os.remove(path)

    def send_event(self, sender: str, args: list[str]):
        event = self.bridge.store.get_event(int(args[0])) if args and args[0].isdigit() else None
        if event is None:
            self.client.text(sender, "Send *event <number>*, for example *event 12*. *alarms* lists recent ones.")
            return
        local = from_iso(event["event_time"]).astimezone(self.bridge.tz)
        ai = event.get("ai") or {}
        caption = (f"Event {event['id']}: {event['label'].replace('_', ' ')} at {event['channel_name']}, "
                   f"{local:%a %d %b %H:%M:%S}\nStatus: {event['status']} - {event.get('reason') or ''}")
        if ai.get("description"):
            caption += f"\n{ai['description']}"
        snapshot = self.bridge.store.read_media(event.get("snapshot"))
        if snapshot:
            self.client.image(sender, self.client.upload(snapshot, "image/jpeg", f"event_{event['id']}.jpg"), caption)
        else:
            self.client.text(sender, caption)
        clip = self.bridge.store.abspath(event.get("clip"))
        if clip and os.path.exists(clip) and os.path.getsize(clip) <= VIDEO_LIMIT:
            with open(clip, "rb") as f:
                media_id = self.client.upload(f.read(), "video/mp4", f"event_{event['id']}.mp4")
            self.client.video(sender, media_id, f"🎥 Event {event['id']} clip")
        elif event["status"] != "pending" and self.bridge.nvr.configured and not clip:
            self.bridge.schedule_clip(event["id"], delay=0)
            self.client.text(sender, f"No clip saved for this event yet; fetching it from the NVR now. "
                                     f"Send *event {event['id']}* again in a minute.")
