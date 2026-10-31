# Keep me updated

Jarvis messages you without being asked. It only sends when there's something worth knowing.

| When | What you get |
|---|---|
| **Morning brief** (06:00) | Your Outlook meetings, then where every crew is, then unassigned jobs and clashes, then emails that need you today |
| **Checks** (every 15 min, 06:00–19:00) | Sent **only if something is new**: important emails (junk and newsletters filtered out by the AI), crew updates ("Sipho on site at #14"), jobs moved in Outlook (with clash warnings), unassigned jobs starting within 2 hours, and jobs that ran 30+ minutes over without being closed off. Each alert is sent once. |
| **Evening wrap** (17:30) | What got done, what's still open, tomorrow's plan |

## Where it goes

Set `NOTIFY_CHANNELS` to any combination of these:
- `email`: to your own Outlook inbox (no extra setup).
- `whatsapp`: through a Twilio WhatsApp sender, to `OWNER_PHONE`. WhatsApp only allows free-form messages within 24 hours of your last message to the number, so reply "ok" each morning, or get a message template approved in Twilio.
- `sms`: from `TWILIO_FROM_NUMBER` to `OWNER_PHONE`.

If one channel fails, the others still go out. Every update is logged in `GET /api/activity`.

## Running it on time

- **Built-in:** set `JARVIS_SCHEDULER=1`. This needs a server that stays awake, such as a paid Heroku dyno, Railway, Render paid, or a VPS.
- **Or with any cron service** (cron-job.org is free): call
  `POST /api/updates/run` with header `Authorization: Bearer $JARVIS_API_KEY` and body `{"kind":"morning"}` at 06:00,
  `{"kind":"check"}` every 15 minutes, and `{"kind":"evening"}` at 17:30.

The times (`MORNING_BRIEF_AT`, `EVENING_WRAP_AT`, `CHECK_EVERY_MIN`, `WORK_HOURS`) use `CALENDAR_TZ` (South Africa by default).
If the server was down at 06:00, the morning brief still goes out when it comes back, but only until about 09:00. After that it's skipped, so you never get a morning brief at night.
