# Who can do what

Each staff member has an **access level**:

| | admin (you) | scheduler | crew |
|---|---|---|---|
| See jobs, staff list, day sheet, clashes | ✅ | ✅ | ✅ (jobs only through the API) |
| Book, move and assign jobs | ✅ | ✅ | ❌ |
| Update job status | ✅ any job | ✅ any job | ✅ own jobs only |
| Text crew about jobs | ✅ | ✅ | ❌ |
| Add, remove or change staff (including access levels) | ✅ | ❌ | ❌ |
| Your email, drafts, sending | ✅ | ❌ | ❌ |
| Your Outlook calendar, briefs, settings, call log, API keys | ✅ | ❌ | ❌ |

Jobs a scheduler books still appear in your Outlook calendar and in your update checks
("#14 Trench (Sipho) booked — by Lerato"), so you can see what they've done.

The same rules apply everywhere: in the API, in chat with Claude (a scheduler's Claude only gets
the scheduling tools), and on the phone (callers are recognised by extension or number).

## Give someone scheduling rights

```bash
U=https://<your-app>; H="Authorization: Bearer $JARVIS_API_KEY"   # your master key

# 1. add them as a scheduler (or change an existing person: PATCH /api/staff/<id> {"access":"scheduler"})
curl -XPOST $U/api/staff -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Lerato","phone":"+2782...","sip_ext":"210","access":"scheduler"}'

# 2. make them a personal key. It is shown ONCE, so send it to them securely.
curl -XPOST $U/api/keys -H "$H" -H 'Content-Type: application/json' -d '{"staff":"Lerato","label":"office PC"}'
```

They use their key exactly the way you use yours (`Authorization: Bearer jv_...`). You can also
just tell Claude: "Add Lerato as a scheduler, number 082…, extension 210."
Claude can set the access level, but making keys is API-only.

## Take it away

- `DELETE /api/keys/<id>` revokes one key. `GET /api/keys` lists keys, who owns them and when each was last used.
- `PATCH /api/staff/<id> {"access":"crew"}` downgrades someone.
- `DELETE /api/staff/<id>` deactivates the person, and all their keys stop working immediately.

Keys are stored only as a hash, so a database leak doesn't reveal them. Your `JARVIS_API_KEY` stays the master admin key.
