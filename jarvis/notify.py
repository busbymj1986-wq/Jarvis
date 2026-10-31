"""Deliver updates to the owner: WhatsApp, SMS and/or email to themselves.

NOTIFY_CHANNELS=whatsapp,email   (any of whatsapp, sms, email)
OWNER_PHONE=+2782...             (SMS and WhatsApp)
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886   (your Twilio WhatsApp sender)
"""
import os

from . import mail, store


def channels():
    raw = os.environ.get("NOTIFY_CHANNELS")
    if raw:
        return [c.strip().lower() for c in raw.split(",") if c.strip()]
    return ["email"] if mail.configured() else ["sms"]


def _twilio():
    sid, token = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        raise RuntimeError("Twilio is not configured")
    from twilio.rest import Client
    return Client(sid, token)


def send_owner(subject, text):
    """Send to every configured channel. Returns {channel: "sent" | error}."""
    results = {}
    phone = os.environ.get("OWNER_PHONE")
    for ch in channels():
        try:
            if ch == "email":
                mail.send_to_self(f"Jarvis: {subject}", text)
            elif ch == "sms":
                if not phone:
                    raise RuntimeError("OWNER_PHONE not set")
                _twilio().messages.create(to=phone, from_=os.environ.get("TWILIO_FROM_NUMBER"),
                                          body=f"{subject}\n{text}"[:1500])
            elif ch == "whatsapp":
                wa_from = os.environ.get("TWILIO_WHATSAPP_FROM")
                if not (phone and wa_from):
                    raise RuntimeError("OWNER_PHONE and TWILIO_WHATSAPP_FROM must be set")
                _twilio().messages.create(to=f"whatsapp:{phone}", from_=wa_from, body=f"*{subject}*\n{text}"[:1500])
            else:
                raise RuntimeError(f"unknown channel {ch}")
            results[ch] = "sent"
        except Exception as e:  # one channel failing must not stop the others
            results[ch] = f"failed: {e}"
    store.log_activity("update", f"{subject} → " + ", ".join(f"{k}: {v}" for k, v in results.items()))
    return results
