"""Supabase persistence. Requires a direct or SESSION-mode pooler connection."""
import json
import os
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from household import Scheduler, stamp
from transport import send


class PostgresStore:
    def __init__(self):
        self.db = psycopg.connect(os.environ['DATABASE_URL'], autocommit=True,
                                 row_factory=dict_row, connect_timeout=5)
        self.db.execute('SET search_path TO household, public')
        self.db.execute("SET statement_timeout = '10s'")

    def load(self):
        return self.db.execute('SELECT body FROM state WHERE id=1').fetchone()['body']

    def enqueue(self, sid, phone, body):
        self.db.execute('INSERT INTO inbox(sid,phone,body) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (sid, phone, body))

    def finalized(self, sid, status):
        # Retain callbacks even when they arrive before the send response.
        with self.db.transaction():
            self.db.execute('INSERT INTO receipts(sid,status) VALUES(%s,%s) ON CONFLICT(sid) DO UPDATE SET status=excluded.status', (sid, status))
            self.db.execute('UPDATE outbox SET status=%s WHERE provider_sid=%s', (status, sid))

    @contextmanager
    def worker(self):
        # Session lock survives per-message commits; never use transaction pooling.
        locked = self.db.execute('SELECT pg_try_advisory_lock(748219301) AS locked').fetchone()['locked']
        try:
            if locked:
                self.db.execute("UPDATE outbox SET status='needs_review' WHERE status='sending'")
            if locked:
                self.db.execute("UPDATE outbox o SET status=r.status FROM receipts r WHERE o.provider_sid=r.sid AND o.status IS DISTINCT FROM r.status")
            yield locked
        finally:
            if locked:
                self.db.execute('SELECT pg_advisory_unlock(748219301)')

    def save(self, state, messages):
        self.db.execute('UPDATE state SET body=%s::jsonb WHERE id=1', (json.dumps(state),))
        with self.db.cursor() as cursor:
            cursor.executemany('INSERT INTO outbox(phone,body,event,version) VALUES(%s,%s,%s,%s)', messages)

    def process(self, parser, now):
        row = self.db.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        state = self.load()
        app = Scheduler(state, parser, now)
        try:
            app.receive(row['phone'], row['body'])
        except (ValueError, KeyError, TypeError):
            state = self.load()
            app = Scheduler(state, parser, now)
            app.say(row['phone'], 'I could not safely read that. Please resend with specific dates and start/end times.')
        except Exception:
            self.db.execute("UPDATE inbox SET attempts=attempts+1, status=CASE WHEN attempts>=2 THEN 'needs_review' ELSE 'pending' END WHERE sid=%s", (row['sid'],))
            raise
        with self.db.transaction():
            self.save(state, app.messages)
            self.db.execute("UPDATE inbox SET status='done' WHERE sid=%s", (row['sid'],))
        return True

    def remind(self, now):
        state = self.load()
        app = Scheduler(state, None, now)
        for event in state['events'].values():
            if event['status'] != 'confirmed':
                continue
            remaining = (stamp(event['slot']) - now).total_seconds()
            for hours in (24, 1):
                key = f"{event['version']}:{hours}"
                if hours * 3600 - 900 <= remaining <= hours * 3600 and key not in event['reminders']:
                    app.broadcast(event, f"Reminder: {app.details(event)}. Can't make it? Text {event['code']} can't come.")
                    event['reminders'].append(key)
        with self.db.transaction():
            self.save(state, app.messages)

    def deliver(self, sender=send):
        row = self.db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        state = self.load()
        event = state['events'].get(row['event'])
        stale = event and (event['version'] != row['version'] or event['members'].get(row['phone'], {}).get('rsvp') == 'declined')
        if stale or row['phone'] in state['blocked']:
            self.db.execute("UPDATE outbox SET status='suppressed' WHERE id=%s", (row['id'],))
            return True
        self.db.execute("UPDATE outbox SET status='sending' WHERE id=%s", (row['id'],))
        try:
            sid = sender(row['phone'], row['body'])
        except Exception as exc:
            self.db.execute("UPDATE outbox SET status='needs_review',error=%s WHERE id=%s", (type(exc).__name__, row['id']))
        else:
            self.db.execute("UPDATE outbox SET provider_sid=%s,status=COALESCE((SELECT status FROM receipts WHERE sid=%s),'accepted') WHERE id=%s", (sid, sid, row['id']))
        return True
