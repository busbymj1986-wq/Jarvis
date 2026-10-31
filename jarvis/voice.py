"""SIP / phone voice line for Jarvis (Twilio Programmable Voice webhooks).

Calls arrive from a phone number or a SIP endpoint (desk phone, softphone, PBX
trunk) registered on a Twilio SIP Domain. Twilio does speech-to-text, we run
the Claude/Grok agent, and reply with TwiML <Say>. Staff are recognised by
caller ID (phone) or by their SIP extension. See docs/SIP.md.
"""
import os
import re
from collections import OrderedDict
from xml.sax.saxutils import escape, quoteattr

from flask import Blueprint, Response, abort, request

from . import ai, mail, store

bp = Blueprint("voice", __name__, url_prefix="/voice")

VOICE = os.environ.get("TWILIO_VOICE", "Polly.Ayanda-Neural")
LANG = os.environ.get("VOICE_LANGUAGE", "en-ZA")
_histories = OrderedDict()   # CallSid -> [{"role":..., "content":...}]
_last_draft = {}             # CallSid -> id of the last email drafted on this call
_MAX_CALLS = 200
HANGUP_WORDS = ("goodbye", "bye", "that's all", "hang up", "end call")
TRANSFER_RE = re.compile(r"\b(?:transfer|put me through|connect me)\s+(?:me\s+)?(?:to\s+)?(.+)", re.I)


def sip_domain():
    return os.environ.get("SIP_DOMAIN", "")


def _twiml(body):
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', mimetype="text/xml")


def _say(text):
    return f"<Say voice={quoteattr(VOICE)} language={quoteattr(LANG)}>{escape(text)}</Say>"


def _gather(prompt=""):
    inner = _say(prompt) if prompt else ""
    return (f'<Gather input="speech" action="/voice/gather" method="POST" language={quoteattr(LANG)} '
            f'speechTimeout="auto" actionOnEmptyResult="true">{inner}</Gather>')


def _validate():
    """Reject requests not signed by Twilio when TWILIO_AUTH_TOKEN is set."""
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not token or os.environ.get("TWILIO_SKIP_VALIDATION") == "1":
        return
    from twilio.request_validator import RequestValidator
    base = os.environ.get("PUBLIC_BASE_URL")
    if base:
        url = base.rstrip("/") + request.full_path.rstrip("?")
    else:
        url = request.url.replace("http://", request.headers.get("X-Forwarded-Proto", "http") + "://", 1)
    if not RequestValidator(token).validate(url, request.form, request.headers.get("X-Twilio-Signature", "")):
        abort(403)


def identify(from_value):
    """Map a caller to a staff member: SIP URI user part = extension, else phone number."""
    if not from_value:
        return None
    m = re.match(r"sips?:([^@;]+)@", from_value)
    if m:
        user = m.group(1)
        for s in store.list_staff():
            if s.get("sip_ext") and s["sip_ext"] == user:
                return s
        return store.staff_by_phone(user)
    return store.staff_by_phone(from_value)


def _history(call_sid):
    h = _histories.setdefault(call_sid, [])
    _histories.move_to_end(call_sid)
    while len(_histories) > _MAX_CALLS:
        _histories.popitem(last=False)
    return h


def _dial_staff(staff):
    if staff.get("sip_ext") and sip_domain():
        target = f"<Sip>sip:{escape(staff['sip_ext'])}@{escape(sip_domain())}</Sip>"
    elif staff.get("phone"):
        target = f"<Number>{escape(staff['phone'])}</Number>"
    else:
        return None
    caller_id = os.environ.get("TWILIO_FROM_NUMBER")
    attr = f" callerId={quoteattr(caller_id)}" if caller_id else ""
    return f"{_say('Putting you through to ' + staff['name'] + '.')}<Dial{attr} timeout=\"25\">{target}</Dial>" + \
        _say("They didn't answer.") + _gather("Anything else?")


@bp.route("/incoming", methods=["POST"])
def incoming():
    _validate()
    caller = identify(request.form.get("From"))
    if not caller and os.environ.get("ALLOW_UNKNOWN_CALLERS") != "1":
        return _twiml(_say("Sorry, this line is for registered staff only. Goodbye.") + "<Hangup/>")
    name = caller["name"].split()[0] if caller else "there"
    _history(request.form.get("CallSid", "")).clear()
    return _twiml(_gather(f"Hi {name}, Jarvis here. How can I help?") + _say("I didn't catch that. Goodbye."))


@bp.route("/gather", methods=["POST"])
def gather():
    _validate()
    call_sid = request.form.get("CallSid", "")
    caller = identify(request.form.get("From"))
    if not caller and os.environ.get("ALLOW_UNKNOWN_CALLERS") != "1":
        return _twiml("<Hangup/>")
    heard = (request.form.get("SpeechResult") or "").strip()
    if not heard:
        return _twiml(_gather("Sorry, I didn't catch that. Say it again?") + "<Hangup/>")
    low = heard.lower().rstrip(".!?")
    if any(low == w or low.endswith(w) for w in HANGUP_WORDS):
        _histories.pop(call_sid, None)
        return _twiml(_say("Cheers, goodbye.") + "<Hangup/>")

    # Sending email is decided here in code, never by the model: only a manager saying
    # "send it" (last draft on this call) or "send draft 3" sends anything.
    is_send, draft_id = mail.parse_send_command(heard)
    if is_send:
        if not (caller and caller.get("is_manager")):
            reply = "Only a manager can send emails."
        elif (draft_id or _last_draft.get(call_sid)) is None:
            reply = "There's no draft to send yet. Which draft number?"
        else:
            draft_id = draft_id or _last_draft.get(call_sid)
            try:
                d = mail.send_draft(draft_id)
                reply = f"Sent to {d['to_addr'].split('<')[0].strip() or d['to_addr']}."
            except mail.MailError as e:
                reply = f"I couldn't send it. {e}"
        store.log_call(call_sid, request.form.get("From"), heard, reply, "email")
        return _twiml(_gather(reply + " Anything else?") + "<Hangup/>")

    m = TRANSFER_RE.match(low)
    if m:
        target = store.find_staff(m.group(1).strip(" ."))
        if target:
            twiml = _dial_staff(target)
            if twiml:
                store.log_call(call_sid, request.form.get("From"), heard, f"transfer -> {target['name']}", "sip")
                return _twiml(twiml)

    # Treat an unknown caller (only when ALLOW_UNKNOWN_CALLERS=1) as read-only crew.
    as_caller = caller or {"id": -1, "name": "Unknown caller", "is_manager": 0}
    history = _history(call_sid)
    provider = os.environ.get("VOICE_PROVIDER", ai.default_provider())
    try:
        result = ai.run_agent(heard, provider=provider, history=history, caller=as_caller, voice=True)
        reply = result["reply"] or "Done."
        for act in result.get("actions", []):
            if act["tool"] == "draft_email" and act["result"].get("draft_id"):
                _last_draft[call_sid] = act["result"]["draft_id"]
    except ai.AgentError:
        reply = "Sorry, my brain is offline right now. Please try again in a minute."
    history += [{"role": "user", "content": heard}, {"role": "assistant", "content": reply}]
    store.log_call(call_sid, request.form.get("From"), heard, reply, provider)
    return _twiml(_gather(reply) + _say("Goodbye.") + "<Hangup/>")


@bp.route("/status", methods=["POST"])
def status():
    _validate()
    if request.form.get("CallStatus") in ("completed", "failed", "busy", "no-answer", "canceled"):
        _histories.pop(request.form.get("CallSid", ""), None)
        _last_draft.pop(request.form.get("CallSid", ""), None)
    return ("", 204)
