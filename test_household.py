import base64
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from household import Store, UTC, overlap
from transport import deliver, verify

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
WINDOW = ['2026-10-24T10:00:00-04:00', '2026-10-24T12:00:00-04:00']


def create(**extra):
    return dict(intent='create', title='Park playdate', expected=3, minutes=60,
                timezone='America/New_York', location='Prospect Park', name='Alex',
                planning_window='October 24, 2026', windows=[WINDOW], **extra)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.counter = 0

    def tearDown(self):
        self.store.db.close()

    def text(self, phone, body, result=None, sid=None):
        self.counter += 1
        self.store.enqueue(sid or str(self.counter), phone, body)
        self.store.process(lambda *_: result, NOW)

    def plan(self):
        self.text('alex', 'Plan a playdate', create())
        return next(iter(self.store.load()['events']))

    def event(self, code):
        return self.store.load()['events'][code]

    def proposed(self):
        code = self.plan()
        for person in ('sarah', 'jess'):
            self.text(person, code + ' Saturday 10 to noon',
                      {'intent': 'availability', 'name': person, 'windows': [WINDOW]})
        return code

    def confirmed(self):
        code = self.proposed()
        for person in ('alex', 'sarah', 'jess'):
            self.text(person, 'YES')
        return code

    def test_end_to_end_and_reminders_once(self):
        code = self.confirmed()
        self.assertEqual(self.event(code)['status'], 'confirmed')
        due = datetime(2026, 10, 23, 14, tzinfo=UTC)
        self.store.remind(due)
        self.store.remind(due)
        count = self.store.db.execute("SELECT count(*) FROM outbox WHERE body LIKE '%Reminder:%'").fetchone()[0]
        self.assertEqual(count, 3)
        self.store.remind(due + timedelta(hours=23))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE body LIKE '%Reminder:%'").fetchone()[0], 6)

    def test_waits_for_everyone(self):
        code = self.plan()
        self.text('sarah', code, {'intent': 'availability', 'windows': [WINDOW]})
        self.assertEqual(self.event(code)['status'], 'collecting')

    def test_no_overlap_does_not_propose(self):
        code = self.plan()
        self.text('sarah', code, {'intent': 'availability', 'windows': [WINDOW]})
        self.text('jess', code, {'intent': 'availability', 'windows': [['2026-10-25T10:00:00-04:00', '2026-10-25T12:00:00-04:00']]})
        self.assertIsNone(self.event(code)['slot'])

    def test_cancellation_keeps_group_plan_and_stops_reminders(self):
        code = self.confirmed()
        self.text('sarah', "Can't come", {'intent': 'decline'})
        self.assertEqual(self.event(code)['status'], 'confirmed')
        self.store.remind(datetime(2026, 10, 23, 14, tzinfo=UTC))
        phones = [r[0] for r in self.store.db.execute("SELECT phone FROM outbox WHERE body LIKE '%Reminder:%'")]
        self.assertEqual(set(phones), {'alex', 'jess'})

    def test_opt_out_and_stale_queued_sends(self):
        code = self.confirmed()
        self.text('sarah', 'STOP')
        sent = []
        while deliver(self.store, lambda p, t: sent.append((p, t)) or 'id'):
            pass
        self.assertFalse(any(p == 'sarah' for p, _ in sent))
        self.text('sarah', 'START')
        self.assertNotIn('sarah', self.store.load()['blocked'])
        self.assertEqual(self.event(code)['members']['sarah']['rsvp'], 'declined')

    def test_duplicate_webhook(self):
        self.text('alex', 'Plan', create(), sid='same')
        self.text('alex', 'Plan', create(), sid='same')
        self.assertEqual(len(self.store.load()['events']), 1)

    def test_reschedule_invalidates_confirmation_and_old_messages(self):
        code = self.confirmed()
        self.text('alex', 'Find another time', {'intent': 'reschedule'})
        e = self.event(code)
        self.assertEqual(e['status'], 'collecting')
        self.assertTrue(all(not m['windows'] for m in e['members'].values()))
        sent = []
        while deliver(self.store, lambda p, t: sent.append(t) or 'id'):
            pass
        self.assertFalse(any("It's on!" in t for t in sent))

    def test_only_owner_cancels_plan(self):
        code = self.confirmed()
        self.text('sarah', 'CANCEL PLAN')
        self.assertEqual(self.event(code)['status'], 'confirmed')
        self.text('alex', 'CANCEL PLAN')
        self.assertEqual(self.event(code)['status'], 'cancelled')

    def test_ambiguous_plan_requires_code(self):
        first = self.plan()
        self.text('alex', 'NEW dinner', create())
        self.text('alex', 'YES')
        self.assertEqual(self.event(first)['members']['alex']['rsvp'], 'pending')
        self.assertIn('Which plan?', self.store.db.execute('SELECT body FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])

    def test_invalid_output_rolls_back_membership(self):
        code = self.plan()
        self.text('sarah', code, {'intent': 'availability', 'windows': [['yesterday', 'tomorrow']]})
        self.assertNotIn('sarah', self.event(code)['members'])

    def test_changed_availability_requires_new_confirmations(self):
        code = self.confirmed()
        self.text('sarah', 'Actually 11 to noon', {'intent': 'availability', 'windows': [['2026-10-24T11:00:00-04:00', WINDOW[1]]]})
        self.assertEqual(self.event(code)['status'], 'proposed')
        self.assertTrue(all(m['rsvp'] == 'pending' for m in self.event(code)['members'].values()))

    def test_transport_timeout_is_not_blindly_retried(self):
        self.plan()
        def fail(*_):
            raise TimeoutError()
        deliver(self.store, fail)
        self.assertEqual(self.store.db.execute('SELECT status FROM outbox ORDER BY id LIMIT 1').fetchone()[0], 'needs_review')

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder + '/db')
            store.enqueue('one', 'alex', 'Plan')
            store.process(lambda *_: create(), NOW)
            store.db.close()
            reopened = Store(folder + '/db')
            self.assertEqual(len(reopened.load()['events']), 1)
            reopened.db.close()

    def test_timezone_overlap_and_duration(self):
        west = ['2026-10-24T07:30:00-07:00', '2026-10-24T09:00:00-07:00']
        self.assertEqual(overlap([[WINDOW], [west]], 60, NOW), '2026-10-24T14:30:00+00:00')
        self.assertIsNone(overlap([[WINDOW], [west]], 120, NOW))

    def test_creation_clarification_is_retained(self):
        self.text('alex', 'Playdate next week', {'intent': 'unknown', 'question': 'How many adults including you?'})
        history = self.store.load()['drafts']['alex']
        self.assertIn('Playdate next week', history[0]['user'])


class SecurityTests(unittest.TestCase):
    def test_signature_tamper_and_replay(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        body = b'{"test":true}'
        signature = base64.b64encode(key.sign(b'1000|' + body)).decode()
        headers = {'telnyx-timestamp': '1000', 'telnyx-signature-ed25519': signature}
        verify(body, headers, public, now=1001)
        with self.assertRaises(Exception):
            verify(body + b' ', headers, public, now=1001)
        with self.assertRaises(ValueError):
            verify(body, headers, public, now=1400)


if __name__ == '__main__':
    unittest.main()
