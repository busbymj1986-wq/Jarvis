"""The bridge pipeline: ingest -> rules -> cooldown -> AI check -> notify -> clip."""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .ai import AnalysisError, SnapshotAnalyzer
from .config import whatsapp_configured
from .events import parse_push
from .notifiers import build_notifiers
from .nvr import NvrClient
from .reports import build_report, render_html, render_text, report_title
from .rules import ARM_MODES, RuleEngine, _parse_hhmm
from .store import Store, from_iso, iso
from .whatsapp import WhatsAppBot, WhatsAppClient, Windows

log = logging.getLogger("vigi")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VigiBridge:
    def __init__(self, cfg: dict, store=None, nvr=None, analyzer=None, notifiers=None, clock=utcnow):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.get("timezone") or "UTC")
        self.store = store or Store(cfg["data_dir"])
        self.nvr = nvr or NvrClient(cfg)
        self.analyzer = analyzer or SnapshotAnalyzer(cfg)
        self.now = clock
        self.whatsapp_windows = Windows(self.store, clock)
        self.notifiers = build_notifiers(cfg, self.whatsapp_windows) if notifiers is None else notifiers
        self.rules = RuleEngine(cfg, self.store, self.tz)
        self.whatsapp = None
        if whatsapp_configured(cfg):
            self.whatsapp = WhatsAppBot(self, WhatsAppClient(cfg["notify"]["whatsapp"]), self.whatsapp_windows)
        self._events: queue.Queue = queue.Queue()
        self._clips: queue.PriorityQueue = queue.PriorityQueue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._skew_warned = False

    # --- lifecycle ----------------------------------------------------------

    def start(self):
        if self._threads:
            return
        for target, name in ((self._event_loop, "vigi-events"), (self._clip_loop, "vigi-clips"),
                             (self._scheduler_loop, "vigi-scheduler")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        # Pick up events left pending by a restart.
        for e in self.store.list_events(status="pending", limit=None, newest_first=False):
            self._events.put(e["id"])

    def stop(self):
        self._stop.set()

    # --- ingest -------------------------------------------------------------

    def ingest(self, payload, snapshot: bytes | None = None) -> list[int]:
        """Store a push from the NVR and queue it. Returns the new event ids."""
        received = self.now()
        events = parse_push(payload, self.tz, received, self.cfg.get("event_labels"))
        if events and not self._skew_warned:
            skew = abs((events[0].event_time - received).total_seconds())
            if skew > 15 * 60:
                self._skew_warned = True
                log.warning("NVR event time is %d minutes away from this server's clock; check that the "
                            "timezone setting (%s) matches the NVR and the NVR clock is right, or clip "
                            "replay will fetch the wrong footage", skew // 60, self.tz)
        ids = []
        for ev in events:
            rule = self.rules.channel_rule({"channel": ev.channel, "device_id": ev.device_id,
                                            "device_name": ev.device_name})
            if rule.get("name"):
                ev.channel_name = rule["name"]
            event_id = self.store.add_event(ev, received)
            # A push carries one snapshot; it belongs to the first message's camera.
            if snapshot and ev.camera_key == events[0].camera_key:
                rel = self.store.save_media(event_id, ev.event_time, "jpg", snapshot)
                self.store.update_event(event_id, snapshot=rel)
            ids.append(event_id)
            self._events.put(event_id)
        if events:
            log.info("received %d event(s) from %s: %s", len(events), events[0].device_name or "device",
                     ", ".join(f"{e.channel_name}/{e.label}" for e in events))
        else:
            log.info("received a push with no events (NVR connection test?): %.300s", payload)
        return ids

    # --- processing ---------------------------------------------------------

    def _event_loop(self):
        while not self._stop.is_set():
            try:
                event_id = self._events.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.process(event_id)
            except Exception:
                log.exception("failed to process event %s", event_id)
                self.store.update_event(event_id, status="error", reason="processing failed; see server log")

    def process(self, event_id: int) -> dict:
        event = self.store.get_event(event_id)
        if event is None or event["status"] != "pending":
            return event

        decision = self.rules.evaluate(event)
        if decision.action != "candidate":
            status = "ignored" if decision.action == "ignore" else "logged"
            self.store.update_event(event_id, status=status, armed=int(decision.armed), reason=decision.reason)
            if self.cfg["clips"].get("for") == "all" and decision.action != "ignore":
                self.schedule_clip(event_id)
            return self.store.get_event(event_id)

        self.store.update_event(event_id, armed=1)
        earlier = self.rules.cooldown_hit(event)
        if earlier:
            age = int((from_iso(event["event_time"]) - from_iso(earlier["event_time"])).total_seconds())
            self.store.update_event(event_id, status="grouped",
                                    reason=f"already alerted for this camera {age}s earlier (event {earlier['id']})")
            return self.store.get_event(event_id)

        snapshot = self._snapshot_for(event)
        verdict, ai_error = None, None
        skip_ai = set(event.get("labels") or []) & set(self.cfg["ai"].get("skip_labels", []))
        if self.analyzer.enabled and self.cfg["ai"].get("mode", "verify") != "off" and snapshot and not skip_ai:
            try:
                verdict = self.analyzer.analyze(snapshot, self._ai_context(event, decision.reason))
            except AnalysisError as exc:
                ai_error = str(exc)
                log.warning("AI check failed for event %s: %s", event_id, ai_error)

        raise_alarm, reason = self._final_call(decision.reason, verdict, ai_error, snapshot is not None, bool(skip_ai))
        self.store.update_event(event_id, ai=verdict or ({"error": ai_error} if ai_error else None), reason=reason)
        if not raise_alarm:
            self.store.update_event(event_id, status="dismissed")
            if self.cfg["clips"].get("for") in ("candidates", "all"):
                self.schedule_clip(event_id)
            return self.store.get_event(event_id)

        self.store.update_event(event_id, status="alarm")
        event = self.store.get_event(event_id)
        sent = self._notify("alarm", event, snapshot)
        self.store.update_event(event_id, notified_at=iso(self.now()), notified_via=sent)
        if self.cfg["clips"].get("enabled", True):
            self.schedule_clip(event_id)
        return self.store.get_event(event_id)

    def _final_call(self, rule_reason, verdict, ai_error, has_snapshot, skipped) -> tuple[bool, str]:
        ai_cfg = self.cfg["ai"]
        if skipped:
            return True, f"{rule_reason} (sensor event, no AI check)"
        if not self.analyzer.enabled or ai_cfg.get("mode") == "off":
            return True, rule_reason
        if not has_snapshot:
            return True, f"{rule_reason} (no snapshot available for AI check)"
        if ai_error:
            if ai_cfg.get("on_error", "alarm") == "alarm":
                return True, f"{rule_reason} (AI check failed: {ai_error})"
            return False, f"AI check failed and on_error is dismiss: {ai_error}"
        if ai_cfg.get("mode") == "advisory":
            return True, f"{rule_reason}; AI: {verdict.get('reason', '')}"
        if verdict.get("alarm"):
            return True, f"{rule_reason}; AI confirmed: {verdict.get('reason', '')}"
        return False, f"AI dismissed: {verdict.get('reason', '')}"

    def _snapshot_for(self, event: dict) -> bytes | None:
        data = self.store.read_media(event.get("snapshot"))
        if data is None and self.cfg["snapshots"].get("fetch_if_missing", True) and self.nvr.configured:
            data = self.nvr.snapshot(event.get("channel"))
            if data:
                rel = self.store.save_media(event["id"], from_iso(event["event_time"]), "jpg", data)
                self.store.update_event(event["id"], snapshot=rel)
        return data

    def _ai_context(self, event: dict, why: str) -> dict:
        local = from_iso(event["event_time"]).astimezone(self.tz)
        return {
            "camera": event["channel_name"],
            "event": ", ".join(l.replace("_", " ") for l in (event.get("labels") or [event["label"]])),
            "local time": local.strftime("%A %d %B %Y %H:%M:%S"),
            "why it was flagged": why,
        }

    # --- notifications ------------------------------------------------------

    def alert_for(self, event: dict) -> dict:
        local = from_iso(event["event_time"]).astimezone(self.tz)
        ai = event.get("ai") or {}
        what = event["label"].replace("_", " ").title()
        cam = event["channel_name"]
        detail = (ai.get("description") or event.get("reason") or "").rstrip(". ")
        lines = [f"🚨 ALARM - {cam}", f"{what} at {local:%a %d %b %H:%M:%S}"]
        if ai.get("description"):
            threat = f" (threat: {ai['threat_level']})" if ai.get("threat_level") else ""
            lines.append(f"{ai['description']}{threat}")
        else:
            lines.append(event.get("reason") or "")
        public = (self.cfg.get("public_url") or "").rstrip("/")
        link = f"{public}/vigi/events/{event['id']}" if public else None
        if link:
            lines.append(link)
        return {
            "id": event["id"],
            "channel": event["channel"],
            # Body parameters for the WhatsApp templates: camera, event and time, details.
            "template_params": [cam, f"{what} at {local:%H:%M:%S on %d %b}", detail],
            "subject": f"[Jarvis] ALARM: {what} at {cam} ({local:%H:%M})",
            "text": "\n".join(lines),
            "clip_text": f"🎥 Clip - {cam}, {what} at {local:%H:%M:%S}",
            "payload": {
                "event_id": event["id"],
                "camera": cam,
                "channel": event["channel"],
                "event": event["label"],
                "time": event["event_time"],
                "local_time": local.isoformat(),
                "status": event["status"],
                "reason": event.get("reason"),
                "ai": ai or None,
                "link": link,
                "has_clip": bool(event.get("clip")),
            },
        }

    def _notify(self, kind: str, event: dict, data=None) -> list[str]:
        alert = self.alert_for(event)
        sent = []
        for n in self.notifiers:
            try:
                if kind == "alarm":
                    n.send_alarm(alert, data)
                else:
                    n.send_clip(alert, data)
                sent.append(n.name)
            except Exception as exc:
                log.warning("%s notification failed for event %s: %s", n.name, event["id"], exc)
        log.info("%s for event %s sent via %s", kind, event["id"], ", ".join(sent) or "nothing (no notifiers set up)")
        return sent

    # --- clips --------------------------------------------------------------

    def schedule_clip(self, event_id: int, delay: float | None = None):
        if not self.nvr.configured:
            return
        event = self.store.get_event(event_id)
        clips = self.cfg["clips"]
        if delay is None:
            if clips.get("source", "replay") == "live":
                delay = 0
            else:
                ready = from_iso(event["event_time"]) + timedelta(
                    seconds=clips.get("post_seconds", 10) + clips.get("ready_delay_seconds", 15))
                delay = max(0.0, (ready - self.now()).total_seconds())
        self._clips.put((time.time() + delay, event_id))

    def _clip_loop(self):
        while not self._stop.is_set():
            try:
                due, event_id = self._clips.get(timeout=1)
            except queue.Empty:
                continue
            wait = due - time.time()
            if wait > 0:
                self._clips.put((due, event_id))
                self._stop.wait(min(wait, 1.0))
                continue
            try:
                self.capture_clip(event_id)
            except Exception:
                log.exception("clip capture failed for event %s", event_id)

    def capture_clip(self, event_id: int) -> str | None:
        event = self.store.get_event(event_id)
        if event is None or event.get("clip"):
            return event and event.get("clip")
        when = from_iso(event["event_time"])
        rel = self.store.media_relpath(event_id, when, "mp4")
        if not self.nvr.record_clip(event.get("channel"), when, self.store.abspath(rel)):
            return None
        self.store.update_event(event_id, clip=rel)
        if event["status"] == "alarm":
            self._notify("clip", self.store.get_event(event_id), self.store.abspath(rel))
        return rel

    # --- arming, status, reports -------------------------------------------

    def set_arm_mode(self, mode: str):
        if mode not in ARM_MODES:
            raise ValueError(f"mode must be one of {', '.join(ARM_MODES)}")
        self.store.set_setting("arm_mode", mode)
        log.info("arm mode set to %s", mode)

    def status(self) -> dict:
        now = self.now()
        armed, why = self.rules.is_armed(now)
        midnight = now.astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        today = build_report(self.store, midnight, now + timedelta(seconds=1), self.tz)
        return {
            "arm_mode": self.rules.arm_mode(),
            "armed_now": armed,
            "armed_reason": why,
            "local_time": now.astimezone(self.tz).isoformat(timespec="seconds"),
            "nvr_configured": self.nvr.configured,
            "ffmpeg_available": bool(self.nvr.ffmpeg),
            "ai_enabled": self.analyzer.enabled,
            "ai_mode": self.cfg["ai"].get("mode"),
            "notifiers": [n.name for n in self.notifiers],
            "whatsapp_commands": bool(self.whatsapp and self.cfg["notify"]["whatsapp"].get("app_secret")),
            "queued_events": self._events.qsize(),
            "queued_clips": self._clips.qsize(),
            "today": {k: today[k] for k in ("total_events", "alarms_sent", "dismissed_by_ai", "grouped", "logged")},
        }

    def report(self, start: datetime, end: datetime) -> dict:
        return build_report(self.store, start, end, self.tz)

    def send_report(self, start: datetime, end: datetime) -> list[str]:
        rep = self.report(start, end)
        title = report_title(rep, self.tz)
        text, html = render_text(rep), render_html(rep, title)
        sent = []
        for n in self.notifiers:
            try:
                n.send_report(title, text, html)
                sent.append(n.name)
            except Exception as exc:
                log.warning("%s report delivery failed: %s", n.name, exc)
        return sent

    def _scheduler_loop(self):
        next_purge = 0.0
        while not self._stop.wait(30):
            try:
                self.send_daily_report_if_due()
                if time.time() >= next_purge:
                    self.purge_old_events()
                    next_purge = time.time() + 3600
            except Exception:
                log.exception("scheduled task failed")

    def send_daily_report_if_due(self) -> bool:
        daily_at = self.cfg["report"].get("daily_at")
        if not daily_at:
            return False
        local = self.now().astimezone(self.tz)
        send_time = _parse_hhmm(daily_at)
        if local.time() < send_time or not self.store.claim_setting("last_daily_report", local.date().isoformat()):
            return False
        end = local.replace(hour=send_time.hour, minute=send_time.minute, second=0, microsecond=0)
        self.send_report(end - timedelta(days=1), end)
        return True

    def purge_old_events(self) -> int:
        days = self.cfg.get("retention_days")
        if not days:
            return 0
        removed = self.store.purge_before(self.now() - timedelta(days=days))
        if removed:
            log.info("removed %d events older than %d days", removed, days)
        return removed
