"""Send a fake VIGI NVR Alarm Server push to the bridge.

Examples:
  python tools/simulate_nvr.py --url http://localhost:5000 --token nvr-token --event intrusion --channel 1
  python tools/simulate_nvr.py --url http://localhost:5000 --token nvr-token --event motion --snapshot gate.jpg
"""
import argparse
import json
import sys
from datetime import datetime

import requests

EVENTS = {
    "motion": {"type": 1, "sub_type": 2},
    "line_crossing": {"type": 1, "sub_type": 4},
    "line_crossing_person": {"type": 1, "sub_type": 4, "target": 101},
    "line_crossing_vehicle": {"type": 1, "sub_type": 4, "target": 102},
    "intrusion": {"type": 1, "sub_type": 5},
    "alarm_input": {"type": 2, "sub_type": 13},
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:5000", help="bridge base URL")
    parser.add_argument("--token", default="", help="VIGI_INGEST_TOKEN")
    parser.add_argument("--event", default="intrusion", choices=sorted(EVENTS))
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--name", default=None, help="camera name")
    parser.add_argument("--time", default=None, help="NVR local time, default now (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--snapshot", default=None, help="JPEG file to attach")
    args = parser.parse_args()

    message = {
        **EVENTS[args.event],
        "localtime": args.time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "channel": args.channel,
        "channel_name": args.name or f"Camera {args.channel}",
    }
    payload = {"device_id": "SIMULATED-NVR", "device_name": "Simulated NVR", "messages": [message]}
    url = args.url.rstrip("/") + "/vigi/alarm" + (f"/{args.token}" if args.token else "")

    if args.snapshot:
        with open(args.snapshot, "rb") as f:
            files = {
                "event": ("event.json", json.dumps(payload), "application/json"),
                "snapshot": ("snapshot.jpg", f.read(), "image/jpeg"),
            }
        resp = requests.post(url, files=files, timeout=15)
    else:
        resp = requests.post(url, json=payload, timeout=15)

    print(resp.status_code, resp.text.strip())
    return 0 if resp.ok else 1


if __name__ == "__main__":
    sys.exit(main())
