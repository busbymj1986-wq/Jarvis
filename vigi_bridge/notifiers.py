"""Deliver alarms, clips and reports to Telegram, email and webhooks."""
from __future__ import annotations

import base64
import logging
import os
import smtplib
from email.message import EmailMessage

import requests

log = logging.getLogger("vigi.notify")

TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_VIDEO_LIMIT = 50 * 1024 * 1024


class Notifier:
    name = "notifier"

    def send_alarm(self, alert: dict, snapshot: bytes | None):
        raise NotImplementedError

    def send_clip(self, alert: dict, clip_path: str):
        pass

    def send_report(self, subject: str, text: str, html: str):
        pass


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id

    def _post(self, method: str, data: dict, files: dict | None = None):
        resp = requests.post(f"{self.base}/{method}", data=data, files=files, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram {method} returned HTTP {resp.status_code}: {resp.text[:200]}")

    def send_alarm(self, alert, snapshot):
        if snapshot:
            self._post("sendPhoto", {"chat_id": self.chat_id, "caption": alert["text"][:TELEGRAM_CAPTION_LIMIT]},
                       files={"photo": ("snapshot.jpg", snapshot, "image/jpeg")})
        else:
            self._post("sendMessage", {"chat_id": self.chat_id, "text": alert["text"][:TELEGRAM_TEXT_LIMIT]})

    def send_clip(self, alert, clip_path):
        if os.path.getsize(clip_path) > TELEGRAM_VIDEO_LIMIT:
            log.warning("clip %s is over Telegram's 50 MB limit; not sent", clip_path)
            return
        with open(clip_path, "rb") as f:
            self._post("sendVideo", {"chat_id": self.chat_id, "caption": alert["clip_text"][:TELEGRAM_CAPTION_LIMIT],
                                     "supports_streaming": "true"},
                       files={"video": (os.path.basename(clip_path), f, "video/mp4")})

    def send_report(self, subject, text, html):
        self._post("sendMessage", {"chat_id": self.chat_id, "text": f"{subject}\n\n{text}"[:TELEGRAM_TEXT_LIMIT]})


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.to = cfg["to"] if isinstance(cfg["to"], list) else [cfg["to"]]

    def _send(self, msg: EmailMessage):
        msg["From"] = self.cfg.get("from") or self.cfg.get("username")
        msg["To"] = ", ".join(self.to)
        port = int(self.cfg.get("port", 587))
        if self.cfg.get("ssl"):
            server = smtplib.SMTP_SSL(self.cfg["host"], port, timeout=30)
        else:
            server = smtplib.SMTP(self.cfg["host"], port, timeout=30)
        with server:
            if self.cfg.get("starttls", True) and not self.cfg.get("ssl"):
                server.starttls()
            if self.cfg.get("username"):
                server.login(self.cfg["username"], self.cfg.get("password", ""))
            server.send_message(msg)

    def send_alarm(self, alert, snapshot):
        msg = EmailMessage()
        msg["Subject"] = alert["subject"]
        msg.set_content(alert["text"])
        if snapshot:
            msg.add_attachment(snapshot, maintype="image", subtype="jpeg", filename=f"event_{alert['id']}.jpg")
        self._send(msg)

    def send_clip(self, alert, clip_path):
        if os.path.getsize(clip_path) > 20 * 1024 * 1024:
            log.warning("clip %s too large to email", clip_path)
            return
        msg = EmailMessage()
        msg["Subject"] = f"{alert['subject']} - clip"
        msg.set_content(alert["clip_text"])
        with open(clip_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="video", subtype="mp4", filename=os.path.basename(clip_path))
        self._send(msg)

    def send_report(self, subject, text, html):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        self._send(msg)


class WebhookNotifier(Notifier):
    name = "webhook"

    def __init__(self, url: str, headers: dict | None = None, include_snapshot: bool = False):
        self.url = url
        self.headers = headers or {}
        self.include_snapshot = include_snapshot

    def _post(self, body: dict):
        resp = requests.post(self.url, json=body, headers=self.headers, timeout=15)
        if resp.status_code >= 300:
            raise RuntimeError(f"webhook returned HTTP {resp.status_code}")

    def send_alarm(self, alert, snapshot):
        body = {"kind": "alarm", **alert["payload"]}
        if snapshot and self.include_snapshot:
            body["snapshot_base64"] = base64.b64encode(snapshot).decode("ascii")
        self._post(body)

    def send_clip(self, alert, clip_path):
        self._post({"kind": "clip", **alert["payload"]})

    def send_report(self, subject, text, html):
        self._post({"kind": "report", "subject": subject, "text": text})


def build_notifiers(cfg: dict) -> list[Notifier]:
    n = cfg["notify"]
    out: list[Notifier] = []
    if n["telegram"].get("bot_token") and n["telegram"].get("chat_id"):
        out.append(TelegramNotifier(n["telegram"]["bot_token"], str(n["telegram"]["chat_id"])))
    if n["email"].get("host") and n["email"].get("to"):
        out.append(EmailNotifier(n["email"]))
    if n["webhook"].get("url"):
        out.append(WebhookNotifier(n["webhook"]["url"], n["webhook"].get("headers"),
                                   n["webhook"].get("include_snapshot", False)))
    return out
