# Jarvis

Flask backend for Jarvis. Besides the original support-call demo endpoints
(`/api/status`, `/api/next`, `/api/reset`, `/api/sms`, `/api/voice`), it now
includes a **VIGI NVR bridge**: a bolt-on that a TP-Link VIGI NVR reports to,
which keeps snapshots and short clips, decides when an event is worth an alarm,
alerts you, and produces reports.

## VIGI bridge: how it works

```
VIGI NVR --(Alarm Server push: event JSON + snapshot)--> POST /vigi/alarm/<token>
                                                              |
   1. store event + snapshot (reply 200 to the NVR immediately)
   2. rules: armed? which camera? which event type? escalation?   -> logged / ignored
   3. cooldown: already alerted for this camera recently?         -> grouped
   4. AI check (optional): Claude looks at the snapshot           -> dismissed
   5. ALARM: Telegram / email / webhook with the snapshot
   6. clip: pull 5 s before -> 10 s after from the NVR over RTSP, send it as a follow-up
                                                              |
                         reports: /vigi/report, /vigi/events.csv, daily summary at 07:00
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
  -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_CHAT_ID=... \
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

All endpoints except `/vigi/alarm` need the API key.

| Method | Path | |
|---|---|---|
| POST | `/vigi/alarm/<token>` | NVR Alarm Server target (JSON or multipart) |
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
