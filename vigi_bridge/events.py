"""Parse VIGI Alarm Server pushes into normalized events.

A VIGI NVR posts JSON shaped like
    {"device_id": "...", "device_name": "NVR", "messages": [
        {"type": 1, "sub_type": 5, "localtime": "...", "channel": 2, ...}]}
either as application/json or as multipart with an "event" part (the JSON)
and a "snapshot" part (JPEG). VIGI cameras pushing directly use
    {"ip": "...", "mac": "...", "event_list": [{"dateTime": "...", "event_type": ...}]}
Field names vary with firmware and the Enhanced Alarm Message option, so the
parser looks for several spellings and keeps the raw message for inspection.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# (type, sub_type) codes published for the VIGI NVR Alarm Server.
NVR_EVENT_LABELS = {
    (1, 2): "motion",
    (1, 4): "line_crossing",
    (1, 5): "intrusion",
    (2, 13): "alarm_input",
}
# Detection target sub-values (e.g. line crossing by a person or a vehicle).
TARGET_CODES = {101: "person", 102: "vehicle"}

# Free-text event names (cameras, enhanced messages) -> label.
NAME_LABELS = {
    "motion": "motion",
    "motion_detection": "motion",
    "md": "motion",
    "line_crossing": "line_crossing",
    "linecrossing": "line_crossing",
    "line_crossing_detection": "line_crossing",
    "intrusion": "intrusion",
    "intrusion_detection": "intrusion",
    "field_detection": "intrusion",
    "region_intrusion": "intrusion",
    "region_entering": "region_entering",
    "region_entrance": "region_entering",
    "region_exiting": "region_exiting",
    "loitering": "loitering",
    "people": "person",
    "person": "person",
    "human": "person",
    "people_detection": "person",
    "human_detection": "person",
    "vehicle": "vehicle",
    "vehicle_detection": "vehicle",
    "tampering": "tampering",
    "camera_tampering": "tampering",
    "video_tampering": "tampering",
    "scene_change": "scene_change",
    "alarm_in": "alarm_input",
    "alarm_input": "alarm_input",
}

# Most significant first; decides the headline label when an event has several.
LABEL_PRIORITY = [
    "alarm_input",
    "intrusion",
    "region_entering",
    "line_crossing",
    "tampering",
    "loitering",
    "person",
    "vehicle",
    "region_exiting",
    "scene_change",
    "motion",
]

TYPE_KEYS = ("type", "event_type", "eventType", "Event Type")
SUBTYPE_KEYS = ("sub_type", "subtype", "subType", "event_sub_type", "Event Sub Type")
TIME_KEYS = ("localtime", "local_time", "EVENT TIME", "event_time", "dateTime", "datetime", "time", "timestamp")
CHANNEL_KEYS = ("channel", "Camera", "camera", "channel_id", "chn", "port")
CHANNEL_NAME_KEYS = ("channel_name", "Camera Name", "camera_name", "channelName")
TARGET_KEYS = ("target", "target_type", "object_type", "obj_type", "detect_target", "sub_sub_type", "smart_type")

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y%m%dT%H%M%S",
    "%Y%m%d%H%M%S",
)


@dataclass
class Event:
    source: str
    device_id: str
    device_name: str
    channel: int | None
    channel_name: str
    event_type: int | None
    event_subtype: int | None
    labels: list[str]
    event_time: datetime
    raw: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.labels[0] if self.labels else "unknown"

    @property
    def camera_key(self) -> str:
        if self.channel is not None:
            return f"ch{self.channel}"
        return f"dev:{self.device_id or self.device_name}"


def slug(value) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def tags_for(labels) -> set[str]:
    """Every name a rule can match: "line_crossing_person" also matches
    "line_crossing" and "person"."""
    tags = set()
    for label in labels:
        tags.add(label)
        for target in TARGET_CODES.values():
            suffix = "_" + target
            if label.endswith(suffix):
                tags.add(label[: -len(suffix)])
                tags.add(target)
    return tags


def sort_labels(labels) -> list[str]:
    def rank(label):
        base = label
        for target in TARGET_CODES.values():
            if label.endswith("_" + target):
                base = label[: -len(target) - 1]
        return LABEL_PRIORITY.index(base) if base in LABEL_PRIORITY else len(LABEL_PRIORITY)

    return sorted(dict.fromkeys(labels), key=rank)


def _pick(d: dict, keys):
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return None


def _is_number(value) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, str) and value.strip().lstrip("-").isdigit())


def _to_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"-?\d+", str(value))
    return int(match.group()) if match else None


def parse_time(value, tz: ZoneInfo, fallback: datetime) -> datetime:
    """Return an aware UTC datetime. Naive NVR times are in the NVR's timezone."""
    if value is None or value == "" or isinstance(value, bool):
        return fallback
    text = str(value).strip()
    if isinstance(value, (int, float)) or (text.isdigit() and len(text) in (10, 13)):
        num = float(value)
        if num > 1e12:
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=timezone.utc)

    text = text.upper()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in TIME_FORMATS:
        try:
            dt = datetime.strptime(text.rstrip("Z"), fmt)
        except ValueError:
            continue
        local_tz = timezone.utc if text.endswith("Z") else tz
        return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
    return fallback


def _labels_from_names(value) -> list[str]:
    names = value if isinstance(value, list) else [value]
    labels = []
    for name in names:
        if isinstance(name, dict):
            name = _pick(name, ("event_type", "type", "name"))
        if name in (None, ""):
            continue
        s = slug(name)
        labels.append(NAME_LABELS.get(s, s))
    return labels


def _nvr_labels(etype, subtype, target, extra_labels: dict) -> list[str]:
    for key in (f"{etype}:{subtype}:{target}", f"{etype}:{subtype}"):
        if key in extra_labels:
            return [extra_labels[key]]
    base = NVR_EVENT_LABELS.get((etype, subtype))
    if base is None:
        return [f"type{etype}_sub{subtype}"]
    target_name = TARGET_CODES.get(target)
    if target_name:
        return [f"{base}_{target_name}"]
    return [base]


def _event_from_message(msg: dict, device_id, device_name, source, tz, received_at, extra_labels) -> Event:
    raw_type = _pick(msg, TYPE_KEYS)
    etype = subtype = None

    if raw_type is None:
        labels = ["unknown"]
    elif _is_number(raw_type):
        etype = int(raw_type)
        subtype = _to_int(_pick(msg, SUBTYPE_KEYS))
        target = _to_int(_pick(msg, TARGET_KEYS))
        labels = _nvr_labels(etype, subtype, target, extra_labels)
    else:
        # Named event types: "motion", ["people", "intrusion"].
        labels = _labels_from_names(raw_type)

    channel = _to_int(_pick(msg, CHANNEL_KEYS))
    channel_name = _pick(msg, CHANNEL_NAME_KEYS)
    return Event(
        source=source,
        device_id=str(device_id or ""),
        device_name=str(device_name or ""),
        channel=channel,
        channel_name=str(channel_name) if channel_name else (f"Channel {channel}" if channel is not None else str(device_name or device_id or "Camera")),
        event_type=etype,
        event_subtype=subtype,
        labels=sort_labels(labels),
        event_time=parse_time(_pick(msg, TIME_KEYS), tz, received_at),
        raw=msg,
    )


def parse_push(payload, tz: ZoneInfo, received_at: datetime, extra_labels: dict | None = None) -> list[Event]:
    """Turn one Alarm Server request body into a list of events."""
    extra_labels = extra_labels or {}
    if isinstance(payload, list):
        payload = {"messages": payload}
    if not isinstance(payload, dict):
        raise ValueError("alarm payload must be a JSON object")

    device_id = _pick(payload, ("device_id", "deviceId", "mac", "MAC", "sn"))
    device_name = _pick(payload, ("device_name", "deviceName", "name", "ip", "IP"))

    if "messages" in payload:
        messages, source = payload.get("messages") or [], "nvr"
    elif "event_list" in payload:
        messages, source = payload.get("event_list") or [], "camera"
    elif _pick(payload, TYPE_KEYS) is not None:
        messages, source = [payload], "nvr"
    else:
        messages, source = [], "nvr"

    events = []
    for msg in messages:
        if isinstance(msg, dict):
            events.append(_event_from_message(msg, device_id, device_name, source, tz, received_at, extra_labels))
    return events
