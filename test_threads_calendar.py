import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
import test_household as fixtures
from test_household import create, WINDOW, NOW
from household import plan_handle
from calendar_export import calendar_bytes
from api.calendar import handler
import test_cloud as cloud_tests


class ThreadCalendarTests(unittest.TestCase):
    setUp = fixtures.SchedulingTests.setUp
    tearDown = fixtures.SchedulingTests.tearDown
    text = fixtures.SchedulingTests.text
    plan = fixtures.SchedulingTests.plan
    event = fixtures.SchedulingTests.event
    proposed = fixtures.SchedulingTests.proposed
    confirmed = fixtures.SchedulingTests.confirmed
    def test_readable_invitation_selects_once_and_new_plan_is_remembered(self):
        first = self.plan()
        self.text('alex', 'NOT NOW')
        other = create(); other['title'] = 'Parent dinner'
        self.text('alex', 'NEW dinner', other)
        second = self.store.load()['threads']['alex']
        self.assertNotEqual(first, second)
        self.assertRegex(self.event(first)['handle'], r'^[1-9][0-9]{5}$')
        self.assertEqual(first, self.event(first)['handle'])
        self.text('alex', 'Park playdate')
        self.assertEqual(self.store.load()['threads']['alex'], first)
        self.text('alex', '10 to noon', {'intent':'availability','windows':[WINDOW]})
        self.assertEqual(self.store.load()['threads']['alex'], first)
        self.text('sarah', self.event(first)['handle'].upper())
        self.text('sarah', '10 to noon', {'intent':'availability','windows':[WINDOW]})
        self.assertEqual(self.event(first)['members']['sarah']['windows'][0][0], '2026-10-24T14:00:00+00:00')

    def test_numeric_invitation_avoids_legacy_ids_and_handles(self):
        events = {'123456': {'code': '123456', 'handle': 'old-playdate'},
                  'ABCDEF': {'code': 'ABCDEF', 'handle': '234567'}}
        with patch('household.secrets.randbelow', side_effect=[23456, 134567, 245678]):
            self.assertEqual(plan_handle('Coffee', events), '345678')

    def test_legacy_invitation_still_joins(self):
        code = self.plan()
        state = self.store.load()
        state['events'][code]['handle'] = 'park-playdate-maple-river-48271'
        self.store.db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
        self.store.db.commit()
        self.text('sarah', 'park-playdate-maple-river-48271')
        self.assertEqual(self.store.load()['threads']['sarah'], code)

    def test_new_plan_followups_never_return_to_selected_plan(self):
        from unittest.mock import Mock
        from household import Scheduler
        old = self.plan()
        state = self.store.load()
        original = json.loads(json.dumps(state['events'][old]))
        parser = Mock(side_effect=[
            {'intent':'create', 'question':'How many adults including you?'},
            {'intent':'create', 'question':'What time is school pickup?'},
            create(),
        ])
        app = Scheduler(state, parser, NOW)
        app.receive('alex', 'NEW playdate next week after pickup')
        app.receive('alex', '3')
        app.receive('alex', '3pm to 5pm')
        self.assertEqual(len(state['events']), 2)
        self.assertEqual(state['events'][old], original)
        self.assertNotEqual(state['threads']['alex'], old)
        self.assertNotIn('alex', state['drafts'])
        for call in parser.call_args_list:
            self.assertIsNone(call.args[1]['event'])
        self.assertIn({'user':'3'}, parser.call_args_list[2].args[1]['conversation'])

    def test_explicit_switch_leaves_creation_and_new_restarts_it(self):
        from unittest.mock import Mock
        from household import Scheduler
        old = self.plan()
        state = self.store.load()
        parser = Mock(return_value={'intent':'create','question':'How many adults?'})
        app = Scheduler(state, parser, NOW)
        app.receive('alex','NEW dinner')
        app.receive('alex','PLANS')
        self.assertIn('alex',state['drafts'])
        app.receive('alex','NEW coffee')
        self.assertEqual(state['drafts']['alex'][0],{'user':'NEW coffee'})
        app.receive('alex',old)
        self.assertNotIn('alex',state['drafts'])
        self.assertEqual(state['threads']['alex'],old)

    def test_calendar_is_optional_and_only_sent_after_everyone_confirms(self):
        code = self.plan()
        self.text('alex', 'CALENDAR')
        for person in ('sarah','jess'):
            self.text(person, code+' Saturday', {'intent':'availability','windows':[WINDOW]})
        self.text('alex', 'YES'); self.text('sarah', 'YES')
        self.assertNotIn('calendar_token', self.event(code))
        self.text('jess', 'YES')
        self.assertEqual(len(self.event(code)['calendar_token']),43)
        links=self.store.db.execute("SELECT phone FROM outbox WHERE body LIKE '%api/calendar?token=%'").fetchall()
        self.assertEqual({r[0] for r in links},{'alex','sarah','jess'})

    def test_not_now_does_not_decline_and_can_opt_in_later(self):
        code=self.plan();self.text('alex','not now')
        self.assertFalse(self.event(code)['calendar_choice'])
        self.assertEqual(self.event(code)['members']['alex']['rsvp'],'pending')
        for person in ('sarah','jess'):
            self.text(person,code+' Saturday',{'intent':'availability','windows':[WINDOW]})
        for person in ('alex','sarah','jess'):self.text(person,'YES')
        self.assertNotIn('calendar_token', self.event(code))
        self.text('alex','CALENDAR')
        self.assertIn('calendar_token',self.event(code))

    def test_bare_no_after_proposal_declines_attendance_not_calendar(self):
        code=self.proposed();self.text('alex','NO')
        self.assertEqual(self.event(code)['members']['alex']['rsvp'],'declined')

    def test_expired_or_canceled_thread_never_redirects_to_other_plan(self):
        first=self.plan()
        self.text('alex','NEW dinner',create())
        second=self.store.load()['threads']['alex']
        self.text('alex','CANCEL PLAN')
        self.text('alex','NO')
        self.assertEqual(self.event(first)['members']['alex']['rsvp'],'pending')
        self.assertEqual(self.event(second)['status'],'cancelled')
        self.assertIn('last plan has ended',self.store.db.execute('SELECT body FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])

    def test_multiple_names_in_one_message_do_not_join_either(self):
        first=self.plan();self.text('alex','NEW dinner',create())
        second=self.store.load()['threads']['alex']
        self.text('sarah',first+' '+second)
        self.assertNotIn('sarah',self.event(first)['members'])
        self.assertNotIn('sarah',self.event(second)['members'])

    def test_other_plan_broadcast_requires_explicit_selection(self):
        from household import Scheduler
        first=self.confirmed()
        self.text('alex','NEW dinner',create())
        state=self.store.load()
        app=Scheduler(state,None,NOW)
        app.broadcast(state['events'][first],'Reminder for this plan')
        self.assertNotIn('alex',state['threads'])
        self.assertTrue(any('first text' in m[1] for m in app.messages if m[0]=='alex'))

    def test_duplicate_title_requires_full_invitation_name(self):
        self.plan();self.text('alex','NEW another park plan',create())
        self.text('alex','Park playdate')
        self.assertIn('invitation code',self.store.db.execute('SELECT body FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])


class CalendarDownloadTests(unittest.TestCase):
    serve=cloud_tests.CloudSecurityTests.serve

    def event(self):
        return {'calendar_token':'a'*43,'calendar_generated_at':NOW.isoformat(),'slot':'2026-10-24T14:00:00+00:00',
                'minutes':60,'version':2,'title':'Playdate, friends\nBring toys','location':'Park; north gate','status':'confirmed'}

    def test_ics_escaping_utf8_folding_and_utc(self):
        event=self.event();event['title']+='é'*80
        data=calendar_bytes(event)
        self.assertIn(b'DTSTART:20261024T140000Z',data)
        self.assertIn(b'DTEND:20261024T150000Z',data)
        self.assertIn(b'Playdate\\, friends\\nBring toys',data)
        self.assertIn(b'Park\\; north gate',data)
        self.assertTrue(all(len(line)<=75 for line in data.split(b'\r\n')))
        data.decode('utf8')

    def test_download_requires_secret_and_confirmed_plan(self):
        url=self.serve(handler)
        with patch('calendar_export.PostgresStore') as store:
            with self.assertRaises(HTTPError) as error:urlopen(url+'?token=short')
            self.assertEqual(error.exception.code,404);store.assert_not_called()
            event=self.event();store.return_value.load.return_value={'events':{'code':event}}
            with urlopen(url+'?token='+'a'*43) as response:
                self.assertEqual(response.status,200)
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                self.assertIn(b'BEGIN:VCALENDAR',response.read())
            with self.assertRaises(HTTPError) as error:urlopen(url+'?token='+'b'*43)
            self.assertEqual(error.exception.code,404)
            event['status']='cancelled'
            with self.assertRaises(HTTPError) as error:urlopen(url+'?token='+'a'*43)
            self.assertEqual(error.exception.code,409)
