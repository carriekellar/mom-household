"""Run only against a disposable test DB: TEST_DATABASE_URL (never .env)."""
import os
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from household import UTC
from postgres import PostgresStore
from test_household import NOW, WINDOW, create


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL to a disposable Postgres database')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'DATABASE_URL': os.environ['TEST_DATABASE_URL']})
        self.env.start()
        self.store = PostgresStore()
        self.store.db.execute(Path('supabase/migrations/202610210001_household.sql').read_text())
        self.store.db.execute('TRUNCATE inbox,outbox,receipts RESTART IDENTITY')
        self.store.db.execute('UPDATE state SET body=%s::jsonb', (json.dumps({'events': {}, 'drafts': {}, 'blocked': []}),))
        self.lock = self.store.worker()
        self.assertTrue(self.lock.__enter__())
        self.counter = 0

    def tearDown(self):
        self.lock.__exit__(None, None, None)
        self.store.db.close()
        self.env.stop()

    def text(self, phone, body, result=None):
        self.counter += 1
        self.store.enqueue(str(self.counter), phone, body)
        self.store.process(lambda *_: result, NOW)

    def test_full_flow_cancellation_and_reminder_deduplication(self):
        self.text('alex', 'Playdate', create())
        code = next(iter(self.store.load()['events']))
        for person in ('sarah', 'jess'):
            self.text(person, code, {'intent': 'availability', 'windows': [WINDOW]})
        for person in ('alex', 'sarah', 'jess'):
            self.text(person, 'YES')
        self.assertEqual(self.store.load()['events'][code]['status'], 'confirmed')
        self.text('sarah', "Can't come", {'intent': 'decline'})
        due = datetime(2026, 10, 23, 14, tzinfo=UTC)
        self.store.remind(due)
        self.store.remind(due)
        reminders = self.store.db.execute("SELECT phone FROM outbox WHERE body LIKE '%Reminder:%'").fetchall()
        self.assertEqual({r['phone'] for r in reminders}, {'alex', 'jess'})
        self.assertEqual(len(reminders), 2)
        self.text('alex', 'CANCEL PLAN')
        self.assertEqual(self.store.load()['events'][code]['status'], 'cancelled')
        sent = []
        while self.store.deliver(lambda p, b: sent.append(b) or str(len(sent))):
            pass
        self.assertFalse(any('Reminder:' in body for body in sent))

    def test_duplicate_enqueue_and_worker_exclusion(self):
        self.store.enqueue('same', 'alex', 'Playdate')
        self.store.enqueue('same', 'alex', 'Playdate')
        other = PostgresStore()
        try:
            with other.worker() as locked:
                self.assertFalse(locked)
            other.enqueue('second', 'sarah', 'hello')
        finally:
            other.db.close()
        self.store.process(lambda *_: create(), NOW)
        self.assertEqual(self.store.db.execute('SELECT count(*) AS n FROM inbox').fetchone()['n'], 2)
        self.assertEqual(len(self.store.load()['events']), 1)

    def test_early_delivery_receipt_and_ambiguous_send(self):
        self.store.db.execute("INSERT INTO outbox(phone,body) VALUES('alex','hello'),('sarah','hello')")
        def sender(*_):
            self.store.finalized('provider-1', 'delivered')
            return 'provider-1'
        self.store.deliver(sender)
        self.assertEqual(self.store.db.execute('SELECT status FROM outbox WHERE id=1').fetchone()['status'], 'delivered')
        def fail(*_):
            raise TimeoutError()
        self.store.deliver(fail)
        self.assertFalse(self.store.deliver(fail))
        self.assertEqual(self.store.db.execute('SELECT status FROM outbox WHERE id=2').fetchone()['status'], 'needs_review')

    def test_provider_error_retry_limit_and_no_partial_state(self):
        self.store.enqueue('bad', 'alex', 'Playdate')
        def fail(*_):
            raise RuntimeError('provider unavailable')
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                self.store.process(fail, NOW)
        self.assertFalse(self.store.process(fail, NOW))
        self.assertEqual(self.store.load()['events'], {})
        self.assertEqual(self.store.db.execute('SELECT status FROM inbox').fetchone()['status'], 'needs_review')
