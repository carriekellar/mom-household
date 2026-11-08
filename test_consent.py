"""Enrollment must precede parsing, scheduling, and delivery in both stores."""
import json
import unittest
from datetime import timedelta
from unittest.mock import Mock
from household import Store, Scheduler, WELCOME, ENROLLED, HELP, INVITE
from test_household import NOW, create
from transport import deliver


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.db.close)
        self.sequence = 0
        self.parser = Mock(return_value=create())

    def text(self, body, now=NOW, sid=None):
        self.sequence += 1
        self.store.enqueue(sid or str(self.sequence), 'alex', body)
        self.store.process(self.parser, now)

    def last(self):
        return self.store.db.execute('SELECT body FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0]

    def test_no_enrollment_from_free_text_yes_or_unprompted_y(self):
        for text in ('Plan a playdate', 'YES', 'Y'):
            self.text(text)
            self.assertEqual(self.last(), INVITE)
        self.parser.assert_not_called()
        self.assertEqual(self.store.load()['events'], {})

    def test_join_y_records_consent_and_duplicate_webhook_is_idempotent(self):
        self.text('JOIN')
        self.assertEqual(self.last(), WELCOME)
        self.text('YES')
        self.assertEqual(self.store.load()['consents']['alex']['status'], 'pending')
        self.text('Y', sid='confirmation')
        self.text('Y', sid='confirmation')
        self.assertEqual(self.last(), ENROLLED)
        log = self.store.load()['consent_log']
        self.assertEqual([e['status'] for e in log], ['pending', 'enrolled'])
        self.assertEqual(log[-1]['version'], '2026-11-08')
        self.parser.assert_not_called()
        self.text('Plan a playdate')
        self.assertEqual(len(self.store.load()['events']), 1)

    def test_pending_confirmation_expires(self):
        self.text('JOIN')
        self.text('Y', NOW + timedelta(hours=25))
        self.assertEqual(self.last(), INVITE)
        self.assertEqual(self.store.load()['consents']['alex']['status'], 'pending')

    def test_stop_restart_does_not_revive_queued_messages_or_consent(self):
        for text in ('JOIN', 'Y', 'Plan', 'STOP', 'START'):
            self.text(text)
        self.assertEqual(self.store.load()['consents']['alex']['status'], 'pending')
        sent = []
        while deliver(self.store, lambda p, b: sent.append(b) or 'sent'):
            pass
        self.assertEqual(sent, [WELCOME])
        self.text('Y')
        self.assertEqual(self.store.load()['consents']['alex']['status'], 'enrolled')
        event = next(iter(self.store.load()['events'].values()))
        self.assertEqual(event['members']['alex']['rsvp'], 'declined')

    def test_blocked_join_cannot_bypass_provider_restart(self):
        self.text('STOP')
        self.text('JOIN')
        self.text('Y')
        self.assertEqual(self.store.load()['consents']['alex']['status'], 'withdrawn')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    def test_help_does_not_enroll_or_call_ai(self):
        self.text('HELP')
        self.assertEqual(self.last(), HELP)
        self.assertNotIn('alex', self.store.load()['consents'])
        self.parser.assert_not_called()

    def test_legacy_state_does_not_imply_consent_and_suppresses_old_queue(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO outbox(phone,body) VALUES('alex','old reminder')")
        sender = Mock()
        self.assertTrue(deliver(self.store, sender))
        sender.assert_not_called()
        app = Scheduler({'events': {}, 'blocked': [], 'drafts': {}}, self.parser, NOW)
        app.receive('alex', 'Plan')
        self.parser.assert_not_called()
        self.assertEqual(app.messages[0][1], INVITE)
