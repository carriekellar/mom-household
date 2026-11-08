import base64
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from plivo_sms import verify, decode, send
import test_cloud
from server import Handler
from household import Store

ENV = {'SMS_PROVIDER':'plivo', 'PLIVO_AUTH_ID':'testauth', 'PLIVO_AUTH_TOKEN':'test-token',
       'PLIVO_PHONE_NUMBER':'+15555550101', 'PLIVO_WEBHOOK_URL':'https://example.com/sms'}

def headers(url=ENV['PLIVO_WEBHOOK_URL']):
    nonce = '12345'
    sig = base64.b64encode(hmac.new(b'test-token', (url+nonce).encode(), hashlib.sha256).digest()).decode()
    return {'X-Plivo-Signature-V2-Nonce':nonce, 'X-Plivo-Signature-V2':sig}

def incoming(**overrides):
    values = {'MessageUUID':'message-123', 'Status':'received', 'From':'15555550102', 'To':'15555550101', 'Text':'Saturday 10 to noon'}
    values.update(overrides)
    return urlencode(values).encode()

class PlivoTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict(os.environ, ENV)
        p.start()
        self.addCleanup(p.stop)

    def test_signature_requires_exact_configured_url_and_token(self):
        verify(headers())
        for bad in ({}, headers('https://attacker.example/sms')):
            with self.assertRaises(ValueError): verify(bad)
        with patch.dict(os.environ, {'PLIVO_AUTH_TOKEN':'other'}):
            with self.assertRaises(ValueError): verify(headers())

    def test_received_normalizes_phone_numbers_and_checks_destination(self):
        self.assertEqual(decode(incoming()), ('received','message-123','+15555550102','Saturday 10 to noon'))
        for raw in (incoming(To='15555550999'), incoming(Text='x'*1601), incoming()+b'&From=15555550999'):
            with self.assertRaises(ValueError): decode(raw)

    def test_intermediate_receipts_do_not_mark_messages_failed(self):
        self.assertEqual(decode(incoming(Status='sent'))[0], 'ignored')
        for status, expected in [('delivered','delivered'),('undelivered','failed'),('failed','failed')]:
            self.assertEqual(decode(incoming(Status=status,From='15555550101'))[-1],expected)

    def test_sends_using_plivo_auth_and_callback(self):
        response = io.BytesIO(b'{"message_uuid":["outbound-123"]}')
        with patch('plivo_sms.urlopen', return_value=response) as request:
            self.assertEqual(send('+15555550102','Hello'), 'outbound-123')
        req = request.call_args.args[0]
        self.assertEqual(req.full_url,'https://api.plivo.com/v1/Account/testauth/Message/')
        body=json.loads(req.data)
        self.assertEqual(body['url'], ENV['PLIVO_WEBHOOK_URL'])
        self.assertEqual(body['method'],'POST')
        self.assertEqual(body['dst'],'+15555550102')

class PlivoHTTPTests(unittest.TestCase):
    serve = test_cloud.CloudSecurityTests.serve

    def test_signed_incoming_text_persists_once(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {**ENV,'DATABASE_PATH':folder+'/db'}):
            url=self.serve(Handler)+'/sms'
            for _ in range(2):
                with urlopen(Request(url,data=incoming(),headers=headers())) as response:
                    self.assertEqual(response.status,200)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url,data=incoming()))
            self.assertEqual(error.exception.code,403)
            store=Store(folder+'/db')
            self.assertEqual(store.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
            store.db.close()
