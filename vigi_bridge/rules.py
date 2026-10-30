"""Decide whether an event is worth an alarm.

The rule pass is cheap and runs first. Only events it marks as "candidate"
go on to the cooldown check, the AI snapshot check and notification.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from .events import tags_for
from .store import from_iso

ARM_MODES = ("armed", "disarmed", "schedule")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class Decision:
    action: str  # "ignore", "log" or "candidate"
    armed: bool
    reason: str


def _parse_hhmm(text: str) -> dtime:
    hours, minutes = str(text).split(":")[:2]
    return dtime(int(hours) % 24, int(minutes))


def in_schedule(local: datetime, windows: list[dict]) -> bool:
    """True if a local datetime falls inside any window.

    A window is {"start": "18:00", "end": "06:00", "days": ["mon", ...]}.
    Windows that end before they start run past midnight; "days" refers to
    the day the window starts on. Omitting "days" means every day.
    """
    now_t = local.time()
    today = DAYS[local.weekday()]
    yesterday = DAYS[(local.weekday() - 1) % 7]
    for w in windows or []:
        start, end = _parse_hhmm(w.get("start", "00:00")), _parse_hhmm(w.get("end", "00:00"))
        days = [d[:3].lower() for d in w.get("days", DAYS)]
        if start < end:
            if today in days and start <= now_t < end:
                return True
        else:  # overnight, or start == end meaning all day
            if today in days and now_t >= start:
                return True
            if yesterday in days and now_t < end:
                return True
    return False


class RuleEngine:
    def __init__(self, cfg: dict, store, tz: ZoneInfo):
        self.cfg = cfg
        self.rules = cfg["rules"]
        self.store = store
        self.tz = tz

    def arm_mode(self) -> str:
        mode = self.store.get_setting("arm_mode") or self.rules.get("mode", "schedule")
        return mode if mode in ARM_MODES else "schedule"

    def is_armed(self, when: datetime) -> tuple[bool, str]:
        mode = self.arm_mode()
        if mode == "armed":
            return True, "system armed"
        if mode == "disarmed":
            return False, "system disarmed"
        local = when.astimezone(self.tz)
        if in_schedule(local, self.rules.get("schedule", [])):
            return True, "inside armed schedule"
        return False, "outside armed schedule"

    def channel_rule(self, event: dict) -> dict:
        channels = self.rules.get("channels", {})
        for key in (event.get("channel"), event.get("device_id"), event.get("device_name")):
            if key not in (None, "") and str(key) in channels:
                return channels[str(key)]
        return {}

    def evaluate(self, event: dict) -> Decision:
        when = from_iso(event["event_time"])
        rule = self.channel_rule(event)
        if rule.get("enabled", True) is False:
            return Decision("ignore", False, "camera disabled in rules")

        if rule.get("always_armed"):
            armed, why = True, "camera is always armed"
        else:
            armed, why = self.is_armed(when)
        if not armed:
            return Decision("log", False, why)

        tags = tags_for(event.get("labels") or [event.get("label", "")])
        alarm_events = set(rule.get("alarm_events", self.rules.get("alarm_events", [])))
        hit = sorted(tags & alarm_events)
        if hit:
            return Decision("candidate", True, f"{why}: {hit[0].replace('_', ' ')}")

        esc = self.rules.get("escalate", {})
        if esc.get("enabled"):
            window = timedelta(seconds=esc.get("window_seconds", 60))
            count, cameras = self.store.armed_activity(when - window, when, exclude_id=event["id"])
            count += 1
            cameras.add(event["camera_key"])
            if count >= esc.get("min_events", 3) and len(cameras) >= esc.get("min_channels", 2):
                return Decision(
                    "candidate", True,
                    f"{why}: {count} events on {len(cameras)} cameras within {int(window.total_seconds())}s",
                )

        return Decision("log", True, f"{why}: {event.get('label', 'event').replace('_', ' ')} is log-only")

    def cooldown_hit(self, event: dict) -> dict | None:
        seconds = self.rules.get("cooldown_seconds", 0)
        if not seconds:
            return None
        when = from_iso(event["event_time"])
        return self.store.last_alarm(event["camera_key"], when - timedelta(seconds=seconds), exclude_id=event["id"])
