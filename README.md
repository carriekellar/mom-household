# Mom Household

A tiny group scheduling assistant you text. No accounts, slot-picking forms, or app downloads.

An organizer describes a playdate, carpool, dinner, or meetup. Friends text the same number
with a shared plan code and their availability. The assistant finds an overlap, asks everyone
to confirm, sends reminders, and handles cancellations.

## Status

First working backend prototype. Telnyx SMS integration, OpenAI natural-language extraction,
SQLite persistence, confirmations, reminders, and cancellations are implemented. The automated
tests and offline demo run without credentials. **Live carrier delivery and live AI extraction
have not yet been verified.** No texting number is provisioned and nothing is deployed.

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
- `server.py`: authenticated webhook endpoint and background worker.
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
  in the local database. Relevant user text is sent to OpenAI for parsing with `store: false`.
  Secure the disk and establish retention/deletion and a privacy notice before a public pilot.
- Single instance only. Before opening to strangers, add per-number abuse limits, spending
  controls, operational alerts, a retention job, and a live carrier/AI acceptance test.

## Integration references

- [Telnyx sending messages](https://developers.telnyx.com/docs/messaging/messages/send-message)
- [Telnyx webhooks and signatures](https://developers.telnyx.com/docs/messaging/messages/receiving-webhooks)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
