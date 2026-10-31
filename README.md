# Jarvis: my personal planning system

A PA that runs the civils crews, watches my email and Outlook calendar, and keeps me up to date, through Claude, WhatsApp/SMS/email and a SIP voice line.

A Flask backend that holds the crew and jobs data and exposes it three ways:

- **REST API** (`/api/*`) for the phone app or a dashboard.
- **AI agent** (`POST /api/agent`): plain-English requests handled by Claude (`claude-opus-5-5`) using the staff tools.
- **Voice / SIP** (`/voice/*`): staff call in from a desk phone, softphone, PBX or mobile and talk to Jarvis. See [docs/SIP.md](docs/SIP.md).
- **Email** (Outlook / Microsoft 365, or Gmail and other IMAP hosts): check and summarise the inbox, turn emails into jobs, draft replies. Jarvis only drafts; you send. See [docs/EMAIL.md](docs/EMAIL.md).
- **Keeps me updated**: a morning brief, checks through the day that only send when something changed, and an evening wrap-up, by WhatsApp, SMS or email. See [docs/UPDATES.md](docs/UPDATES.md).
- **Outlook calendar**: jobs sync both ways with your calendar, and Jarvis reads your meetings and books your time.

## Layout

| File | Role |
|---|---|
| `app.py` | Flask app. Keeps the original call-demo routes and mounts the API and voice blueprints |
| `jarvis/store.py` | SQLite store: staff, jobs, settings (WhatsApp group link), call log, clash detection |
| `jarvis/tools.py` | Staff tools, defined once and with manager/crew permissions |
| `jarvis/ai.py` | Claude tool-use loop, the desk note and email triage |
| `jarvis/api.py` | REST endpoints (Bearer `JARVIS_API_KEY`) |
| `jarvis/mail.py` | Inbox reading (Graph or IMAP) and sending approved drafts (Graph or SMTP) |
| `jarvis/calendar_sync.py` | Outlook calendar: jobs to events, pull moves back, your meetings |
| `jarvis/updates.py`, `jarvis/notify.py` | Morning brief, change checks, evening wrap, scheduler; delivery by WhatsApp/SMS/email |
| `jarvis/voice.py` | Twilio voice webhooks: caller ID/SIP extension lookup, speech to agent, call transfer |
| `tests/` | `python -m pytest -q` |

## Run

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in keys, then export them
python app.py
```

## Examples

```bash
H='Authorization: Bearer '$JARVIS_API_KEY
curl -XPOST localhost:5000/api/staff -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Sipho Dlamini","phone":"+27822220000","sip_ext":"201"}'
curl -XPOST localhost:5000/api/agent -H "$H" -H 'Content-Type: application/json' \
  -d '{"message":"Book Sipho for a fibre install at 12 Main Rd tomorrow 8-10"}'
curl -XPOST localhost:5000/api/agent -H "$H" -H 'Content-Type: application/json' \
  -d '{"message":"Who is free Thursday afternoon?"}'
curl -XPOST localhost:5000/api/desk-note -H "$H" -H 'Content-Type: application/json' -d '{}'
curl "localhost:5000/api/day-sheet?day=today" -H "$H"   # WhatsApp group text
```

| Endpoint | Purpose |
|---|---|
| `GET/POST /api/staff`, `PATCH/DELETE /api/staff/<id>` | Roster (DELETE deactivates) |
| `GET/POST /api/jobs`, `PATCH /api/jobs/<id>` | Jobs. Returns 409 on a clash unless `"force": true` |
| `GET /api/day-sheet`, `PUT /api/settings/whatsapp-group` | WhatsApp day message and group link |
| `POST /api/agent` | `{message, history}` sent to Claude |
| `POST /api/desk-note` | Daily manager briefing |
| `GET /api/sip`, `GET /api/calls` | SIP config (extensions, webhooks) and voice call log |
| `/api/email/...` | Inbox, read, drafts, send (see docs/EMAIL.md) |
| `/api/calendar...` | Your meetings, book time, two-way sync |
| `POST /api/updates/run`, `GET /api/activity` | Run a brief/check/wrap now; everything that changed |
