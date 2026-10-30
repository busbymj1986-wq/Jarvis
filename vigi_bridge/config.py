"""Configuration for the VIGI bridge.

Settings come from three layers, later ones winning:
built-in DEFAULTS -> JSON file (VIGI_CONFIG) -> environment variables (secrets).
"""
from __future__ import annotations

import copy
import json
import os

DEFAULTS = {
    "data_dir": "vigi_data",
    # Timezone the NVR clock runs in. Needed to turn NVR local times into UTC
    # for clip replay and to evaluate the armed schedule.
    "timezone": "UTC",
    # Base URL this bridge is reachable on, used for links in notifications.
    "public_url": "",
    # Shared secret the NVR puts in its Alarm Server URL (/vigi/alarm/<token>).
    "ingest_token": "",
    # Key required (X-API-Key header or ?key=) for reports, events and arming.
    "api_key": "",
    "retention_days": 30,
    "nvr": {
        "host": "",
        "username": "admin",
        "password": "",
        "https_port": 443,
        "rtsp_port": 554,
        "verify_tls": False,
    },
    "snapshots": {
        # Pull a snapshot from the NVR when a push arrives without one.
        "fetch_if_missing": True,
    },
    "clips": {
        "enabled": True,
        # Which events get a clip: "alarms", "candidates" or "all".
        "for": "alarms",
        "pre_seconds": 5,
        "post_seconds": 10,
        # 1 = main stream, 2 = sub stream (smaller files, faster to send).
        "stream": 2,
        # "replay" pulls recorded footage (gives pre-roll, needs recording on);
        # "live" records the live stream from the moment of processing.
        "source": "replay",
        # Seconds to wait after the post-roll before asking the NVR for replay.
        "ready_delay_seconds": 15,
        # Re-encode to H.264 baseline so phones can play H.265 / High-profile
        # camera streams. "auto" re-encodes when WhatsApp is set up, since
        # WhatsApp only accepts H.264 without B-frames.
        "transcode": "auto",
    },
    "ai": {
        # true / false / "auto" (on when ANTHROPIC_API_KEY is set).
        "enabled": "auto",
        "model": "claude-opus-5-5",
        "effort": "low",
        # "verify": AI must confirm before an alarm is sent.
        # "advisory": alarm per rules, AI only adds a description.
        "mode": "verify",
        # When the AI call fails: "alarm" (fail safe) or "dismiss".
        "on_error": "alarm",
        # Event labels that alarm without an AI check (sensor or covered lens).
        "skip_labels": ["alarm_input", "tampering"],
        # Free-text facts about the site that help judge what is normal.
        "site_notes": "",
    },
    "rules": {
        # "armed", "disarmed" or "schedule". Overridden at runtime by /vigi/arm.
        "mode": "schedule",
        "schedule": [{"start": "18:00", "end": "06:00"}],
        # Event labels that can raise an alarm while armed.
        "alarm_events": [
            "intrusion",
            "line_crossing",
            "region_entering",
            "person",
            "alarm_input",
            "tampering",
        ],
        # Per-channel overrides, keyed by channel number, device id or name:
        # {"1": {"name": "Gate", "enabled": true, "always_armed": false,
        #        "alarm_events": ["intrusion"]}}
        "channels": {},
        # Do not re-alert for the same camera within this many seconds.
        "cooldown_seconds": 120,
        # Promote log-only events (e.g. motion) when several cameras fire together.
        "escalate": {
            "enabled": True,
            "window_seconds": 60,
            "min_events": 3,
            "min_channels": 2,
        },
    },
    # Extra NVR type codes -> label, e.g. {"1:6": "region_entering"}.
    "event_labels": {},
    "notify": {
        # WhatsApp Cloud API (Meta). Free-form messages only reach people who
        # messaged the business number in the last 24 hours; outside that the
        # bridge uses the approved templates named here.
        "whatsapp": {
            "token": "",
            "phone_number_id": "",
            # Numbers that get alerts and may send commands, international
            # format, e.g. "27821234567".
            "recipients": [],
            "api_version": "v23.0",
            # Webhook for incoming commands: verify token you choose, and the
            # Meta app secret used to check each request's signature.
            "verify_token": "",
            "app_secret": "",
            "commands": True,
            "language": "en",
            # IMAGE header; body {{1}} camera, {{2}} event and time, {{3}} details.
            "alarm_template": "",
            # VIDEO header; body {{1}} camera, {{2}} event and time.
            "clip_template": "",
            # Body {{1}} period, {{2}} one-line summary.
            "report_template": "",
        },
        "webhook": {"url": "", "headers": {}, "include_snapshot": False},
        "telegram": {"bot_token": "", "chat_id": ""},
        "email": {
            "host": "",
            "port": 587,
            "username": "",
            "password": "",
            "starttls": True,
            "ssl": False,
            "from": "",
            "to": [],
        },
    },
    "report": {
        # Local time to send the daily summary, or "" to disable.
        "daily_at": "07:00",
    },
}

# Environment variable -> config path. Keeps secrets out of the JSON file.
ENV_MAP = {
    "VIGI_DATA_DIR": ("data_dir",),
    "VIGI_TIMEZONE": ("timezone",),
    "VIGI_PUBLIC_URL": ("public_url",),
    "VIGI_INGEST_TOKEN": ("ingest_token",),
    "VIGI_API_KEY": ("api_key",),
    "VIGI_NVR_HOST": ("nvr", "host"),
    "VIGI_NVR_USER": ("nvr", "username"),
    "VIGI_NVR_PASSWORD": ("nvr", "password"),
    "VIGI_ARM_MODE": ("rules", "mode"),
    "VIGI_AI_MODEL": ("ai", "model"),
    "VIGI_WEBHOOK_URL": ("notify", "webhook", "url"),
    "WHATSAPP_TOKEN": ("notify", "whatsapp", "token"),
    "WHATSAPP_PHONE_NUMBER_ID": ("notify", "whatsapp", "phone_number_id"),
    "WHATSAPP_TO": ("notify", "whatsapp", "recipients"),
    "WHATSAPP_VERIFY_TOKEN": ("notify", "whatsapp", "verify_token"),
    "WHATSAPP_APP_SECRET": ("notify", "whatsapp", "app_secret"),
    "WHATSAPP_ALARM_TEMPLATE": ("notify", "whatsapp", "alarm_template"),
    "WHATSAPP_CLIP_TEMPLATE": ("notify", "whatsapp", "clip_template"),
    "WHATSAPP_REPORT_TEMPLATE": ("notify", "whatsapp", "report_template"),
    "TELEGRAM_BOT_TOKEN": ("notify", "telegram", "bot_token"),
    "TELEGRAM_CHAT_ID": ("notify", "telegram", "chat_id"),
    "SMTP_HOST": ("notify", "email", "host"),
    "SMTP_PORT": ("notify", "email", "port"),
    "SMTP_USER": ("notify", "email", "username"),
    "SMTP_PASSWORD": ("notify", "email", "password"),
    "ALERT_EMAIL_FROM": ("notify", "email", "from"),
    "ALERT_EMAIL_TO": ("notify", "email", "to"),
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def whatsapp_configured(cfg: dict) -> bool:
    wa = cfg["notify"]["whatsapp"]
    return bool(wa.get("token") and wa.get("phone_number_id") and wa.get("recipients"))


def _coerce(path, value: str):
    if path in (("notify", "email", "to"), ("notify", "whatsapp", "recipients")):
        return [v.strip() for v in value.split(",") if v.strip()]
    if path == ("notify", "email", "port"):
        return int(value)
    return value


def load_config(path: str | None = None, overrides: dict | None = None, env=None) -> dict:
    env = os.environ if env is None else env
    cfg = copy.deepcopy(DEFAULTS)

    path = path or env.get("VIGI_CONFIG")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))

    for var, keys in ENV_MAP.items():
        if env.get(var):
            node = cfg
            for key in keys[:-1]:
                node = node.setdefault(key, {})
            node[keys[-1]] = _coerce(keys, env[var])

    if overrides:
        cfg = deep_merge(cfg, overrides)
    return cfg
