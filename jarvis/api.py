"""REST API for managing staff and jobs, and for talking to the Claude/Grok agents.

All routes need `Authorization: Bearer $JARVIS_API_KEY` (or `X-API-Key`).
"""
import hmac
import json
import os

from flask import Blueprint, jsonify, request

from . import ai, mail, store, tools

bp = Blueprint("manage", __name__, url_prefix="/api")


def authorized():
    key = os.environ.get("JARVIS_API_KEY")
    if not key:
        return False
    given = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(given, key)


@bp.before_request
def _auth():
    if not os.environ.get("JARVIS_API_KEY"):
        return jsonify({"error": "Set JARVIS_API_KEY on the server to enable the management API"}), 503
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401


def _body():
    return request.get_json(silent=True) or {}


def _tool(name, args):
    out, is_error = tools.execute_tool(name, args)
    data = json.loads(out)
    if is_error:
        return jsonify(data), 400
    if isinstance(data, dict) and data.get("clash"):
        return jsonify(data), 409
    return jsonify(data)


# ---- staff ----

@bp.get("/staff")
def staff_list():
    return jsonify(store.list_staff(include_inactive=request.args.get("all") == "1"))


@bp.post("/staff")
def staff_add():
    return _tool("add_staff", _body())


@bp.patch("/staff/<int:staff_id>")
def staff_update(staff_id):
    return _tool("update_staff", {**_body(), "staff": str(staff_id)})


@bp.delete("/staff/<int:staff_id>")
def staff_remove(staff_id):
    return _tool("update_staff", {"staff": str(staff_id), "active": False})


# ---- jobs ----

@bp.get("/jobs")
def jobs_list():
    args = {k: request.args[k] for k in ("day", "staff") if request.args.get(k)}
    return _tool("list_jobs", args)


@bp.post("/jobs")
def jobs_add():
    return _tool("create_job", _body())


@bp.patch("/jobs/<int:job_id>")
def jobs_update(job_id):
    """Body may contain any of: staff, start+end, status(+notes), force."""
    b = _body()
    steps = []
    if b.get("staff"):
        steps.append(("assign_job", {"job_id": job_id, "staff": b["staff"], "force": b.get("force", False)}))
    if b.get("start") and b.get("end"):
        steps.append(("reschedule_job", {"job_id": job_id, "start": b["start"], "end": b["end"],
                                         "force": b.get("force", False)}))
    if b.get("status"):
        steps.append(("update_job_status", {"job_id": job_id, "status": b["status"], "notes": b.get("notes")}))
    for name, args in steps:
        resp = _tool(name, args)
        if isinstance(resp, tuple):  # error or clash: stop here
            return resp
    job = store.get_job(job_id)
    return (jsonify(job), 200) if job else (jsonify({"error": "not found"}), 404)


@bp.get("/day-sheet")
def day_sheet():
    return _tool("day_sheet", {"day": request.args.get("day", "today")})


@bp.put("/settings/whatsapp-group")
def whatsapp_group():
    link = _body().get("link", "")
    store.set_setting("whatsapp_group_link", link)
    return jsonify({"ok": True, "link": link})


# ---- AI ----

@bp.post("/agent")
def agent():
    """Body: {"message": "...", "provider": "claude"|"grok", "history": [{"role","content"}...]}"""
    b = _body()
    if not b.get("message"):
        return jsonify({"error": "message is required"}), 400
    # "send draft 3" is handled here in code so an email can never trick the model into sending.
    is_send, draft_id = mail.parse_send_command(b["message"])
    if is_send:
        if draft_id is None:
            return jsonify({"reply": "Which draft? Say 'send draft' and the number.", "actions": []})
        return _send_draft(draft_id)
    history = [{"role": h["role"], "content": str(h["content"])} for h in b.get("history", [])
               if h.get("role") in ("user", "assistant")]
    try:
        return jsonify(ai.run_agent(b["message"], provider=b.get("provider"), history=history))
    except ai.AgentError as e:
        return jsonify({"error": str(e)}), 502


@bp.post("/desk-note")
def desk_note():
    b = _body()
    try:
        return jsonify(ai.desk_note(b.get("day", "today"), b.get("provider", "grok")))
    except ai.AgentError as e:
        return jsonify({"error": str(e)}), 502


# ---- SIP / voice ----

@bp.get("/sip")
def sip_info():
    domain = os.environ.get("SIP_DOMAIN", "")
    base = os.environ.get("PUBLIC_BASE_URL", request.host_url.rstrip("/"))
    return jsonify({
        "sip_domain": domain,
        "voice_webhook": f"{base}/voice/incoming",
        "status_callback": f"{base}/voice/status",
        "voice_provider": os.environ.get("VOICE_PROVIDER", ai.default_provider()),
        "signature_validation": bool(os.environ.get("TWILIO_AUTH_TOKEN")),
        "extensions": [{"name": s["name"], "ext": s["sip_ext"],
                        "uri": f"sip:{s['sip_ext']}@{domain}" if domain else None}
                       for s in store.list_staff() if s.get("sip_ext")],
    })


@bp.get("/calls")
def calls():
    return jsonify(store.recent_calls(int(request.args.get("limit", 50))))


# ---- email ----

def _mail_call(fn, *args):
    try:
        return jsonify(fn(*args))
    except mail.MailError as e:
        return jsonify({"error": str(e)}), 400


@bp.get("/email/inbox")
def email_inbox():
    return _mail_call(mail.list_messages, request.args.get("all") != "1",
                      int(request.args.get("limit", 10)), request.args.get("q"))


@bp.get("/email/message/<path:msg_id>")
def email_read(msg_id):
    return _mail_call(mail.read_message, msg_id)


@bp.get("/email/drafts")
def email_drafts():
    return jsonify(store.list_drafts(request.args.get("status", "draft")))


@bp.post("/email/drafts")
def email_draft_create():
    return _tool("draft_email", _body())


def _send_draft(draft_id):
    try:
        d = mail.send_draft(draft_id)
    except mail.MailError as e:
        return jsonify({"reply": f"Couldn't send draft {draft_id}: {e}", "error": str(e)}), 400
    return jsonify({"reply": f"Sent draft {d['id']} to {d['to_addr']}.", "draft": d, "actions": []})


@bp.post("/email/drafts/<int:draft_id>/send")
def email_send(draft_id):
    return _send_draft(draft_id)


@bp.delete("/email/drafts/<int:draft_id>")
def email_discard(draft_id):
    if not store.get_draft(draft_id):
        return jsonify({"error": "not found"}), 404
    return jsonify(store.mark_draft(draft_id, "discarded"))
