import io
import json
import os
import tempfile
import unittest
from urllib.parse import urlencode, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from twilio.request_validator import RequestValidator
import test_cloud
from household import Store, HELP
from server import Handler
from api.sms import handler as SMSHandler
from twilio_sms import verify, decode, send
from transport import provider, phone_number

ENV = {'SMS_PROVIDER':'twilio', 'TWILIO_ACCOUNT_SID':'AC'+'a'*32,
       'TWILIO_AUTH_TOKEN':'test-token', 'TWILIO_PHONE_NUMBER':'+15555550101',
       'TWILIO_WEBHOOK_URL':'https://example.com/sms'}
SID = 'SM'+'b'*32

def incoming(**overrides):
    p={'AccountSid':ENV['TWILIO_ACCOUNT_SID'], 'MessageSid':SID, 'SmsStatus':'received',
       'From':'+15555550102', 'To':ENV['TWILIO_PHONE_NUMBER'], 'Body':'Saturday 10 to noon'}
    p.update(overrides)
    return p

def headers(params, url=ENV['TWILIO_WEBHOOK_URL']):
    return {'X-Twilio-Signature':RequestValidator('test-token').compute_signature(url,params),
            'Content-Type':'application/x-www-form-urlencoded'}

class TwilioTests(unittest.TestCase):
    serve = test_cloud.CloudSecurityTests.serve

    def setUp(self):
        p=patch.dict(os.environ,ENV)
        p.start()
        self.addCleanup(p.stop)

    def test_signature_covers_body_and_exact_url(self):
        p=incoming()
        verify(urlencode(p).encode(),headers(p))
        for raw,h in [(urlencode(incoming(Body='CANCEL PLAN')).encode(),headers(p)),
                      (urlencode(p).encode(),headers(p,'https://wrong.example/sms')),
                      (urlencode(p).encode(),{})]:
            with self.assertRaises(ValueError): verify(raw,h)

    def test_account_destination_and_duplicate_fields(self):
        self.assertEqual(decode(urlencode(incoming()).encode()), ('received',SID,'+15555550102','Saturday 10 to noon'))
        for p in (incoming(AccountSid='wrong'), incoming(To='+15555550999'), incoming(Body='x'*1601)):
            with self.assertRaises(ValueError): decode(urlencode(p).encode())
        with self.assertRaises(ValueError): decode(urlencode(incoming()).encode()+b'&Body=extra')

    def test_statuses_and_opt_out_mapping(self):
        for status,kind in [('sent','ignored'),('queued','ignored'),('delivered','finalized'),('undelivered','finalized')]:
            self.assertEqual(decode(urlencode(incoming(MessageStatus=status,From=ENV['TWILIO_PHONE_NUMBER'])).encode())[0],kind)
        self.assertEqual(decode(urlencode(incoming(OptOutType='STOP',Body='unsubscribe')).encode())[-1],'STOP')

    def test_outbound_api_contract(self):
        with patch('twilio_sms.urlopen',return_value=io.BytesIO(json.dumps({'sid':SID}).encode())) as request:
            self.assertEqual(send('+15555550102','Hello'),SID)
        req=request.call_args.args[0]
        self.assertEqual(req.full_url,f"https://api.twilio.com/2010-04-01/Accounts/{ENV['TWILIO_ACCOUNT_SID']}/Messages.json")
        params=parse_qs(req.data.decode())
        self.assertEqual(params['StatusCallback'],[ENV['TWILIO_WEBHOOK_URL']])
        self.assertEqual(params['Body'],['Hello'])
        self.assertEqual(params['From'],[ENV['TWILIO_PHONE_NUMBER']])

    def test_service_sender_and_invalid_service(self):
        with patch.dict(os.environ, {'TWILIO_MESSAGING_SERVICE_SID': 'MG'+'c'*32}):
            with patch('twilio_sms.urlopen', return_value=io.BytesIO(json.dumps({'sid':SID}).encode())) as request:
                send('+15555550102', 'Hello')
            self.assertEqual(parse_qs(request.call_args.args[0].data.decode())['MessagingServiceSid'], ['MG'+'c'*32])
        with patch.dict(os.environ, {'TWILIO_MESSAGING_SERVICE_SID': 'wrong'}):
            with self.assertRaises(ValueError):
                send('+15555550102', 'Hello')

    def test_provider_start_signal_preserves_rsvp_and_application_enrollment(self):
        for body in ('YES', 'Y', 'JOIN'):
            self.assertEqual(decode(urlencode(incoming(OptOutType='START', Body=body)).encode())[-1], body)

    def test_provider_help_does_not_send_duplicate_response(self):
        self.assertEqual(decode(urlencode(incoming(OptOutType='HELP', Body='HELP')).encode())[0], 'ignored')

    def test_default_provider_and_invitation_number(self):
        with patch.dict(os.environ,ENV):
            os.environ.pop('SMS_PROVIDER')
            self.assertEqual(provider(),'twilio')
            self.assertEqual(phone_number(),ENV['TWILIO_PHONE_NUMBER'])

    def test_local_webhook_persists_once_and_returns_twiml(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'DATABASE_PATH':folder+'/db'}):
            url=self.serve(Handler)+'/sms'
            p=incoming()
            for _ in range(2):
                with urlopen(Request(url,data=urlencode(p).encode(),headers=headers(p))) as response:
                    self.assertEqual(response.read(),b'<Response/>')
            store=Store(folder+'/db')
            self.assertEqual(store.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
            store.db.close()

    def test_cloud_webhook_authenticates_before_database_access(self):
        url=self.serve(SMSHandler)+'/sms'
        p=incoming()
        with patch('cloud.PostgresStore') as store:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url,data=urlencode(p).encode()))
            self.assertEqual(error.exception.code,403)
            store.assert_not_called()
            with urlopen(Request(url,data=urlencode(p).encode(),headers=headers(p))) as response:
                self.assertEqual(response.read(),b'<Response/>')
            store.return_value.enqueue.assert_called_once_with(SID,'+15555550102','Saturday 10 to noon')
            receipt=incoming(MessageStatus='delivered',From=ENV['TWILIO_PHONE_NUMBER'])
            with urlopen(Request(url,data=urlencode(receipt).encode(),headers=headers(receipt))): pass
            store.return_value.finalized.assert_called_once_with(SID,'delivered')


class SendLimitTests(unittest.TestCase):
    def test_cap_survives_reopen_and_counts_uncertain_attempts(self):
        from transport import deliver
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'SMS_SEND_LIMIT':'1'}):
            store = Store(folder+'/db')
            with store.db:
                store.db.execute("INSERT INTO outbox(phone,body) VALUES('alex','" + HELP + "'),('alex','" + HELP + "')")
            def fail(*args):
                raise TimeoutError()
            self.assertTrue(deliver(store, fail))
            store.db.close()
            store = Store(folder+'/db')
            with patch('transport.send') as unused:
                self.assertFalse(deliver(store, unused))
                unused.assert_not_called()
            self.assertEqual(store.db.execute("SELECT count(*) FROM outbox WHERE status='pending'").fetchone()[0],1)
            store.db.close()

    def test_zero_limit_pauses_sending(self):
        from transport import deliver
        store = Store(':memory:')
        self.addCleanup(store.db.close)
        with store.db:
            store.db.execute("INSERT INTO outbox(phone,body) VALUES('alex','" + HELP + "')")
        with patch.dict(os.environ, {'SMS_SEND_LIMIT':'0'}), patch('transport.send') as sender:
            self.assertFalse(deliver(store,sender))
            sender.assert_not_called()
