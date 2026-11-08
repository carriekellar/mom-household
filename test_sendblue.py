import io
import json
import os
import unittest
from unittest.mock import patch
import sendblue_sms as sb
import test_cloud
from api.sms import handler as SMSHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from transport import decode_webhook, verify_webhook, required_settings

ENV = {'SMS_PROVIDER': 'sendblue', 'SENDBLUE_PHONE_NUMBER': '+15555550101',
       'SENDBLUE_WEBHOOK_SECRET': 'test-secret', 'SENDBLUE_API_KEY_ID': 'test-key',
       'SENDBLUE_API_SECRET_KEY': 'test-api-secret'}


def incoming(**overrides):
    p = dict(message_handle='example-id', from_number='+15555550102',
             to_number=ENV['SENDBLUE_PHONE_NUMBER'], is_outbound=False,
             status='RECEIVED', content='Saturday 10 to noon', message_type='message', group_id='')
    p.update(overrides)
    return json.dumps(p).encode()


class SendblueTests(unittest.TestCase):
    serve = test_cloud.CloudSecurityTests.serve

    def test_webhook_authenticates_before_database_and_enqueues(self):
        url = self.serve(SMSHandler) + "/sms"
        with patch("cloud.PostgresStore") as store:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=incoming()))
            self.assertEqual(error.exception.code, 403)
            store.assert_not_called()
            with urlopen(Request(url, data=incoming(), headers={"sb-signing-secret":"test-secret"})) as response:
                self.assertEqual(response.status, 200)
            store.return_value.enqueue.assert_called_once_with("sendblue:example-id", "+15555550102", "Saturday 10 to noon")

    def setUp(self):
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)

    def test_secret_required_case_insensitive(self):
        verify_webhook(b'{}', {'Sb-Signing-Secret': 'test-secret'})
        for headers in ({}, {'sb-signing-secret': 'wrong'}):
            with self.assertRaises(ValueError): verify_webhook(b'{}', headers)
        with patch.dict(os.environ, {'SENDBLUE_WEBHOOK_SECRET': ''}):
            with self.assertRaises(ValueError): verify_webhook(b'{}', {})
        self.assertIn('SENDBLUE_WEBHOOK_SECRET', required_settings())

    def test_inbound_validation_and_group_isolation(self):
        self.assertEqual(decode_webhook(incoming()), ('received', 'sendblue:example-id', '+15555550102', 'Saturday 10 to noon'))
        for override in ({'to_number': '+15555550999'}, {'is_outbound': 'false'},
                         {'content': []}, {'content': 'x'*1601}, {'message_handle': ''},
                         {'from_number': 'not-a-phone'}):
            with self.assertRaises(ValueError): sb.decode(incoming(**override))
        self.assertEqual(sb.decode(incoming(group_id='group'))[0], 'ignored')
        self.assertEqual(sb.decode(incoming(message_type='reaction'))[0], 'ignored')
        self.assertEqual(sb.decode(incoming(content=''))[0], 'ignored')

    def test_opt_out_and_inline_reply_cannot_apply_to_wrong_plan(self):
        for content in ('CANCEL', 'OPT OUT', 'STOP'):
            self.assertEqual(sb.decode(incoming(content=content))[-1], 'STOP')
        self.assertEqual(sb.decode(incoming(content='something', opted_out=True))[-1], 'STOP')
        self.assertEqual(sb.decode(incoming(content='YES'))[-1], 'YES')
        self.assertEqual(sb.decode(incoming(content='YES', reply_to={'message_handle':'old-plan'}))[-1], 'PLANS')
        self.assertEqual(sb.decode(incoming(content='STOP', reply_to={'message_handle':'old-plan'}))[-1], 'STOP')

    def test_delivery_status_is_not_claimed_for_sent(self):
        for status, expected in [('DELIVERED', 'delivered'), ('ERROR', 'failed'), ('DECLINED', 'failed')]:
            result = sb.decode(incoming(is_outbound=True, from_number=ENV['SENDBLUE_PHONE_NUMBER'], to_number='+15555550102', status=status))
            self.assertEqual(result, ('finalized', 'sendblue:example-id', None, expected))
        self.assertEqual(sb.decode(incoming(is_outbound=True, from_number=ENV['SENDBLUE_PHONE_NUMBER'], status='SENT'))[0], 'ignored')
        with self.assertRaises(ValueError): sb.decode(incoming(is_outbound=True, status='DELIVERED'))

    def test_outbound_contract_and_lookup(self):
        responses = [io.BytesIO(json.dumps({'number':'+15555550102','service':'iMessage'}).encode()),
                     io.BytesIO(json.dumps({'message_handle':'sent-id','status':'QUEUED'}).encode())]
        with patch('sendblue_sms.urlopen', side_effect=responses) as request:
            self.assertEqual(sb.send('+15555550102','Hello'), 'sendblue:sent-id')
        lookup, send = [call.args[0] for call in request.call_args_list]
        self.assertIn('evaluate-service?number=%2B15555550102',lookup.full_url)
        self.assertEqual(send.full_url,'https://api.sendblue.com/api/send-message')
        self.assertEqual(json.loads(send.data), {'number':'+15555550102','from_number':ENV['SENDBLUE_PHONE_NUMBER'],'content':'Hello'})
        self.assertEqual(send.get_header('Sb-api-key-id'), 'test-key')

    def test_no_send_when_lookup_is_unknown_or_sms(self):
        for service in ('SMS', None):
            with patch('sendblue_sms.api', return_value={'number':'+15555550102','service':service}) as api:
                with self.assertRaises(ValueError): sb.send('+15555550102','Hello')
                self.assertEqual(api.call_count,1)
        with patch('sendblue_sms.urlopen', return_value=io.BytesIO(b'{"status":"DECLINED"}')):
            with self.assertRaises(ValueError): sb.api('send-message', {})
