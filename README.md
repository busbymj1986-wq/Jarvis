# Jarvis

Flask backend for Jarvis. Besides the original support-call demo endpoints
(`/api/status`, `/api/next`, `/api/reset`, `/api/sms`, `/api/voice`), it now
includes a **VIGI NVR bridge**: a bolt-on that a TP-Link VIGI NVR reports to,
which keeps snapshots and short clips, decides when an event is worth an alarm,
and alerts you. **Everything works through WhatsApp:** alarms arrive with the
photo and then the clip, and you reply to arm, disarm, ask for a live photo or
clip, or get a report. Telegram, email and a webhook are also available.

## VIGI bridge: how it works

```
VIGI NVR --(Alarm Server push: event JSON + snapshot)--> POST /vigi/alarm/<token>
                                                              |
   1. store event + snapshot (reply 200 to the NVR immediately)
   2. rules: armed? which camera? which event type? escalation?   -> logged / ignored
   3. cooldown: already alerted for this camera recently?         -> grouped
   4. AI check (optional): Claude looks at the snapshot           -> dismissed
   5. ALARM: WhatsApp (and/or Telegram / email / webhook) with the snapshot
   6. clip: pull 5 s before -> 10 s after from the NVR over RTSP, send it as a follow-up
                                                              |
                         reports: /vigi/report, /vigi/events.csv, daily summary at 07:00

You on WhatsApp --"disarm" / "snap 2" / "report"--> POST /vigi/whatsapp/webhook --> reply
```

Every event ends in one of these states, and the reason is stored with it:

| Status      | Meaning |
|-------------|---------|
| `alarm`     | Alert sent |
| `grouped`   | Would alarm, but this camera already alerted within `cooldown_seconds` |
| `dismissed` | Rules said alarm, AI check found nothing there (shadow, animal, headlights...) |
| `logged`    | Recorded only: system disarmed, outside the armed schedule, or a log-only type like motion |
| `ignored`   | Camera disabled in the rules |
| `error`     | Processing crashed (check the server log); shown in reports so it can't hide a missed alarm |

### When does it alarm?

1. **Armed state**: `rules.mode` is `armed`, `disarmed` or `schedule` (default:
   armed 18:00-06:00 every night). Change it at runtime with `POST /vigi/arm`.
   A camera with `"always_armed": true` is armed 24/7.
2. **Event type**: while armed, these raise an alarm by default: `intrusion`,
   `line_crossing`, `region_entering`, `person`, `alarm_input`, `tampering`.
   You can narrow it per camera, e.g. `"alarm_events": ["line_crossing_vehicle"]`.
3. **Escalation**: plain `motion` is only logged, unless 3+ armed events hit 2+
   cameras within 60 s. That pattern looks like someone moving through the property.
4. **Cooldown**: one alert per camera per 120 s, so a person walking around
   doesn't trigger 20 messages.
5. **AI check** (when `ANTHROPIC_API_KEY` is set): Claude looks at the snapshot.
   In `verify` mode the alarm only goes out if a person (or an out-of-place
   vehicle) is visible, or the image is too unclear to tell. Every alert gets a
   one-line description ("One person in dark clothing climbing the front gate").
   Wired sensor inputs and tampering skip the check. If the AI call fails, the
   alarm still goes out (`on_error: "alarm"`).

## WhatsApp

### What you get

An alarm arrives as the snapshot with a caption, for example "🚨 ALARM - Front
Gate / Intrusion at Fri 30 Oct 23:00:05 / One person climbing the gate", and the
video clip follows about 30 seconds later. Each number listed in `WHATSAPP_TO`
gets it. You can also send these to the bot:

| Send | Reply |
|---|---|
| `status` | Armed or not, today's events and alarms |
| `arm` / `disarm` / `auto` | Arm, disarm, or go back to the schedule |
| `snap 2` | Live photo from camera 2 |
| `clip 2` (or `clip 2 20`) | 10 s (up to 30 s) live clip from camera 2 |
| `alarms` | The last 5 alarms with their event numbers |
| `event 12` | Photo, details and clip of event 12 |
| `report` / `report 7d` | Summary for the last 24 hours / week |
| anything else | The command list |

Only numbers in `WHATSAPP_TO` are obeyed. Messages from anyone else are
ignored and logged, and every incoming request must carry Meta's signature.

### The 24-hour rule (why templates are needed)

WhatsApp only lets a business send ordinary messages to someone who has
messaged it in the last 24 hours. The bridge keeps track of that for each
recipient:

- **Inside the window** (you've messaged the bot today): alarms, clips and
  reports arrive as normal messages.
- **Outside the window**: the bridge sends **templates**, message formats Meta
  approved in advance. Without an approved `alarm_template`, an alarm only
  reaches people who messaged the bot that day. The bridge logs a warning about
  this at startup.

Create these three templates in WhatsApp Manager (*Message templates > Create*,
category **Utility**, language **English**). Meta needs sample values for each
`{{n}}`, and won't accept a body that starts or ends with a variable, so keep
the wording around them.

| Name (env var) | Header | Body |
|---|---|---|
| `security_alarm` (`WHATSAPP_ALARM_TEMPLATE`) | Image | `🚨 Security alarm at {{1}}: {{2}}. Details: {{3}}. Reply HELP for commands.` |
| `security_clip` (`WHATSAPP_CLIP_TEMPLATE`) | Video | `🎥 Video from {{1}}: {{2}}. Reply HELP for commands.` |
| `security_report` (`WHATSAPP_REPORT_TEMPLATE`) | none | `📋 {{1}}: {{2}} Reply REPORT for the full report.` |

The bridge fills in {{1}} camera, {{2}} event and time, and {{3}} the AI
description or the rule that fired. For the report, {{1}} is the period and
{{2}} the one-line totals. If your templates use another language code, set
`notify.whatsapp.language`, e.g. `en_US`.

**Cost:** replies inside the 24-hour window are free. Meta charges per template
message at its utility rate, which depends on the recipient's country, so
check Meta's current WhatsApp pricing.

### Setting up the WhatsApp side (Meta)

1. At developers.facebook.com, create an app (type *Business*) and add the
   **WhatsApp** product. Add the business phone number the bot will use. It
   must not already be registered on the WhatsApp app. Meta's free test number
   works for trying it out, but then each recipient must be added to its
   allowed list.
2. From *WhatsApp > API Setup*, copy the **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID`.
3. Make a **permanent token**. The one on the API Setup page expires within a
   day. Go to *Business settings > Users > System users*, add a system user,
   assign it your app and WhatsApp account, and generate a token with
   `whatsapp_business_messaging` and `whatsapp_business_management`. Put it
   in `WHATSAPP_TOKEN`.
4. Copy the **App secret** from *App settings > Basic* into `WHATSAPP_APP_SECRET`.
5. For commands, set the webhook under *WhatsApp > Configuration*:
   - Callback URL: `https://<your bridge>/vigi/whatsapp/webhook`
   - Verify token: any phrase you choose, also put in `WHATSAPP_VERIFY_TOKEN`
   - Then subscribe to the **messages** field.

   Meta has to reach this URL over HTTPS. If the bridge runs on your home or
   office network, publish just that path through a tunnel such as Cloudflare
   Tunnel. Alerts going out work without the webhook (via templates), but
   commands need it.
6. Create the templates above and wait for approval.
7. Each person in `WHATSAPP_TO` sends **help** to the bot once to check it answers.

Clips are re-encoded to H.264 baseline, at most 1280 px wide, whenever WhatsApp
is set up. WhatsApp won't play the H.265 or High-profile streams most NVRs
record, and video must stay under 16 MB.

## Setting up the NVR

You need NVR firmware that has **Settings > Event > Alarm Server**.

1. **Add the alarm server:** go to *Settings > Event > Alarm Server* and click **+ Add**.
   - Protocol / host / port: where this bridge runs (for example `192.168.1.10`, port `8080`).
   - URL: `/vigi/alarm/<your VIGI_INGEST_TOKEN>`.
   - Click **Test**. The bridge replies 200, and you will see the request in its log.
   - Turn on *Enhanced Alarm Message* if your firmware has it. The bridge reads both formats.
2. **Point the detections at it:** for each detection on each camera (Intrusion,
   Line Crossing, Motion, People/Vehicle detection, Alarm In...), open its
   processing mode and tick **Send to Alarm Server**. Also tick the snapshot
   option if there is one.
3. **Leave the NVR armed.** The NVR doesn't push events while it is disarmed,
   so keep it armed and let the bridge's schedule decide when to alarm.
4. **For clips:** the RTSP server is on by default (port 554). Don't enable
   SRTP, because that blocks third-party RTSP. Make sure the cameras record
   (continuous or event recording), since clips are cut from the recording.
5. Check the NVR's time zone and clock. The bridge's `VIGI_TIMEZONE` must match,
   or clip replay fetches the wrong footage. The bridge logs a warning if the
   two clocks disagree by more than 15 minutes.

Integration details used here come from TP-Link's VIGI FAQs: the Alarm Server
push (JSON, or multipart with `event` + `snapshot` parts), the URL snapshot
`https://<nvr>/snapshot?channel=N` (Digest auth), and RTSP
`rtsp://<nvr>:554/live/<ch>/<stream>/avm` and
`.../replay/<ch>/<stream>/avm?starttime=YYYYMMDDtHHMMSSz&endtime=...` (UTC).
TP-Link publishes event codes for motion (1/2), line crossing (1/4, with 101
for person and 102 for vehicle), intrusion (1/5) and alarm input (2/13). Other
codes are stored as `typeX_subY`. Map them with `event_labels`, e.g.
`{"1:6": "region_entering"}`. The full raw message of every event is kept at
`GET /vigi/events/<id>`, so you can see exactly what your firmware sends.

**Lockout warning:** the NVR blocks an IP for 30 minutes after 10 failed
logins. If the bridge's NVR password is wrong, it stops calling the NVR for
30 minutes after the first rejection instead of retrying.

## Where to run it

The bridge needs to reach the NVR (snapshots and clips), and the NVR needs to
reach the bridge, so **run it on a machine on the same network as the NVR**:
a mini PC, a Raspberry Pi, or a NAS with Docker.

```bash
docker build -t jarvis .
docker run -d --name jarvis --restart unless-stopped -p 8080:8080 -v jarvis-data:/data \
  -e VIGI_TIMEZONE=Africa/Johannesburg -e VIGI_INGEST_TOKEN=change-me -e VIGI_API_KEY=change-me-too \
  -e VIGI_NVR_HOST=192.168.1.50 -e VIGI_NVR_PASSWORD=... \
  -e WHATSAPP_TOKEN=... -e WHATSAPP_PHONE_NUMBER_ID=... -e WHATSAPP_TO=27821234567,27831234567 \
  -e WHATSAPP_APP_SECRET=... -e WHATSAPP_VERIFY_TOKEN=... \
  -e WHATSAPP_ALARM_TEMPLATE=security_alarm -e WHATSAPP_CLIP_TEMPLATE=security_clip \
  -e WHATSAPP_REPORT_TEMPLATE=security_report \
  -e ANTHROPIC_API_KEY=... \
  jarvis
```

Or without Docker (needs Python 3.10+ and `ffmpeg` for clips):

```bash
pip install -r requirements.txt
export VIGI_TIMEZONE=Africa/Johannesburg VIGI_INGEST_TOKEN=... VIGI_API_KEY=...
gunicorn app:app --bind 0.0.0.0:8080 --workers 1 --threads 4
```

You can run it on the existing cloud deployment (Procfile) instead. The NVR
can push out over HTTPS. But the cloud copy can't pull clips or snapshots
unless you expose the NVR to the internet, which isn't recommended, and the
platform's disk is usually wiped on restart.

## Configuration

Secrets go in environment variables. Rules and everything else can go in a
JSON file pointed to by `VIGI_CONFIG`. See `vigi_config.example.json`, and
`vigi_bridge/config.py` for every option and its default.

| Variable | Purpose |
|---|---|
| `VIGI_INGEST_TOKEN` | Secret in the NVR's Alarm Server URL. Strongly recommended |
| `VIGI_API_KEY` | Required for status, arming, events, media and reports (`X-API-Key` header or `?key=`) |
| `VIGI_TIMEZONE` | NVR time zone, e.g. `Africa/Johannesburg` (default `UTC`) |
| `VIGI_NVR_HOST`, `VIGI_NVR_USER`, `VIGI_NVR_PASSWORD` | Lets the bridge pull snapshots and clips |
| `VIGI_ARM_MODE` | Starting mode: `schedule`, `armed` or `disarmed` |
| `VIGI_CONFIG` | Path to the JSON config file |
| `VIGI_DATA_DIR` | Where the database, snapshots and clips go (default `vigi_data`) |
| `VIGI_PUBLIC_URL` | Base URL used for links in alerts |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Cloud API credentials (see the WhatsApp section) |
| `WHATSAPP_TO` | Comma-separated numbers in international format (`27821234567`) that get alerts and may send commands |
| `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | Check and verify the incoming-commands webhook |
| `WHATSAPP_ALARM_TEMPLATE`, `WHATSAPP_CLIP_TEMPLATE`, `WHATSAPP_REPORT_TEMPLATE` | Approved template names, used outside the 24-hour window |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram alerts: photo, then clip |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO` | Email alerts and reports (`ALERT_EMAIL_TO` is comma-separated) |
| `VIGI_WEBHOOK_URL` | POSTs JSON for alarms, clips and reports, e.g. to a control room or home automation |
| `ANTHROPIC_API_KEY` | Turns on the AI snapshot check |
| `VIGI_AI_MODEL` | Model for the AI check (default `claude-opus-5-5`) |

### AI check and cost

The check makes one Claude API call per alarm candidate (not per motion event).
It uses `claude-opus-5-5` at `effort: "low"` with a structured JSON verdict.
Server-side refusal fallback (`fallbacks: "default"`) is on, so a declined
request is retried on another model instead of failing. Snapshots are capped
at 5 MB. To cut cost, set `ai.model` to a cheaper model such as
`claude-haiku-5-5`, or set `ai.mode` to `advisory` (alarm per rules, AI only
describes) or `off`. `ai.site_notes` tells the model what's normal at your site
("two dogs roam the yard at night").

## API

All endpoints except `/vigi/alarm` and the WhatsApp webhook need the API key.

| Method | Path | |
|---|---|---|
| POST | `/vigi/alarm/<token>` | NVR Alarm Server target (JSON or multipart) |
| GET, POST | `/vigi/whatsapp/webhook` | Meta's webhook for incoming WhatsApp commands (verify token and signature checked, no API key) |
| GET | `/vigi/status` | Armed state, today's counts, what's configured |
| POST | `/vigi/arm` | `{"mode": "armed" \| "disarmed" \| "schedule"}` |
| GET | `/vigi/events` | Filters: `status`, `channel`, `label`, `period=24h\|7d\|30d`, `from`, `to`, `limit`, `offset` |
| GET | `/vigi/events.csv` | Same filters, as a spreadsheet |
| GET | `/vigi/events/<id>` | Full event, including the raw NVR message and AI verdict |
| GET | `/vigi/events/<id>/snapshot.jpg`, `/clip.mp4` | Media |
| POST | `/vigi/events/<id>/clip` | Pull a clip for any event on demand |
| GET | `/vigi/report` | `period` or `from`/`to`; `format=json\|text\|html` |
| POST | `/vigi/report/send` | Send a report through the configured notifiers now |

A daily summary goes out at `report.daily_at` (default 07:00). It covers the
previous 24 hours: totals, alarms with descriptions, false triggers filtered,
the busiest camera, and the busiest hour.

## Trying it without an NVR

```bash
VIGI_INGEST_TOKEN=t VIGI_API_KEY=k VIGI_ARM_MODE=armed python app.py
python tools/simulate_nvr.py --url http://localhost:5000 --token t --event intrusion --channel 1 --snapshot some.jpg
curl -H "X-API-Key: k" "http://localhost:5000/vigi/report?format=text"
```

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests
```
