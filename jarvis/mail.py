"""Email for the PA: read the inbox and send approved drafts.

Two backends, chosen with MAIL_BACKEND:
  graph - Microsoft 365 / Outlook through Microsoft Graph (app registration, client credentials)
  imap  - any IMAP/SMTP mailbox: Gmail (app password), Outlook.com, cPanel and other hosts

Reading never marks messages as read. Sending is NOT a tool the AI can call; it only
happens through `send_draft`, which the API or voice line calls after a human says so.
"""
import email
import imaplib
import os
import re
import smtplib
import time
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, make_msgid, parseaddr

import requests

from . import store

MAX_BODY = 6000


class MailError(Exception):
    pass


def backend():
    b = os.environ.get("MAIL_BACKEND", "").lower()
    if b in ("graph", "imap"):
        return b
    raise MailError("Email is not configured (set MAIL_BACKEND=graph or imap).")


def configured():
    try:
        backend()
        return True
    except MailError:
        return False


def _clean(text):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# ---------------- Microsoft Graph ----------------

_token = {"value": None, "exp": 0}
GRAPH = "https://graph.microsoft.com/v1.0"


def _graph_token():
    if _token["value"] and _token["exp"] > time.time() + 60:
        return _token["value"]
    tenant, cid, secret = (os.environ.get(k) for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"))
    if not (tenant and cid and secret):
        raise MailError("Set MS_TENANT_ID, MS_CLIENT_ID and MS_CLIENT_SECRET for Outlook.")
    r = requests.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", timeout=20, data={
        "client_id": cid, "client_secret": secret, "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default"})
    if r.status_code != 200:
        raise MailError(f"Microsoft login failed ({r.status_code})")
    d = r.json()
    _token.update(value=d["access_token"], exp=time.time() + int(d.get("expires_in", 3600)))
    return _token["value"]


def _graph(method, path, **kw):
    mailbox = os.environ.get("MS_MAILBOX")
    if not mailbox:
        raise MailError("Set MS_MAILBOX to the mailbox address Jarvis manages.")
    r = requests.request(method, f"{GRAPH}/users/{mailbox}{path}", timeout=30,
                         headers={"Authorization": f"Bearer {_graph_token()}"}, **kw)
    if r.status_code >= 400:
        raise MailError(f"Outlook error {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


def _graph_summary(m):
    sender = (m.get("from") or {}).get("emailAddress", {})
    return {"id": m["id"], "from": f"{sender.get('name', '')} <{sender.get('address', '')}>".strip(),
            "subject": m.get("subject") or "", "received": m.get("receivedDateTime"),
            "unread": not m.get("isRead", True), "snippet": (m.get("bodyPreview") or "")[:200]}


def _graph_list(unread_only, limit, query):
    params = {"$top": limit, "$select": "id,from,subject,receivedDateTime,isRead,bodyPreview"}
    if query:
        params["$search"] = f'"{query}"'
    else:
        params["$orderby"] = "receivedDateTime desc"
        if unread_only:
            # Graph needs the $orderby property in $filter too, or it rejects the query.
            params["$filter"] = "receivedDateTime ge 2000-01-01T00:00:00Z and isRead eq false"
    return [_graph_summary(m) for m in _graph("GET", "/mailFolders/inbox/messages", params=params).get("value", [])]


def _graph_read(msg_id):
    m = _graph("GET", f"/messages/{msg_id}", params={
        "$select": "id,from,toRecipients,ccRecipients,subject,receivedDateTime,isRead,body,bodyPreview"})
    out = _graph_summary(m)
    out["to"] = ", ".join(r["emailAddress"]["address"] for r in m.get("toRecipients", []))
    out["cc"] = ", ".join(r["emailAddress"]["address"] for r in m.get("ccRecipients", []))
    out["body"] = _clean(m.get("body", {}).get("content", ""))[:MAX_BODY]
    return out


def _graph_send(d):
    if d.get("reply_to_id"):
        _graph("POST", f"/messages/{d['reply_to_id']}/reply", json={"comment": d["body"]})
        return
    rcpt = lambda s: [{"emailAddress": {"address": a}} for _, a in getaddresses([s or ""]) if a]
    _graph("POST", "/sendMail", json={"message": {
        "subject": d["subject"], "body": {"contentType": "Text", "content": d["body"]},
        "toRecipients": rcpt(d["to_addr"]), "ccRecipients": rcpt(d.get("cc"))}, "saveToSentItems": True})


# ---------------- IMAP / SMTP ----------------

def _imap():
    host, user, pw = (os.environ.get(k) for k in ("IMAP_HOST", "MAIL_USER", "MAIL_PASSWORD"))
    if not (host and user and pw):
        raise MailError("Set IMAP_HOST, MAIL_USER and MAIL_PASSWORD.")
    try:
        m = imaplib.IMAP4_SSL(host, int(os.environ.get("IMAP_PORT", 993)))
        m.login(user, pw)
        m.select("INBOX", readonly=True)  # readonly: never flips messages to read
    except (imaplib.IMAP4.error, OSError) as e:
        raise MailError(f"Could not open mailbox: {e}") from e
    return m


def _text_of(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if not part:
        return ""
    content = part.get_content()
    return _clean(content) if part.get_content_type() == "text/html" else content.strip()


def _imap_fetch(m, uid, full):
    typ, data = m.uid("FETCH", uid, "(FLAGS BODY.PEEK[])" if full else "(FLAGS BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.600>)")
    if typ != "OK" or not data or data[0] is None:
        raise MailError(f"Email {uid} not found.")
    raw = b"".join(p[1] for p in data if isinstance(p, tuple))
    flags = b" ".join(p[0] if isinstance(p, tuple) else p for p in data)
    msg = email.message_from_bytes(raw, policy=default_policy)
    out = {"id": uid if isinstance(uid, str) else uid.decode(), "from": str(msg.get("From", "")),
           "subject": str(msg.get("Subject", "")), "received": str(msg.get("Date", "")),
           "unread": b"\\Seen" not in flags}
    if full:
        out.update(to=str(msg.get("To", "")), cc=str(msg.get("Cc", "")), message_id=str(msg.get("Message-ID", "")),
                   body=_text_of(msg)[:MAX_BODY])
    else:
        try:
            out["snippet"] = _text_of(msg)[:200]
        except Exception:
            out["snippet"] = ""
    return out


def _imap_list(unread_only, limit, query):
    m = _imap()
    try:
        crit = ["UNSEEN"] if unread_only else ["ALL"]
        if query:
            crit = ["TEXT", f'"{query.replace(chr(34), "")}"'] + ([] if not unread_only else ["UNSEEN"])
        typ, data = m.uid("SEARCH", None, *crit)
        uids = (data[0] or b"").split()[-limit:][::-1]
        return [_imap_fetch(m, u, full=False) for u in uids]
    finally:
        m.logout()


def _imap_read(uid):
    m = _imap()
    try:
        return _imap_fetch(m, str(uid), full=True)
    finally:
        m.logout()


def _smtp_send(d):
    user, pw = os.environ.get("MAIL_USER"), os.environ.get("MAIL_PASSWORD")
    host = os.environ.get("SMTP_HOST")
    if not (host and user and pw):
        raise MailError("Set SMTP_HOST, MAIL_USER and MAIL_PASSWORD.")
    msg = EmailMessage()
    msg["From"] = os.environ.get("MAIL_FROM", user)
    msg["To"] = d["to_addr"]
    if d.get("cc"):
        msg["Cc"] = d["cc"]
    msg["Subject"] = d["subject"]
    msg["Message-ID"] = make_msgid()
    if d.get("reply_to_id"):
        orig = _imap_read(d["reply_to_id"]).get("message_id")
        if orig:
            msg["In-Reply-To"] = msg["References"] = orig
    msg.set_content(d["body"])
    port = int(os.environ.get("SMTP_PORT", 465))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, pw)
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Send failed: {e}") from e


# ---------------- public API ----------------

def list_messages(unread_only=True, limit=10, query=None):
    limit = max(1, min(int(limit or 10), 25))
    return (_graph_list if backend() == "graph" else _imap_list)(unread_only, limit, query)


def read_message(msg_id):
    return (_graph_read if backend() == "graph" else _imap_read)(msg_id)


def send_draft(draft_id):
    """Send a stored draft. Only call this from a human-confirmed path."""
    d = store.get_draft(int(draft_id))
    if not d:
        raise MailError(f"Draft {draft_id} not found.")
    if d["status"] == "sent":
        raise MailError(f"Draft {draft_id} was already sent.")
    if not parseaddr(d["to_addr"])[1] and not d.get("reply_to_id"):
        raise MailError("Draft has no valid recipient.")
    try:
        (_graph_send if backend() == "graph" else _smtp_send)(d)
    except MailError as e:
        store.mark_draft(d["id"], "failed", str(e))
        raise
    return store.mark_draft(d["id"], "sent")


SEND_RE = re.compile(r"^\s*(?:yes[, ]*)?(?:please\s+)?send\s+(?:it|that|the email|draft\s*#?\s*(\d+))\s*(?:now|please)?[.!]?\s*$", re.I)


def parse_send_command(text):
    """Return (True, draft_id_or_None) if the human literally said 'send it' / 'send draft 3'."""
    m = SEND_RE.match(text or "")
    if not m:
        return False, None
    return True, int(m.group(1)) if m.group(1) else None
