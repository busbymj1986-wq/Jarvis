# SIP and voice setup

Jarvis answers a phone line or SIP extension. Twilio turns speech into text. Claude
runs the staff tools (rosters, jobs, clashes, SMS) and the answer is read back to the caller.

```
Desk phone / softphone / PBX ──SIP──▶ Twilio SIP Domain ─┐
Mobile / landline ──PSTN──▶ Twilio number ───────────────┼─▶ POST /voice/incoming  (greeting + listen)
                                                         └─▶ POST /voice/gather    (speech → Claude → reply)
                                                             POST /voice/status    (call ended, clear memory)
```

## 1. Twilio SIP Domain

Twilio Console → Voice → Manage → SIP domains → **Create**.

| Setting | Value |
|---|---|
| SIP URI | `jarvis-yourco.sip.twilio.com` (put the same value in `SIP_DOMAIN`) |
| Voice configuration → A call comes in | Webhook `POST https://<your-app>/voice/incoming` |
| Call status changes | `POST https://<your-app>/voice/status` |
| Authentication → Credential lists | One credential per staff member, username = their extension (e.g. `201`) |
| SIP Registration | **Enabled** (desk phones and softphones register to the domain) |

Point your Twilio phone number's "A call comes in" setting at the same `/voice/incoming` webhook so mobile callers reach Jarvis too.

## 2. Phones and softphones (Zoiper, Linphone, Yealink, Grandstream)

| Field | Value |
|---|---|
| Username / Auth ID | the extension, e.g. `201` |
| Password | the password from the credential list |
| Domain / Registrar / Proxy | `jarvis-yourco.sip.twilio.com` (use `jarvis-yourco.sip.<edge>.twilio.com` for a regional edge) |
| Transport | TLS on port 5061 (UDP/TCP 5060 also work) |
| Media | SRTP if TLS is used |
| Codecs | PCMU and PCMA (G.711), plus Opus if offered |

Dial any number from a registered phone and Jarvis answers.

## 3. Existing PBX (3CX, FreePBX/Asterisk)

Create a SIP trunk to Twilio Elastic SIP Trunking. Give the trunk an Origination URI of
`sip:jarvis-yourco.sip.twilio.com`, or route a PBX extension such as `*500` to
`sip:jarvis@jarvis-yourco.sip.twilio.com`. Then keep extensions in step: the `sip_ext` you set
on each staff member in Jarvis must match the user part the PBX sends in `From`
(`sip:201@...`).

## 4. Staff and extensions in Jarvis

Jarvis identifies callers in this order:
1. The SIP `From` user part, matched against `sip_ext` (for example `sip:201@...` matches the staff member with extension 201).
2. The caller-ID number, matched against `phone` on the last 9 digits, so `082…` and `+2782…` both match.

Unknown callers are refused unless `ALLOW_UNKNOWN_CALLERS=1`.

```bash
curl -X POST https://<app>/api/staff -H "Authorization: Bearer $JARVIS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Sipho Dlamini","phone":"+27822220000","sip_ext":"201"}'
curl https://<app>/api/sip -H "Authorization: Bearer $JARVIS_API_KEY"   # shows every extension and webhook
```

## 5. What callers can say

| Caller says | What happens |
|---|---|
| "What am I doing today?" | `list_jobs` for the caller |
| "Book Sipho for a fibre install at 12 Main Road tomorrow 8 to 10" | `create_job` (managers only), with a clash check |
| "Move job 14 to Thursday at 2" | `reschedule_job`, with a clash check |
| "Give job 14 to Thandi" | `assign_job` |
| "I'm on site at job 14" | `update_job_status` (crew can only update their own jobs) |
| "Text Sipho that the client moved to 9" | `notify_staff` sends an SMS |
| "Put me through to Mia" | Transfers the call to `sip:<ext>@SIP_DOMAIN`, or to her mobile if she has no extension |
| "Goodbye" | Hangs up |

Managers (`is_manager`) get every tool. Crew get read-only tools plus status updates on their own jobs.

## 6. Security and limits

- Set `TWILIO_AUTH_TOKEN` and `PUBLIC_BASE_URL`. Every `/voice/*` request is then checked against Twilio's signature, and forged requests get a 403.
- Use TLS/SRTP on phones. Restrict the SIP domain with an IP access control list if your PBX has a static IP.
- Twilio waits about 15 seconds for a webhook. Voice requests run Claude at `effort: low` to stay fast.
- Conversation memory is kept per call (by CallSid) in process memory. Run gunicorn with one worker (`--workers 1 --threads 8`), or move that memory to Redis.
