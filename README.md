# Mom Household

A tiny group scheduling assistant you text. No accounts, slot-picking forms, or app downloads.

An organizer describes a playdate, carpool, dinner, or meetup. Friends text the same number
with a shared plan code and their availability. The assistant finds an overlap, asks everyone
to confirm, sends reminders, and handles cancellations.

## Status

Backend prototype with Telnyx texting, availability matching, confirmations, reminders, and
cancellations. Vercel handlers and Supabase Postgres persistence are implemented; the local
SQLite demo remains available. A Telnyx number is provisioned. **Not yet deployed or verified
with live group texting.** Telnyx credentials/account access and OpenAI API credit need resolving.

## Try the offline demo

Python 3.11 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python demo.py
python -m unittest -v
```

The demo uses explicitly scripted extraction fixtures and simulated SMS delivery. It exercises
the actual scheduling, persistence, confirmation, reminder, and cancellation code; it does not
pretend to test the language model or send real texts.

## Connect a real texting number

1. Create a [Telnyx account](https://telnyx.com/sign-up), obtain an SMS-capable number, and assign
   it to a messaging profile. Complete Telnyx's sender verification/registration for your number
   type and destination before testing with personal phones.
2. Copy `.env.example` to `.env`. Set the Telnyx API key, webhook public key, phone number in
   E.164 format, and an OpenAI API key. Never commit `.env`.
3. Run `python server.py`. Expose port 8000 behind an HTTPS reverse proxy or development tunnel.
4. Set the messaging profile webhook URL to `https://YOUR-HOST/sms`. Enable the provider's
   standard STOP/START handling. The app additionally suppresses opted-out recipients.
5. Keep one application instance running on a persistent disk. Its background worker handles
   extraction and sends queued messages; reminders are checked every second. `/health` is a
   basic HTTP liveness check, not a guarantee that the worker or providers are healthy.
6. Text your number: “Plan an hour-long park playdate next week for three parents including
   me. I'm Alex, free Saturday 10 am to noon.” Answer any short clarifying questions. Forward
   the invitation and **the actual texting number** to your friends.

There are no unsolicited outbound invitations: each participant texts the service first.
The organizer supplies the expected number of adult respondents, including themselves.
The service never interprets missing responses as agreement.

## Vercel + Supabase deployment (preferred)

1. Create a Supabase project. Apply `supabase/migrations/202610210001_household.sql`
   in its SQL editor. Family data lives in the private `household` schema, with RLS enabled
   and no browser policies. Do not expose this schema through the Data API.
2. Import this GitHub repository into Vercel using framework preset **Other**. Select
   `feat/sms-scheduling` until PR #1 is merged; `main` does not contain the app yet.
3. Set private production environment variables: `DATABASE_URL`, `CRON_SECRET`,
   `OPENAI_API_KEY`, `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`, `TELNYX_PHONE_NUMBER`.
   Optionally set `OPENAI_MODEL` and `HOUSEHOLD_TIMEZONE`. Never use public-prefixed secrets.
   Use Supabase's **session pooler** (port 5432) or a direct database connection, with
   `sslmode=require`. Transaction pooling (port 6543) is not supported: the worker uses a
   session advisory lock. Generate a random `CRON_SECRET` locally (at least 32 random bytes).
4. Deploy with Fluid Compute enabled and a 300-second function duration. `/health` checks
   HTTP liveness only. Production webhooks must be reachable by Telnyx and Supabase without
   an interactive login; preview deployments must not share production provider/database keys.
5. Enable `pg_cron` and `pg_net` in Supabase Extensions. Add `household_url` (production
   Vercel origin) and `household_cron_secret` (matching `CRON_SECRET`) in Supabase Vault.
   Run `supabase/schedule.sql`. It invokes `/api/process` once per minute.
6. Set Telnyx's messaging profile webhook to `https://YOUR-DOMAIN/sms`, then test with a
   consenting group. A reply normally waits for the next minute's processing tick; heavier
   traffic can take longer. Reminders do not depend on an always-running server.

The signed webhook only inserts into a durable inbox. A bounded worker processes up to three
incoming texts per run, then sends queued responses. A database lock prevents overlapping
workers from processing or sending twice. State changes, replies, and inbound completion commit
atomically. Interrupted/ambiguous sends require review instead of blind retries. Delivery
callbacks are retained even if they arrive before the send response.

Monitor `household.inbox` and `household.outbox` for `needs_review`, `cron.job_run_details`
for scheduling failures, and `net._http_response` for HTTP failures. A successful cron SQL
execution alone does not mean the HTTP request succeeded. Inspect provider records before
requeuing any ambiguous send. Do not enable the cron job on preview databases.

The Dockerfile and `railway.toml` remain an optional single-instance SQLite deployment path.
Do not use SQLite or `server.py` as the Vercel worker.

See [Vercel Python functions](https://vercel.com/docs/functions/runtimes/python)
and [Supabase scheduled HTTP requests](https://supabase.com/docs/guides/functions/schedule-functions).

## Conversation

```text
Alex:  Plan a playdate at Prospect Park for 3 parents including me.
       An hour on October 24. I'm free 10 am to noon.
Bot:   Forward: Text A1B2C3 and when you're free to this number.
Sarah: A1B2C3 I'm Sarah, October 24 from 10 am to noon.
Jess:  A1B2C3 Jess here, October 24 from 11 am to 1 pm.
Bot:   Saturday at 11 works for everyone. Reply A1B2C3 YES to confirm.
All:   YES
Bot:   It's on! [time, location]. Reminders the day before and one hour before.
Sarah: Can't make the playdate.
Bot:   You're marked as not coming. Your reminders are stopped.
       [Organizer gets notified; the group plan stays in place.]
```

Times are interpreted in the plan's disclosed timezone (America/New_York by default).
Unclear phrases such as “after school” prompt a short question about the actual time range.
The AI reads back interpreted availability. Final YES responses are required from everyone.
If no overlap exists, the assistant asks for additional availability. Each update replaces that
person's previous availability, so include all options. A changed schedule resets confirmations.

`STATUS` lists active plans. Include a code when participating in multiple plans. `NEW ...`
starts another plan. `NO` cancels only your attendance. The organizer can request `RESCHEDULE`;
this clears the old time and asks everyone again. Cancelling the entire event requires the
organizer to text `CODE CANCEL PLAN`. `STOP` opts out; `START` re-enables messages but does not
automatically restore an RSVP.

## Implementation

- `household.py`: deterministic state machine, interval intersection, SQLite inbox/outbox.
- `language.py`: Responses API with Structured Outputs; no model access to SMS tools.
- `transport.py`: Telnyx send API, Ed25519 signature verification, queued delivery.
- `server.py`: local SQLite webhook endpoint and background worker.
- `cloud.py`, `api/`: Vercel signed webhook and authenticated scheduled worker.
- `postgres.py`, `supabase/`: Postgres persistence, private schema, and cron setup.
- `demo.py`, `test_household.py`: offline walkthrough and behavioral tests.

Inbound message IDs are unique, preventing duplicate processing on webhook retries. Webhook
handlers acknowledge after durable enqueueing; model calls happen on the worker. Model calls
do not hold a database write lock. A single worker owns state updates. Signature verification
uses the exact raw body and rejects timestamps more than five minutes away.

The outbox suppresses obsolete schedule versions and opted-out/declined recipients before
sending. Messages accepted by Telnyx are tracked separately from delivery callbacks. Ambiguous
send failures become `needs_review` instead of automatically sending duplicate texts. Inspect
those rows against Telnyx message records before deciding whether to requeue. Inbound provider
errors retry up to three times, then need operator review. This prototype has no operator UI.

## First-version boundaries

- 2–12 adults, one-off events, 15–240 minute duration, availability within 90 days.
- Reminders are due at 24 hours and one hour before the event. A 15-minute catch-up window
  avoids obsolete reminders after downtime. Plans confirmed after a reminder window skip it.
- No calendar sync, recurring carpools, seat assignments, media, or location editing yet.
  Supply a location when creating the plan; otherwise messages say “Location to follow.”
- Invitation codes are bearer invitations. Keep them within the group. Group members' raw
  availability and phone numbers are not broadcast to other participants.
- Phone numbers, availability, conversation clarifications, and message bodies are stored
  in the configured database. Relevant user text is sent to OpenAI for parsing with `store: false`.
  Secure the disk and establish retention/deletion and a privacy notice before a public pilot.
- The Postgres MVP serializes work through one shared state record. Before opening to strangers, add per-number abuse limits, spending
  controls, operational alerts, a retention job, and a live carrier/AI acceptance test.

## Integration references

- [Telnyx sending messages](https://developers.telnyx.com/docs/messaging/messages/send-message)
- [Telnyx webhooks and signatures](https://developers.telnyx.com/docs/messaging/messages/receiving-webhooks)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
