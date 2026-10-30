"""Summaries of what the cameras saw over a period."""
from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from .store import from_iso, iso


def _local(text: str, tz: ZoneInfo) -> datetime:
    return from_iso(text).astimezone(tz)


def build_report(store, start: datetime, end: datetime, tz: ZoneInfo) -> dict:
    events = store.list_events(since=start, until=end, limit=None, newest_first=False)
    by_status = Counter(e["status"] for e in events)
    by_label = Counter(e["label"] for e in events)
    by_hour = Counter(_local(e["event_time"], tz).hour for e in events)

    cameras: dict[str, dict] = {}
    for e in events:
        cam = cameras.setdefault(e["channel_name"] or e["camera_key"], {"events": 0, "alarms": 0})
        cam["events"] += 1
        if e["status"] == "alarm":
            cam["alarms"] += 1

    alarms = []
    for e in events:
        if e["status"] != "alarm":
            continue
        ai = e.get("ai") or {}
        alarms.append({
            "id": e["id"],
            "time": _local(e["event_time"], tz).strftime("%a %d %b %H:%M:%S"),
            "camera": e["channel_name"],
            "event": e["label"],
            "description": ai.get("description") or e.get("reason"),
            "threat_level": ai.get("threat_level"),
            "has_clip": bool(e.get("clip")),
        })

    busiest_hour = max(by_hour, key=by_hour.get) if by_hour else None
    busiest_camera = max(cameras, key=lambda c: cameras[c]["events"]) if cameras else None
    return {
        "from": iso(start),
        "to": iso(end),
        "timezone": str(tz),
        "total_events": len(events),
        "alarms_sent": by_status.get("alarm", 0),
        "grouped": by_status.get("grouped", 0),
        "dismissed_by_ai": by_status.get("dismissed", 0),
        "logged": by_status.get("logged", 0),
        "by_status": dict(by_status),
        "by_event": dict(by_label.most_common()),
        "by_camera": cameras,
        "by_hour": {f"{h:02d}:00": by_hour[h] for h in sorted(by_hour)},
        "busiest_hour": f"{busiest_hour:02d}:00" if busiest_hour is not None else None,
        "busiest_camera": busiest_camera,
        "alarms": alarms,
    }


def report_title(report: dict, tz: ZoneInfo) -> str:
    start = from_iso(report["from"]).astimezone(tz)
    end = from_iso(report["to"]).astimezone(tz)
    return f"Jarvis security report {start:%d %b %H:%M} - {end:%d %b %H:%M}"


def render_text(report: dict) -> str:
    lines = [
        f"Events: {report['total_events']}  |  Alarms sent: {report['alarms_sent']}  |  "
        f"Dismissed by AI: {report['dismissed_by_ai']}  |  Grouped: {report['grouped']}",
    ]
    if report["busiest_camera"]:
        lines.append(f"Busiest camera: {report['busiest_camera']}  |  Busiest hour: {report['busiest_hour']}")
    if report["by_event"]:
        lines.append("By type: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in report["by_event"].items()))
    if report["alarms"]:
        lines.append("")
        lines.append("Alarms:")
        for a in report["alarms"]:
            threat = f" [{a['threat_level']}]" if a["threat_level"] else ""
            lines.append(f"- {a['time']} {a['camera']} ({a['event'].replace('_', ' ')}){threat}: {a['description']}")
    else:
        lines.append("No alarms in this period.")
    return "\n".join(lines)


def render_html(report: dict, title: str, media_link=None) -> str:
    """media_link(event_id) -> URL for the snapshot, or None to omit thumbnails."""
    def row(cells, tag="td"):
        return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"

    stats = [
        ("Events", report["total_events"]),
        ("Alarms sent", report["alarms_sent"]),
        ("Dismissed by AI", report["dismissed_by_ai"]),
        ("Grouped", report["grouped"]),
        ("Busiest camera", escape(report["busiest_camera"] or "-")),
        ("Busiest hour", report["busiest_hour"] or "-"),
    ]
    camera_rows = "".join(
        row([escape(name), c["events"], c["alarms"]]) for name, c in sorted(report["by_camera"].items())
    )
    alarm_rows = ""
    for a in report["alarms"]:
        thumb = ""
        if media_link:
            url = escape(media_link(a["id"]), quote=True)
            thumb = f'<a href="{url}"><img src="{url}" alt="snapshot" width="160"></a>'
        alarm_rows += row([
            escape(a["time"]), escape(a["camera"] or ""), escape(a["event"].replace("_", " ")),
            escape(a["threat_level"] or "-"), escape(a["description"] or ""), thumb,
        ])

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;color:#1d1d1f;background:#fff}}
table{{border-collapse:collapse;margin:8px 0 20px;width:100%;max-width:960px}}
td,th{{border:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f4f4f6}} .stats td:first-child{{font-weight:600;width:180px}}
</style></head><body>
<h1>{escape(title)}</h1>
<table class="stats">{"".join(row([k, v]) for k, v in stats)}</table>
<h2>Cameras</h2>
<table>{row(["Camera", "Events", "Alarms"], "th")}{camera_rows}</table>
<h2>Alarms</h2>
{"<table>" + row(["Time", "Camera", "Event", "Threat", "What was seen", "Snapshot"], "th") + alarm_rows + "</table>" if alarm_rows else "<p>No alarms in this period.</p>"}
</body></html>"""


CSV_FIELDS = ["id", "event_time", "channel", "channel_name", "label", "status", "reason",
              "threat_level", "description", "snapshot", "clip"]


def render_csv(events: list[dict], tz: ZoneInfo) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for e in events:
        ai = e.get("ai") or {}
        writer.writerow({
            "id": e["id"],
            "event_time": _local(e["event_time"], tz).strftime("%Y-%m-%d %H:%M:%S"),
            "channel": e["channel"],
            "channel_name": e["channel_name"],
            "label": e["label"],
            "status": e["status"],
            "reason": e["reason"],
            "threat_level": ai.get("threat_level", ""),
            "description": ai.get("description", ""),
            "snapshot": "yes" if e.get("snapshot") else "",
            "clip": "yes" if e.get("clip") else "",
        })
    return buf.getvalue()
