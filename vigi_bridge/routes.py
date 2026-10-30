"""HTTP endpoints under /vigi."""
from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, Response, abort, current_app, jsonify, request, send_file

from .reports import render_csv, render_html, render_text, report_title

log = logging.getLogger("vigi.http")

bp = Blueprint("vigi", __name__, url_prefix="/vigi")

PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def bridge():
    return current_app.extensions["vigi"]


def _matches(supplied: str | None, expected: str) -> bool:
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


def require_key(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        expected = bridge().cfg.get("api_key")
        if not expected:
            return jsonify({"error": "set VIGI_API_KEY to enable this endpoint"}), 503
        if not _matches(request.headers.get("X-API-Key") or request.args.get("key"), expected):
            return jsonify({"error": "invalid or missing API key"}), 401
        return view(*args, **kwargs)
    return wrapper


def _parse_alarm_request() -> tuple[object, bytes | None]:
    """Read the Alarm Server body: JSON, or multipart with event + snapshot parts."""
    if request.mimetype and request.mimetype.startswith("multipart/"):
        event_text, snapshot = request.form.get("event"), None
        for name, part in request.files.items():
            data = part.read()
            is_image = (part.mimetype or "").startswith("image/") or data[:2] == b"\xff\xd8"
            if name == "snapshot" or (is_image and snapshot is None):
                snapshot = data
            elif event_text is None and (name == "event" or not is_image):
                event_text = data.decode("utf-8", errors="replace")
        if event_text is None:
            for value in request.form.values():
                event_text = value
                break
        return (json.loads(event_text) if event_text else {}), snapshot
    raw = request.get_data(cache=True)
    return (json.loads(raw) if raw.strip() else {}), None


@bp.post("/alarm")
@bp.post("/alarm/<token>")
def alarm(token=None):
    expected = bridge().cfg.get("ingest_token")
    if expected and not _matches(token or request.args.get("token") or request.headers.get("X-Ingest-Token"), expected):
        return jsonify({"error": "invalid ingest token"}), 401
    try:
        payload, snapshot = _parse_alarm_request()
        ids = bridge().ingest(payload, snapshot)
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("unreadable alarm push from %s: %s", request.remote_addr, exc)
        return jsonify({"ok": False, "error": f"could not parse alarm message: {exc}"}), 400
    return jsonify({"ok": True, "events": ids})


@bp.get("/status")
@require_key
def status():
    return jsonify(bridge().status())


@bp.post("/arm")
@require_key
def arm():
    mode = (request.get_json(silent=True) or {}).get("mode") or request.args.get("mode", "")
    try:
        bridge().set_arm_mode(mode)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(bridge().status())


def _time_range():
    now = datetime.now(timezone.utc)
    tz = bridge().tz

    def parse(name):
        value = request.args.get(name)
        if not value:
            return None
        dt = datetime.fromisoformat(value)
        return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(timezone.utc)

    try:
        start, end = parse("from"), parse("to")
    except ValueError:
        abort(400, "from/to must be ISO dates such as 2026-10-30 or 2026-10-30T18:00")
    end = end or now
    if start is None:
        period = request.args.get("period", "24h")
        if period not in PERIODS:
            abort(400, f"period must be one of {', '.join(PERIODS)}")
        start = end - PERIODS[period]
    return start, end


def _event_filters():
    channel = request.args.get("channel", type=int)
    return dict(channel=channel, status=request.args.get("status"), label=request.args.get("label"))


@bp.get("/events")
@require_key
def events():
    start, end = _time_range() if (request.args.get("from") or request.args.get("period")) else (None, None)
    limit = min(request.args.get("limit", 100, type=int), 1000)
    rows = bridge().store.list_events(since=start, until=end, limit=limit,
                                      offset=request.args.get("offset", 0, type=int), **_event_filters())
    for r in rows:
        r.pop("raw", None)
    return jsonify(rows)


@bp.get("/events.csv")
@require_key
def events_csv():
    start, end = _time_range()
    rows = bridge().store.list_events(since=start, until=end, limit=None, newest_first=False, **_event_filters())
    return Response(render_csv(rows, bridge().tz), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=vigi_events.csv"})


@bp.get("/events/<int:event_id>")
@require_key
def event_detail(event_id):
    row = bridge().store.get_event(event_id)
    if row is None:
        abort(404)
    return jsonify(row)


def _media(event_id, field, mimetype):
    row = bridge().store.get_event(event_id)
    path = bridge().store.abspath(row and row.get(field))
    if not path:
        abort(404)
    return send_file(path, mimetype=mimetype)


@bp.get("/events/<int:event_id>/snapshot.jpg")
@require_key
def event_snapshot(event_id):
    return _media(event_id, "snapshot", "image/jpeg")


@bp.get("/events/<int:event_id>/clip.mp4")
@require_key
def event_clip(event_id):
    return _media(event_id, "clip", "video/mp4")


@bp.post("/events/<int:event_id>/clip")
@require_key
def request_clip(event_id):
    if bridge().store.get_event(event_id) is None:
        abort(404)
    if not bridge().nvr.configured:
        return jsonify({"error": "NVR host and password are not configured"}), 409
    bridge().schedule_clip(event_id)
    return jsonify({"ok": True, "queued": event_id}), 202


@bp.get("/report")
@require_key
def report():
    start, end = _time_range()
    b = bridge()
    rep = b.report(start, end)
    fmt = request.args.get("format", "json")
    if fmt == "text":
        return Response(render_text(rep), mimetype="text/plain")
    if fmt == "html":
        key = request.args.get("key", "")
        link = (lambda i: f"/vigi/events/{i}/snapshot.jpg?key={key}") if key else None
        return Response(render_html(rep, report_title(rep, b.tz), link), mimetype="text/html")
    return jsonify(rep)


@bp.post("/report/send")
@require_key
def send_report():
    start, end = _time_range()
    sent = bridge().send_report(start, end)
    return jsonify({"ok": bool(sent), "sent_via": sent})
