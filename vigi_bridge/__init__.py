"""Bolt-on bridge for TP-Link VIGI NVRs.

The NVR's Alarm Server pushes events here; the bridge stores snapshots, pulls
short clips over RTSP, decides when an event deserves an alarm, notifies, and
produces reports. Mount it on a Flask app with init_vigi(app).
"""
from __future__ import annotations

import logging

from .config import load_config
from .routes import bp
from .service import VigiBridge

__all__ = ["init_vigi", "load_config", "VigiBridge"]

log = logging.getLogger("vigi")


def init_vigi(app, cfg: dict | None = None, start: bool = True, **bridge_kwargs) -> VigiBridge:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = cfg or load_config()
    vigi = VigiBridge(cfg, **bridge_kwargs)
    app.extensions["vigi"] = vigi
    app.register_blueprint(bp)
    app.config.setdefault("MAX_CONTENT_LENGTH", 20 * 1024 * 1024)

    if not cfg.get("ingest_token"):
        log.warning("VIGI_INGEST_TOKEN is not set; anyone who can reach /vigi/alarm can post events")
    if not cfg.get("api_key"):
        log.warning("VIGI_API_KEY is not set; reports, events and arming endpoints are disabled")
    if start:
        vigi.start()
    return vigi
