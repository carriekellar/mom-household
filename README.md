# Mom Household

A tiny group scheduling assistant you text. No accounts, slot-picking forms, or app downloads.

An organizer describes a playdate, carpool, dinner, or meetup. Friends text the same number
with a shared plan code and their availability. The assistant finds an overlap, asks everyone
to confirm, sends reminders, and handles cancellations.

## Status

Backend prototype with Twilio texting, availability matching, confirmations, reminders, and
cancellations. Vercel handlers and Supabase Postgres persistence are implemented; the local
SQLite demo remains available. Twilio is the default provider; its account and number setup remain pending. **Not yet deployed or verified
with live group texting.** Live provider credentials, registration, hosting, and an OpenAI extraction check remain required.

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

1. Create a [Twilio account](https://www.twilio.com/try-twilio), complete account verification,
   and obtain an SMS-enabled US number. Complete toll-free verification or local-number
   10DLC registration before live group texting. Trial sends are limited to verified recipients.
2. Copy `.env.example` to `.env`. Set `SMS_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` (E.164), and `OPENAI_API_KEY`.
   Keep credentials in the ignored `.env` file or private hosting variables.
3. Set `TWILIO_WEBHOOK_URL` to the exact public HTTPS URL, including any trailing slash.
   Configure the Twilio number's **A message comes in** webhook to this URL, method **POST**. The app uses the same URL for outbound delivery callbacks.
4. For local hosting, run `python server.py` behind HTTPS/a development tunnel. Vercel
   uses `/sms` as described below. Enable Twilio's standard STOP/START handling and verify
   it during acceptance testing; the app also suppresses opted-out recipients.
5. Local SQLite requires one continuously running instance and persistent storage.
   Vercel uses Supabase and scheduled processing instead. `/health` checks HTTP liveness only.
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
   `OPENAI_API_KEY`, `SMS_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
   `TWILIO_PHONE_NUMBER`, and `TWILIO_WEBHOOK_URL`.
   Optionally set `OPENAI_MODEL` and `HOUSEHOLD_TIMEZONE`. Never use public-prefixed secrets.
   Use Supabase's **session pooler** (port 5432) or a direct database connection, with
   `sslmode=require`. Transaction pooling (port 6543) is not supported: the worker uses a
   session advisory lock. Generate a random `CRON_SECRET` locally (at least 32 random bytes).
4. Deploy with Fluid Compute enabled and a 300-second function duration. `/health` checks
   HTTP liveness only. Production webhooks must be reachable by Twilio and Supabase without
   an interactive login; preview deployments must not share production provider/database keys.
5. Enable `pg_cron` and `pg_net` in Supabase Extensions. Add `household_url` (production
   Vercel origin) and `household_cron_secret` (matching `CRON_SECRET`) in Supabase Vault.
   Run `supabase/schedule.sql`. It invokes `/api/process` once per minute.
6. Configure the Twilio number's incoming-message webhook (POST) and `TWILIO_WEBHOOK_URL` as
   `https://YOUR-DOMAIN/sms`, then test with a
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
- `transport.py`, `twilio_sms.py`: provider dispatch, Twilio sending and signature validation, queued delivery.
- `server.py`: local SQLite webhook endpoint and background worker.
- `cloud.py`, `api/`: Vercel signed webhook and authenticated scheduled worker.
- `postgres.py`, `supabase/`: Postgres persistence, private schema, and cron setup.
- `demo.py`, `test_household.py`: offline walkthrough and behavioral tests.

Inbound message IDs are unique, preventing duplicate processing on webhook retries. Webhook
handlers acknowledge after durable enqueueing; model calls happen on the worker. Model calls
do not hold a database write lock. A single worker owns state updates. Twilio signatures are verified using the official SDK against the exact configured HTTPS
URL and all submitted form fields. Host/proxy headers cannot override the signed URL.
Signed Account SID and destination are checked. Duplicate Message SIDs are ignored.
Successful callbacks return empty TwiML; actual replies are sent by the durable worker.


The outbox suppresses obsolete schedule versions and opted-out/declined recipients before
sending. Messages accepted by the provider are tracked separately from delivery callbacks. Ambiguous
send failures become `needs_review` instead of automatically sending duplicate texts. Inspect
those rows against provider message records before deciding whether to requeue. Inbound provider
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

- [Twilio callbacks](https://www.twilio.com/docs/messaging/guides/webhook-request)
- [Twilio signature validation](https://www.twilio.com/docs/usage/security)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

Legacy Telnyx support is retained only when explicitly setting `SMS_PROVIDER=telnyx` with
`TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`, and `TELNYX_PHONE_NUMBER`. The default never sends
through Telnyx. Previously purchased Telnyx resources are not cancelled by this code change.

Plivo support remains available only with `SMS_PROVIDER=plivo` and its `PLIVO_AUTH_ID`,
`PLIVO_AUTH_TOKEN`, `PLIVO_PHONE_NUMBER`, and `PLIVO_WEBHOOK_URL` settings. Existing Plivo
or Telnyx accounts and numbers are not changed by selecting Twilio.

Twilio setup requires `TWILIO_ACCOUNT_SID`, the corresponding account `TWILIO_AUTH_TOKEN`
(for signature validation and initial API access), `TWILIO_PHONE_NUMBER`, and
`TWILIO_WEBHOOK_URL`. Do not put credentials in chat, Git, or callback URLs. A trial can
help validate the integration but is not a substitute for sender registration and approval.
For toll-free SMS, follow Twilio's current verification flow before sending to the group.

## Starter send cap

`SMS_SEND_LIMIT` defaults to 100 outbound API attempts over the lifetime of this database.
Pending and suppressed messages do not count; uncertain and failed sends do. At the cap,
the worker leaves remaining messages pending and does not call the sending API. Set 0 to
pause sends. The count persists across restarts; do not delete outbox rows to reset it.
This uses the existing single-worker guarantee (Postgres advisory lock or one SQLite worker).

This is not a Twilio credit counter: multipart messages, incoming texts, provider-generated
responses, and sends made elsewhere can affect the provider allowance separately. Check the
Twilio dashboard before a live pilot. Increasing the cap later releases pending messages;
review the queue first, including any old reminders.

## Work Wife registration page drafts

The consumer product is now **Work Wife**. Static drafts live in `public/`:

- `index.html`: opt-in invitation, proposed JOIN → Y transcript, and proof preparation notes.
- `privacy.html`: Privacy Policy draft reflecting the prototype's data handling.
- `terms.html`: Terms & Conditions draft with SMS disclosures and support information.

Preview with `python3 -m http.server 8765 --directory public`. Vercel serves `public`
with aliases `/opt-in`, `/privacy`, and `/terms`; the existing Python API rewrites remain.
These pages are drafts, not evidence of a live opt-in system. Before registration, finalize
retention and provider details, implement and test enrollment and keyword responses,
confirm the published policy URLs, and review policies with the operator.
Only remove draft notices when the described behavior is actually available. Support is
`carrie@candur.ai`; the operator is Carrington Hay Kellar. No SMS flow or provider settings
are changed by these static pages.

Draft pages are published at https://work-wife-registration.vercel.app in the
`thecarringtonevent/work-wife-registration` Vercel project. This standalone static
deployment contains only `public/` and does not deploy the scheduler or credentials.
