"""Small SMS scheduling domain. Python 3.11+, standard library only."""
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from plan_dates import date_plan, duration_text, normalize_ranges, match_dates, event_end, reminder_schedule

UTC = timezone.utc
CONSENT_VERSION = '2026-11-08'
SITE_URL = 'https://work-wife-registration.vercel.app'
def plan_handle(title, events):
    # Reserve both public invitations and legacy internal IDs to avoid ambiguity.
    used = {str(value).lower() for key, event in events.items()
            for value in (key, event.get('code', ''), event.get('handle', ''))}
    if sum(len(value) == 6 and value.isascii() and value.isdigit()
           and value[0] != '0' for value in used) >= 900000:
        raise ValueError('No invitation codes available')
    while True:
        code = str(100000 + secrets.randbelow(900000))
        if code not in used:
            return code


def label(event):
    return event.get('handle', event['code'])

WELCOME = ("Work Wife: Get automated scheduling texts for plans you join: availability, confirmations, "
           "reminders, changes and cancellations. Message frequency varies. Msg & data rates may apply. "
           "Reply HELP for help or STOP to opt out. Support: carrie@candur.ai. "
           f"Terms: {SITE_URL}/terms.html Privacy: {SITE_URL}/privacy.html "
           "Reply Y to confirm enrollment and accept the Terms.")
ENROLLED = ("Work Wife: You're enrolled in scheduling texts. Message frequency varies. "
            "Msg & data rates may apply. HELP for help; STOP to opt out. Support: carrie@candur.ai. "
            "Tell us what you'd like to plan, or send your plan code and availability.")
HELP = ("Work Wife scheduling support: carrie@candur.ai. Describe a plan or send your plan code "
        "and availability. Message frequency varies. Msg & data rates may apply. Reply STOP to opt out.")
INVITE = f"Work Wife: To enroll, read {SITE_URL} and text JOIN. HELP for help; STOP to opt out."


def enrolled(state, phone):
    return state.get('consents', {}).get(phone, {}).get('status') == 'enrolled'


def permitted_message(state, phone, body):
    # Also gate legacy queued messages when upgrading an existing database.
    return phone not in state['blocked'] and (enrolled(state, phone) or body in (WELCOME, HELP, INVITE))



def stamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('A timezone offset is required.')
    return dt.astimezone(UTC)


def overlap(groups, minutes, now):
    """Intersect unions of intervals; never infer availability from silence."""
    if not groups or any(not group for group in groups):
        return None
    common = [(stamp(a), stamp(b)) for a, b in groups[0]]
    for group in groups[1:]:
        common = [(max(a, stamp(c)), min(b, stamp(d)))
                  for a, b in common for c, d in group
                  if max(a, stamp(c)) < min(b, stamp(d))]
    for a, b in sorted(common):
        a = max(a, now + timedelta(minutes=15))
        if b - a >= timedelta(minutes=minutes):
            return a.isoformat()
    return None


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
          INSERT OR IGNORE INTO state VALUES (1, '{"events":{},"drafts":{},"blocked":[]}');
          CREATE TABLE IF NOT EXISTS inbox (
            sid TEXT PRIMARY KEY, phone TEXT, body TEXT, status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, phone TEXT, body TEXT, event TEXT,
            version INTEGER, status TEXT DEFAULT 'pending', provider_sid TEXT,
            error TEXT);
        ''')
        self.db.commit()

    def load(self):
        return json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])

    def enqueue(self, sid, phone, body):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO inbox(sid,phone,body) VALUES(?,?,?)',
                            (sid, phone, body))

    def process(self, parser, now):
        # One worker owns processing. Webhooks only append to inbox.
        row = self.db.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
        if not row:
            return False
        state = self.load()
        app = Scheduler(state, parser, now)
        try:
            app.receive(row['phone'], row['body'])
        except (ValueError, KeyError, TypeError):
            # Discard partial changes if model output fails validation.
            state = self.load()
            app = Scheduler(state, parser, now)
            app.say(row['phone'], 'I could not safely read that. Please restate which dates work; include start/end times only for a timed event.')
        with self.db:
            self.db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
            if row['phone'] in state['blocked']:
                self.db.execute("UPDATE outbox SET status='suppressed' WHERE phone=? AND status='pending'", (row['phone'],))
            self.save_messages(app.messages)
            self.db.execute("UPDATE inbox SET status='done' WHERE sid=?", (row['sid'],))
        return True

    def save_messages(self, messages):
        self.db.executemany('INSERT INTO outbox(phone,body,event,version) VALUES(?,?,?,?)', messages)

    def remind(self, now):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            state = self.load()
            app = Scheduler(state, None, now)
            for e in state['events'].values():
                if e['status'] != 'confirmed':
                    continue
                # Calendar plans remind at 9am local the preceding day.
                # Narrow windows avoid sending stale reminders after downtime.
                for kind, due in reminder_schedule(e):
                    key = f"{e['version']}:{kind}"
                    if 0 <= (now - due).total_seconds() <= 900 and key not in e['reminders']:
                        app.broadcast(e, f"Reminder: {app.details(e)}. Can't make it? Reply that you can't come. To switch to this plan, text {label(e)}.")
                        e['reminders'].append(key)
            self.db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
            self.save_messages(app.messages)


class Scheduler:
    def __init__(self, state, parser, now):
        self.state, self.parser, self.now = state, parser, now
        self.messages = []
        self.state.setdefault('consents', {})
        self.state.setdefault('consent_log', [])
        self.state.setdefault('threads', {})
        self.state.setdefault('pending_invitations', {})

    def say(self, phone, body, event=None, bare=False):
        if permitted_message(self.state, phone, body):
            selected = self.state['threads'].get(phone)
            if event and selected and selected != event['code']:
                self.state['threads'].pop(phone, None)
                body += f' To reply about this plan, first text {label(event)}.'
            if not bare and not body.startswith('Work Wife'):
                body = 'Work Wife: ' + body
            self.messages.append((phone, body, event['code'] if event else None,
                                  event['version'] if event else None))

    def broadcast(self, e, body):
        for phone, member in e['members'].items():
            if member['rsvp'] != 'declined':
                self.say(phone, f"[{e['title']}] {body}", e)

    def details(self, e):
        if date_plan(e):
            start = stamp(e['slot']).astimezone(ZoneInfo(e['timezone'])).date()
            end = event_end(e).date() - timedelta(days=1)
            return f"{e['title']}, {start:%b %d, %Y}–{end:%b %d, %Y}, {duration_text(e)}, {e['location']}; times not set"
        when = stamp(e['slot']).astimezone(ZoneInfo(e['timezone'])).strftime('%a %b %d, %I:%M %p %Z')
        return f"{e['title']}, {when}, {e['minutes']} min, {e['location']}"

    def reset(self, e):
        e['version'] += 1
        e['slot'] = None
        e['status'] = 'collecting'
        for m in e['members'].values():
            if m['rsvp'] != 'declined':
                m['rsvp'] = 'pending'

    def match(self, e):
        members = list(e['members'].values())
        if len(members) != e['expected'] or any(m['rsvp'] == 'declined' for m in members):
            return
        slot = match_dates(e, self.now) if date_plan(e) else overlap([m['windows'] for m in members], e['minutes'], self.now)
        if not slot:
            if all(m.get('date_windows' if date_plan(e) else 'windows') for m in members):
                self.broadcast(e, 'No shared time yet. Please text any additional availability, including your earlier options.')
            return
        e['slot'], e['status'] = slot, 'proposed'
        self.broadcast(e, f"A time works for everyone: {self.details(e)}. Reply YES to confirm or NO if it no longer works. To select this plan, text {label(e)}.")

    def calendar_links(self, event):
        event.setdefault('calendar_token', secrets.token_urlsafe(32))
        event['calendar_generated_at'] = self.now.isoformat()
        self.broadcast(event, 'Add this confirmed plan to your calendar: '
                       f"https://work-wife-app.vercel.app/api/calendar?token={event['calendar_token']} "
                       'Open the calendar file and save it. Changes and cancellations still arrive by text; saved events do not sync automatically.')

    def record_consent(self, phone, status):
        record = {'status': status, 'at': self.now.isoformat(), 'version': CONSENT_VERSION}
        self.state['consents'][phone] = record
        self.state['consent_log'].append({'phone': phone, **record})

    def receive(self, phone, body):
        body = body.strip()
        upper = body.upper()
        consent = self.state['consents'].get(phone, {})
        if consent.get('status') == 'pending' and self.now - stamp(consent['at']) > timedelta(hours=24):
            self.state['pending_invitations'].pop(phone, None)
        if upper in ('STOP', 'STOPALL', 'UNSUBSCRIBE', 'END', 'QUIT', 'REVOKE', 'OPTOUT'):
            self.record_consent(phone, 'withdrawn')
            self.state['threads'].pop(phone, None)
            self.state['pending_invitations'].pop(phone, None)
            if phone not in self.state['blocked']:
                self.state['blocked'].append(phone)
            for e in self.state['events'].values():
                if phone in e['members'] and e['status'] != 'cancelled':
                    e['members'][phone]['rsvp'] = 'declined'
                    self.say(e['owner'], f"[{e['title']}] A participant opted out and won't receive plan messages.")
            return  # The messaging provider supplies the standard opt-out response.
        if upper in ('START', 'UNSTOP'):
            self.state['blocked'] = [p for p in self.state['blocked'] if p != phone]
            self.record_consent(phone, 'pending')
            self.say(phone, WELCOME)
            return
        if phone in self.state['blocked']:
            return
        if upper == 'HELP':
            self.say(phone, HELP)
            return
        if upper == 'JOIN':
            if enrolled(self.state, phone):
                self.say(phone, ENROLLED)
            else:
                self.record_consent(phone, 'pending')
                self.say(phone, WELCOME)
            return
        if upper == 'Y':
            if consent.get('status') == 'pending' and self.now - stamp(consent['at']) <= timedelta(hours=24):
                self.record_consent(phone, 'enrolled')
                self.say(phone, ENROLLED)
                pending = self.state['pending_invitations'].pop(phone, None)
                if pending:
                    checkpoint = json.dumps(self.state)
                    message_count = len(self.messages)
                    try:
                        self.receive(phone, pending)
                    except (ValueError, KeyError, TypeError):
                        self.state.clear()
                        self.state.update(json.loads(checkpoint))
                        del self.messages[message_count:]
                        self.say(phone, 'Enrollment is saved. Please resend your invitation code and availability so I can read the plan safely.')
            elif enrolled(self.state, phone):
                self.say(phone, ENROLLED)
            else:
                self.say(phone, INVITE)
            return
        if not enrolled(self.state, phone):
            references = {key for key, event in self.state['events'].items()
                          if any(t.strip('[],.:;').lower() in (key.lower(), label(event).lower()) for t in body.split())}
            if len(references) == 1:
                self.state['pending_invitations'][phone] = body
                self.record_consent(phone, 'pending')
                self.say(phone, WELCOME)
                return
            self.say(phone, INVITE)
            return
        events = self.state['events']
        tokens = body.split()
        found = {key for key, event in events.items()
                 if any(token.strip('[],.:;').lower() in (key.lower(), label(event).lower()) for token in tokens)}
        if len(found) > 1:
            self.say(phone, 'Please select one plan at a time. Text PLANS to see yours.')
            return
        code = next(iter(found), None)
        active = [e for e in events.values() if phone in e['members'] and e['status'] != 'cancelled'
                  and (not e['slot'] or event_end(e) > self.now)]
        if upper in ('STATUS', 'PLANS'):
            selected = self.state['threads'].get(phone)
            self.say(phone, '\n'.join(f"{'Active: ' if selected == e['code'] else ''}{e['title']} — {e['status']}. Switch: {label(e)}" for e in active)
                     or 'No active plans. Tell me what you want to organize.')
            return
        new = upper.startswith('NEW ')
        named = body[7:].strip() if upper.startswith('SWITCH ') else body
        titles = [event for event in active if event['title'].casefold() == named.casefold()]
        if not code and len(titles) == 1:
            code = titles[0]['code']
            tokens = []
        elif not code and (upper.startswith('SWITCH ') or len(titles) > 1):
            self.say(phone, 'Select a plan by its invitation code: ' + ', '.join(label(e) for e in active))
            return
        selected = self.state['threads'].get(phone)
        if selected and not any(e['code'] == selected for e in active):
            self.state['threads'].pop(phone, None)
            self.state['pending_invitations'].pop(phone, None)
            if not code and not new:
                self.say(phone, 'Your last plan has ended or was canceled. Text PLANS to select another, or NEW followed by a new plan.')
                return
        if not code and not new and self.state['threads'].get(phone):
            e = events[self.state['threads'][phone]]
        elif code:
            e = events[code]
        elif not new and len(active) == 1:
            e = active[0]
        elif not new and len(active) > 1:
            self.say(phone, 'Which plan? Select it once: ' + ', '.join(label(e) for e in active))
            return
        else:
            e = None
        text = ' '.join(t for t in tokens if t.strip('[],.:;').lower() not in (code.lower(), label(e).lower(), 'switch')) if code else body
        if e and (e['status'] == 'cancelled' or (e['slot'] and event_end(e) <= self.now)):
            self.say(phone, 'That plan has ended or was canceled. Text PLANS or NEW and describe a new plan.')
            return
        if e and phone not in e['members']:
            if e['status'] != 'collecting' or len(e['members']) >= e['expected']:
                self.say(phone, 'This plan is already full or scheduled. Please check with the organizer.')
                return
            e['members'][phone] = {'name': 'A participant', 'windows': [], 'rsvp': 'pending'}
            self.say(phone, f"Joined {e['title']}. Just reply here; no need to repeat the invitation name. Text PLANS to switch. Times use {e['timezone']}. Reply STOP to opt out. Your availability is used only to find a shared time.")
        if e:
            self.state['threads'][phone] = e['code']
            if not text:
                self.say(phone, f"You're chatting about {e['title']}. Just reply here; no need to repeat the invitation name. Text PLANS to switch. "
                         + ('Reply YES or NO for the proposed time.' if e['status'] in ('proposed', 'confirmed') else 'When are you free?'))
                return
        calendar_reply = text.strip().upper().rstrip('.!')
        if e and phone == e['owner'] and (
            calendar_reply in ('CALENDAR', 'YES CALENDAR', 'ADD TO CALENDAR', 'NOT NOW', 'SKIP CALENDAR', 'NO CALENDAR')
            or (e.get('calendar_question_pending') and e['status'] == 'collecting' and calendar_reply in ('YES', 'NO'))
        ):
            e['calendar_question_pending'] = False
            e['calendar_choice'] = calendar_reply not in ('NOT NOW', 'SKIP CALENDAR', 'NO CALENDAR', 'NO')
            if e['calendar_choice']:
                self.say(phone, 'Everyone will get an add-to-calendar link once the plan is confirmed. Each person saves it themselves; no calendar connection is needed.')
                if e['status'] == 'confirmed':
                    self.calendar_links(e)
            else:
                self.say(phone, "No problem—we'll keep this in texts. You can ask for CALENDAR later.")
            return
        if e and phone == e['owner']:
            e['calendar_question_pending'] = False
        if e and text.upper() == 'CANCEL PLAN':
            if phone != e['owner']:
                self.say(phone, 'Only the organizer can cancel the whole plan. Text NO to cancel your attendance.')
                return
            e['version'] += 1
            e['status'] = 'cancelled'
            self.broadcast(e, f"{e['title']} has been cancelled by the organizer. Reminders stopped.")
            return
        if e and text.upper() in ('YES', 'NO'):
            parsed = {'intent': 'confirm' if text.upper() == 'YES' else 'decline'}
        else:
            draft = self.state['drafts'].get(phone, []) if not e else e['members'][phone].get('clarification', [])
            context = {'now': self.now.isoformat(), 'event': {k: e.get(k) for k in ('title', 'expected', 'location', 'timezone', 'minutes', 'status', 'planning_window', 'schedule_mode', 'duration_unit', 'duration_count', 'date_bounds')} if e else None,
                       'current_availability': e['members'][phone] if e else None,
                       'conversation': draft}
            parsed = self.parser(text, context)
        intent = parsed['intent']
        if not e:
            if intent != 'create' or parsed.get('question'):
                history = self.state['drafts'].setdefault(phone, [])
                question = parsed.get('question') or 'What are we planning, how many adults will reply including you, and which week?'
                history.extend([{'user': text}, {'assistant': question}])
                self.state['drafts'][phone] = history[-12:]
                self.say(phone, question)
                return
            count, minutes = parsed['expected'], parsed['minutes']
            mode = parsed.get('schedule_mode', 'timed')
            unit = parsed.get('duration_unit', 'minutes')
            duration = parsed.get('duration_count', minutes)
            bounds = parsed.get('date_bounds', [])
            if mode not in ('timed', 'dates'):
                raise ValueError('Invalid plan mode')
            if mode == 'dates':
                if unit not in ('days', 'nights') or type(duration) is not int or not 1 <= duration <= 30:
                    raise ValueError('Invalid calendar duration')
                minutes = 0
            if bounds:
                from datetime import date
                if len(bounds) != 2 or date.fromisoformat(bounds[1]) < date.fromisoformat(bounds[0]):
                    raise ValueError('Invalid date bounds')
            if not isinstance(count, int) or not 2 <= count <= 12 or (mode == 'timed' and (not isinstance(minutes, int) or not 15 <= minutes <= 240)):
                raise ValueError('Invalid group size or duration')
            ZoneInfo(parsed['timezone'])
            code = plan_handle(parsed['title'], events)
            e = {'code': code, 'handle': code, 'owner': phone, 'title': parsed['title'][:80], 'expected': count,
                 'schedule_mode': mode, 'duration_unit': unit, 'duration_count': duration, 'date_bounds': bounds, 'activity': parsed.get('activity', ''),
                 'minutes': minutes, 'timezone': parsed['timezone'], 'location': parsed['location'] or 'Location to follow',
                 'planning_window': parsed.get('planning_window', ''), 'status': 'collecting', 'slot': None, 'version': 1, 'reminders': [],
                 'calendar_choice': None, 'calendar_question_pending': True, 'members': {phone: {'name': parsed.get('name') or 'Organizer', 'windows': [], 'rsvp': 'pending'}}}
            events[code] = e
            self.state['threads'][phone] = code
            self.state['drafts'].pop(phone, None)
            from transport import phone_number
            number = phone_number() or '[scheduling number]'
            self.say(phone, label(e), bare=True)
            self.say(phone, f"Copy this invitation into your group chat:\n{e['title']} — {duration_text(e)}. "
                     f"{e['planning_window']}\nText {label(e)} to {number}, then tell Work Wife when you're free. "
                     "New participants will be asked to enroll first. Work Wife coordinates privately with each person.")
            self.say(phone, 'Would you like an add-to-calendar link for everyone once the time is confirmed? Reply CALENDAR or NOT NOW. No calendar connection needed; each person saves it themselves.')
            if not parsed.get('date_windows' if date_plan(e) else 'windows'):
                self.say(phone, f"[{e['title']}] When are you free? " + ('Tell me which dates work; no clock times needed.' if date_plan(e) else 'Tell me dates and a time range in your own words.'))
                return
            intent = 'availability'
        if parsed.get('question'):
            member = e['members'][phone]
            member['clarification'] = (member.get('clarification', []) + [{'user': text}, {'assistant': parsed['question']}])[-12:]
            self.say(phone, parsed['question'])
            return
        if intent == 'confirm':
            if e['status'] not in ('proposed', 'confirmed') or stamp(e['slot']) <= self.now:
                self.say(phone, 'There is no current time to confirm. Please text your availability.')
                return
            member = e['members'][phone]
            if member['rsvp'] == 'declined':
                self.say(phone, 'You previously declined. Send your availability again to rejoin before confirming.')
                return
            member['rsvp'] = 'yes'
            self.say(phone, f"[{e['title']}] Your YES is saved.")
            if e['status'] != 'confirmed' and all(m['rsvp'] == 'yes' for m in e['members'].values()):
                e['status'] = 'confirmed'
                self.broadcast(e, f"It's on! {self.details(e)}. We'll send reminders before the plan. Text if you can't come.")
                if e.get('calendar_choice'):
                    self.calendar_links(e)
        elif intent == 'decline':
            e['members'][phone]['rsvp'] = 'declined'
            e['members'][phone]['windows'] = []
            e['members'][phone]['date_windows'] = []
            self.say(phone, f"[{e['title']}] You're marked as not coming. Your reminders are stopped.")
            self.say(e['owner'], f"[{e['title']}] {e['members'][phone]['name']} can't make it. The plan has not been rescheduled. Text RESCHEDULE to ask everyone again.")
        elif intent == 'reschedule':
            if phone != e['owner']:
                self.say(phone, 'Ask the organizer to reschedule, or text NO to decline.')
                return
            self.reset(e)
            for m in e['members'].values():
                m.update(windows=[], date_windows=[], rsvp='pending', clarification=[])
            self.broadcast(e, 'We are finding a new time. Please text your availability again; the previous time is no longer confirmed.')
        elif intent == 'cancel_event':
            self.say(phone, f"To cancel the whole plan, reply CANCEL PLAN. To cancel just your attendance, reply NO.")
        elif intent == 'availability':
            if date_plan(e):
                normalized = normalize_ranges(parsed.get('date_windows', []), e, self.now)
                action = parsed.get('availability_action', 'replace')
                current = e['members'][phone].get('date_windows', [])
                if action in ('add', 'remove'):
                    if not current:
                        self.say(phone, 'I do not have your dates yet. Which dates could work for you?')
                        return
                    existing = {a for a, b in current}
                    affected = {a for a, b in normalized}
                    remaining = existing | affected if action == 'add' else existing - affected
                    normalized = [[d, d] for d in sorted(remaining)]
                elif action != 'replace':
                    raise ValueError('Invalid availability action')
            else:
                windows = parsed.get('windows', [])
                if not windows or len(windows) > 40:
                    raise ValueError('Availability missing')
                normalized = []
                for a, b in windows:
                    start, end = stamp(a), stamp(b)
                    if start <= self.now or end <= start or end > self.now + timedelta(days=90):
                        raise ValueError('Invalid availability')
                    normalized.append([start.isoformat(), end.isoformat()])
            if e['status'] in ('proposed', 'confirmed'):
                self.reset(e)
                self.broadcast(e, 'Availability changed. The previous time is no longer confirmed; checking again.')
            member = e['members'][phone]
            member.update(rsvp='pending', clarification=[], tentative=parsed.get('tentative', False))
            member['date_windows' if date_plan(e) else 'windows'] = normalized
            if parsed.get('name'):
                member['name'] = parsed['name'][:40]
            tz = ZoneInfo(e['timezone'])
            summary = ', '.join(a for a, b in normalized) if date_plan(e) else '; '.join(f"{stamp(a).astimezone(tz):%a %b %d %I:%M %p}–{stamp(b).astimezone(tz):%I:%M %p %Z}" for a, b in normalized)
            summary = summary or 'no available dates remaining; send other dates when you can'
            self.say(phone, f"[{e['title']}] I read your availability as: {summary}{' (tentative)' if member.get('tentative') else ''}. Text a correction anytime.")
            self.match(e)
        else:
            self.say(phone, f"[{e['title']}] Tell me when you're free, YES to confirm, NO if you can't come, or RESCHEDULE to find a new time.")
