"""Small SMS scheduling domain. Python 3.11+, standard library only."""
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


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
            app.say(row['phone'], 'I could not safely read that. Please resend with specific dates and start/end times.')
        with self.db:
            self.db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
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
                remaining = (stamp(e['slot']) - now).total_seconds()
                # Narrow windows avoid sending stale reminders after downtime.
                for hours in (24, 1):
                    key = f"{e['version']}:{hours}"
                    if hours * 3600 - 900 <= remaining <= hours * 3600 and key not in e['reminders']:
                        app.broadcast(e, f"Reminder: {app.details(e)}. Can't make it? Text {e['code']} can't come.")
                        e['reminders'].append(key)
            self.db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
            self.save_messages(app.messages)


class Scheduler:
    def __init__(self, state, parser, now):
        self.state, self.parser, self.now = state, parser, now
        self.messages = []

    def say(self, phone, body, event=None):
        if phone not in self.state['blocked']:
            self.messages.append((phone, body, event['code'] if event else None,
                                  event['version'] if event else None))

    def broadcast(self, e, body):
        for phone, member in e['members'].items():
            if member['rsvp'] != 'declined':
                self.say(phone, f"[{e['code']}] {body}", e)

    def details(self, e):
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
        slot = overlap([m['windows'] for m in members], e['minutes'], self.now)
        if not slot:
            if all(m['windows'] for m in members):
                self.broadcast(e, 'No shared time yet. Please text any additional availability, including your earlier options.')
            return
        e['slot'], e['status'] = slot, 'proposed'
        self.broadcast(e, f"A time works for everyone: {self.details(e)}. Reply {e['code']} YES to confirm or NO if it no longer works.")

    def receive(self, phone, body):
        body = body.strip()
        upper = body.upper()
        if upper in ('STOP', 'STOPALL', 'UNSUBSCRIBE', 'END', 'QUIT', 'REVOKE', 'OPTOUT'):
            if phone not in self.state['blocked']:
                self.state['blocked'].append(phone)
            for e in self.state['events'].values():
                if phone in e['members'] and e['status'] != 'cancelled':
                    e['members'][phone]['rsvp'] = 'declined'
                    self.say(e['owner'], f"[{e['code']}] A participant opted out and won't receive plan messages.")
            return  # The messaging provider supplies the standard opt-out response.
        if upper in ('START', 'UNSTOP'):
            self.state['blocked'] = [p for p in self.state['blocked'] if p != phone]
            self.say(phone, 'Messages enabled. Text your plan code to rejoin a plan. Reply STOP to opt out.')
            return
        if phone in self.state['blocked']:
            return
        if upper == 'HELP':
            self.say(phone, 'Mom Household: describe a plan or text its code and your availability. YES confirms; NO declines; STATUS shows your plans. STOP opts out.')
            return
        events = self.state['events']
        tokens = upper.split()
        code = next((t for t in tokens if t in events), None)
        active = [e for e in events.values() if phone in e['members'] and e['status'] != 'cancelled'
                  and (not e['slot'] or stamp(e['slot']) + timedelta(minutes=e['minutes']) > self.now)]
        if upper == 'STATUS':
            self.say(phone, '\n'.join(f"{e['code']}: {e['title']} — {e['status']}" for e in active) or 'No active plans. Tell me what you want to organize.')
            return
        new = upper.startswith('NEW ')
        if not code and len(active) > 1 and not new:
            self.say(phone, 'Which plan? Resend with its code: ' + ', '.join(f"{e['code']} ({e['title']})" for e in active))
            return
        e = events[code] if code else (active[0] if len(active) == 1 and not new else None)
        text = ' '.join(t for t in body.split() if t.upper() != code) if code else body
        if e and e['status'] == 'cancelled':
            self.say(phone, 'That plan is cancelled. Text NEW and describe a new plan.')
            return
        if e and phone not in e['members']:
            if e['status'] != 'collecting' or len(e['members']) >= e['expected']:
                self.say(phone, 'This plan is already full or scheduled. Please check with the organizer.')
                return
            e['members'][phone] = {'name': 'A participant', 'windows': [], 'rsvp': 'pending'}
            self.say(phone, f"Joined {e['title']}. Times use {e['timezone']}. Reply STOP to opt out. Your availability is used only to find a shared time.")
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
            context = {'now': self.now.isoformat(), 'event': {k: e[k] for k in ('title', 'timezone', 'minutes', 'status', 'planning_window')} if e else None,
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
            if not isinstance(count, int) or not 2 <= count <= 12 or not isinstance(minutes, int) or not 15 <= minutes <= 240:
                raise ValueError('Invalid group size or duration')
            ZoneInfo(parsed['timezone'])
            code = secrets.token_hex(3).upper()
            while code in events:
                code = secrets.token_hex(3).upper()
            e = {'code': code, 'owner': phone, 'title': parsed['title'][:80], 'expected': count,
                 'minutes': minutes, 'timezone': parsed['timezone'], 'location': parsed['location'] or 'Location to follow',
                 'planning_window': parsed.get('planning_window', ''), 'status': 'collecting', 'slot': None, 'version': 1, 'reminders': [],
                 'members': {phone: {'name': parsed.get('name') or 'Organizer', 'windows': [], 'rsvp': 'pending'}}}
            events[code] = e
            self.state['drafts'].pop(phone, None)
            self.say(phone, f"[{code}] {e['title']} for {count} adults replying, {minutes} min. Times: {e['timezone']}. Forward: Text {code} and when you're free to this number. Reply STOP to opt out.")
            if not parsed.get('windows'):
                self.say(phone, f"[{code}] When are you free? Tell me dates and a time range in your own words.")
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
            self.say(phone, f"[{e['code']}] Your YES is saved.")
            if e['status'] != 'confirmed' and all(m['rsvp'] == 'yes' for m in e['members'].values()):
                e['status'] = 'confirmed'
                self.broadcast(e, f"It's on! {self.details(e)}. Reminders arrive the day before and an hour before. Text if you can't come.")
        elif intent == 'decline':
            e['members'][phone]['rsvp'] = 'declined'
            e['members'][phone]['windows'] = []
            self.say(phone, f"[{e['code']}] You're marked as not coming. Your reminders are stopped.")
            self.say(e['owner'], f"[{e['code']}] {e['members'][phone]['name']} can't make it. The plan has not been rescheduled. Text RESCHEDULE to ask everyone again.")
        elif intent == 'reschedule':
            if phone != e['owner']:
                self.say(phone, 'Ask the organizer to reschedule, or text NO to decline.')
                return
            self.reset(e)
            for m in e['members'].values():
                m.update(windows=[], rsvp='pending')
            self.broadcast(e, 'We are finding a new time. Please text your availability again; the previous time is no longer confirmed.')
        elif intent == 'cancel_event':
            self.say(phone, f"To cancel the whole plan, reply {e['code']} CANCEL PLAN. To cancel just your attendance, reply {e['code']} NO.")
        elif intent == 'availability':
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
            member.update(windows=normalized, rsvp='pending', clarification=[])
            if parsed.get('name'):
                member['name'] = parsed['name'][:40]
            tz = ZoneInfo(e['timezone'])
            summary = '; '.join(f"{stamp(a).astimezone(tz):%a %b %d %I:%M %p}–{stamp(b).astimezone(tz):%I:%M %p %Z}" for a, b in normalized)
            self.say(phone, f"[{e['code']}] I read your availability as: {summary}. Text a correction anytime.")
            self.match(e)
        else:
            self.say(phone, f"[{e['code']}] Tell me when you're free, YES to confirm, NO if you can't come, or RESCHEDULE to find a new time.")
