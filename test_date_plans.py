import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock
import test_household as fixtures
from household import Scheduler, UTC, WELCOME
from plan_dates import event_end, reminder_schedule
from calendar_export import calendar_bytes
from transport import deliver


def trip(**changes):
    p = dict(intent='create',title='Kentucky trip',expected=3,minutes=0,
             timezone='America/New_York',location='Kentucky',name='Alex',
             planning_window='November 23–30, 2026',schedule_mode='dates',
             duration_unit='days',duration_count=2,date_bounds=['2026-11-23','2026-11-30'],
             date_windows=[['2026-11-28','2026-11-29']],windows=[],tentative=True)
    p.update(changes)
    return p


class DatePlanTests(unittest.TestCase):
    setUp = fixtures.SchedulingTests.setUp
    tearDown = fixtures.SchedulingTests.tearDown
    text = fixtures.SchedulingTests.text
    event = fixtures.SchedulingTests.event

    def make_trip(self, **changes):
        self.text('alex','Two-day Kentucky trip',trip(**changes))
        return next(iter(self.store.load()['events']))

    def join_dates(self, code, person, dates):
        self.text(person,code+' these dates',dict(intent='availability',date_windows=dates))

    def test_two_day_overlap_confirm_and_calendar(self):
        code=self.make_trip()
        self.join_dates(code,'sarah',[['2026-11-27','2026-11-30']])
        self.join_dates(code,'jess',[['2026-11-28','2026-11-29']])
        self.assertEqual(self.event(code)['slot'],'2026-11-28T00:00:00-05:00')
        self.assertEqual(self.event(code)['status'],'proposed')
        self.text('alex','CALENDAR')
        for person in ('alex','sarah','jess'): self.text(person,'YES')
        e=self.event(code)
        self.assertEqual(e['status'],'confirmed')
        content=calendar_bytes(e).decode()
        self.assertIn('DTSTART;VALUE=DATE:20261128',content)
        self.assertIn('DTEND;VALUE=DATE:20261130',content)
        self.assertNotIn('DTSTART:',content)

    def test_disconnected_dates_cannot_make_two_day_trip(self):
        code=self.make_trip(date_windows=[['2026-11-27','2026-11-27'],['2026-11-29','2026-11-29']])
        for person in ('sarah','jess'):
            self.join_dates(code,person,[['2026-11-27','2026-11-30']])
        self.assertEqual(self.event(code)['status'],'collecting')

    def test_two_nights_requires_three_dates(self):
        code=self.make_trip(duration_unit='nights')
        for person in ('sarah','jess'): self.join_dates(code,person,[['2026-11-28','2026-11-30']])
        self.assertIsNone(self.event(code)['slot'])
        self.join_dates(code,'alex',[['2026-11-28','2026-11-30']])
        self.assertEqual(event_end(self.event(code)).date().isoformat(),'2026-12-01')

    def test_single_day_and_bounds_validation(self):
        code=self.make_trip(duration_count=1)
        self.join_dates(code,'sarah',[['2026-12-01','2026-12-02']])
        self.assertNotIn('sarah',self.event(code)['members'])
        for person in ('sarah','jess'): self.join_dates(code,person,[['2026-11-29','2026-11-29']])
        self.assertEqual(self.event(code)['slot'],'2026-11-29T00:00:00-05:00')

    def test_date_reminder_at_nine_local_only_once(self):
        code=self.make_trip()
        for person in ('sarah','jess'): self.join_dates(code,person,[['2026-11-28','2026-11-29']])
        for person in ('alex','sarah','jess'): self.text(person,'YES')
        due=datetime(2026,11,27,14,tzinfo=UTC)
        self.store.remind(due); self.store.remind(due)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE body LIKE '%Reminder:%'").fetchone()[0],3)
        self.store.remind(datetime(2026,11,28,4,tzinfo=UTC))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE body LIKE '%Reminder:%'").fetchone()[0],3)

    def test_daylight_saving_calendar_days_not_48_hours(self):
        e=trip(date_bounds=[],date_windows=[])
        e['slot']='2026-10-31T00:00:00-04:00'
        self.assertEqual(event_end(e).isoformat(),'2026-11-02T00:00:00-05:00')
        self.assertEqual(reminder_schedule(e)[0][1].hour,9)

    def test_update_invalidates_confirmation_and_preserves_context(self):
        code=self.make_trip()
        for person in ('sarah','jess'): self.join_dates(code,person,[['2026-11-28','2026-11-29']])
        for person in ('alex','sarah','jess'): self.text(person,'YES')
        self.text('sarah','Only Saturday now',dict(intent='availability',date_windows=[['2026-11-28','2026-11-28']]))
        self.assertIsNone(self.event(code)['slot'])
        self.assertTrue(all(m['rsvp']=='pending' for m in self.event(code)['members'].values()))
        parser=Mock(return_value=dict(intent='unknown',question='Can Friday work?'))
        app=Scheduler(self.store.load(),parser,fixtures.NOW)
        app.receive('sarah','What dates did I give you?')
        context=parser.call_args.args[1]
        self.assertEqual(context['current_availability']['date_windows'],[['2026-11-28','2026-11-28']])
        self.assertEqual(context['event']['duration_count'],2)

    def test_code_is_its_own_deliverable_message(self):
        code=self.make_trip()
        rows=self.store.db.execute('SELECT body FROM outbox ORDER BY id').fetchall()
        self.assertEqual(rows[0][0],code)
        self.assertIn('Copy this invitation',rows[1][0])
        sender=Mock(return_value='test-id')
        deliver(self.store,sender)
        sender.assert_called_once_with('alex',code)

    def test_code_first_preserves_availability_until_consent(self):
        code=self.make_trip()
        parser=Mock(return_value=dict(intent='availability',date_windows=[['2026-11-28','2026-11-29']]))
        state=self.store.load(); app=Scheduler(state,parser,fixtures.NOW)
        app.receive('new-person',code+' November 28–29')
        parser.assert_not_called()
        self.assertNotIn('new-person',state['events'][code]['members'])
        self.assertEqual(app.messages[-1][1],WELCOME)
        app.receive('new-person','Y')
        parser.assert_called_once()
        self.assertEqual(state['events'][code]['members']['new-person']['date_windows'],[['2026-11-28','2026-11-28'],['2026-11-29','2026-11-29']])
        self.assertNotIn('new-person',state['pending_invitations'])

    def test_code_only_joins_after_y_without_model(self):
        code=self.make_trip(); parser=Mock()
        state=self.store.load();app=Scheduler(state,parser,fixtures.NOW)
        app.receive('new-person',code);app.receive('new-person','Y')
        parser.assert_not_called()
        self.assertIn('new-person',state['events'][code]['members'])

    def test_pending_code_stop_and_expiry_do_not_join(self):
        code=self.make_trip();state=self.store.load();parser=Mock()
        app=Scheduler(state,parser,fixtures.NOW)
        app.receive('new-person',code);app.receive('new-person','STOP');app.receive('new-person','Y')
        self.assertNotIn('new-person',state['pending_invitations'])
        self.assertNotIn('new-person',state['events'][code]['members'])
        app.receive('another',code)
        Scheduler(state,parser,fixtures.NOW+timedelta(days=2)).receive('another','Y')
        self.assertNotIn('another',state['events'][code]['members'])
        parser.assert_not_called()

    def test_exclusion_uses_set_subtraction_and_addition(self):
        code=self.make_trip()
        self.text('alex','Not Sunday',dict(intent='availability',availability_action='remove',date_windows=[['2026-11-29','2026-11-29']]))
        self.assertEqual(self.event(code)['members']['alex']['date_windows'],[['2026-11-28','2026-11-28']])
        self.text('alex','Sunday works again',dict(intent='availability',availability_action='add',date_windows=[['2026-11-29','2026-11-29']]))
        self.assertEqual(len(self.event(code)['members']['alex']['date_windows']),2)

    def test_bad_pending_availability_does_not_undo_enrollment(self):
        code=self.make_trip()
        self.text('new-person',code+' nonsense')
        self.text('new-person','Y',dict(intent='availability',date_windows=[['invalid','invalid']]))
        state=self.store.load()
        self.assertEqual(state['consents']['new-person']['status'],'enrolled')
        self.assertNotIn('new-person',state['events'][code]['members'])
