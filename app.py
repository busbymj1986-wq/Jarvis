from flask import Flask, request, jsonify
from datetime import datetime
import os

app = Flask(__name__)

state = {
    "status": "Active",
    "customer": "Greg Ross",
    "account": "0328150400",
    "current_line": 0,
    "start_time": None,
    "transcript": [],
    "sms_sent": False
}

LINES = [
    {"speaker": "Customer", "text": "Hi, my internet’s completely dead again."},
    {"speaker": "Agent", "text": "Oxygen support, got you. Name and account number?"},
    {"speaker": "Customer", "text": "Greg Ross, 0328150400."},
    {"speaker": "Agent", "text": "Thanks. Quick check shows the line’s up on our side. Power-cycle the ONT for ten seconds, then the router."},
    {"speaker": "Customer", "text": "Done… oh, it’s back."},
    {"speaker": "Agent", "text": "Perfect. You’re on the internet you never think about again. Anything else?"}
]

SMS_TEXT = "Oxygn: Hi Greg, your fibre line is now stable (Ref: OX-2026-1018-4821). Splitter issue cleared. Track status anytime at https://portal.oxygn.co.za – Jarvis"

def duration():
    if not state["start_time"]:
        return "00:00"
    elapsed = int((datetime.utcnow() - state["start_time"]).total_seconds())
    m, s = divmod(elapsed, 60)
    return f"{m:02d}:{s:02d}"

@app.route("/api/status")
def api_status():
    return jsonify({
        "status": state["status"],
        "customer": state["customer"],
        "account": state["account"],
        "duration": duration(),
        "current_line": state["current_line"],
        "transcript": state["transcript"],
        "sms_sent": state["sms_sent"],
        "sms_text": SMS_TEXT if state["sms_sent"] else None
    })

@app.route("/api/next", methods=["POST"])
def api_next():
    if state["current_line"] >= len(LINES):
        state["status"] = "Resolved"
        return jsonify({"done": True, "status": state["status"]})
    if state["current_line"] == 0:
        state["start_time"] = datetime.utcnow()
    line = LINES[state["current_line"]]
    state["transcript"].append(line)
    state["current_line"] += 1
    if state["current_line"] >= len(LINES):
        state["status"] = "Resolved"
    return jsonify({"line": line, "status": state["status"], "duration": duration()})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    state["status"] = "Active"
    state["current_line"] = 0
    state["start_time"] = None
    state["transcript"] = []
    state["sms_sent"] = False
    return jsonify({"ok": True})

@app.route("/api/sms", methods=["POST"])
def api_sms():
    state["sms_sent"] = not state["sms_sent"]
    return jsonify({"sms_sent": state["sms_sent"], "sms_text": SMS_TEXT if state["sms_sent"] else None})

@app.route("/api/voice", methods=["POST"])
def api_voice():
    cmd = (request.json or {}).get("command", "").lower()
    if "next" in cmd or "advance" in cmd:
        return api_next()
    if "reset" in cmd or "clear" in cmd:
        return api_reset()
    if "sms" in cmd or "message" in cmd:
        return api_sms()
    return jsonify({"error": "unknown command", "heard": cmd})

@app.route("/")
def index():
    return jsonify({"service": "Jarvis Flask Backend", "status": "online"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
