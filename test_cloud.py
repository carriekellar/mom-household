import base64
import json
import os
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from cloud import SMSHandler, ProcessHandler


class CloudSecurityTests(unittest.TestCase):
    def serve(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def test_cron_requires_secret_before_connecting_to_database(self):
        url = self.serve(ProcessHandler)
        with patch.dict(os.environ, {'CRON_SECRET': 'test-secret'}), patch('cloud.PostgresStore') as store:
            for headers in ({}, {'Authorization': 'Bearer wrong'}):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url, data=b'{}', headers=headers))
                self.assertEqual(error.exception.code, 401)
            store.assert_not_called()
            store.return_value.worker.return_value.__enter__.return_value = False
            with urlopen(Request(url, data=b'{}', headers={'Authorization': 'Bearer test-secret'})) as response:
                self.assertEqual(response.status, 200)
            store.return_value.process.assert_not_called()
            store.return_value.db.close.assert_called_once()

    def test_webhook_signature_validation_and_database_failure(self):
        url = self.serve(SMSHandler)
        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        raw = json.dumps({'data': {'event_type': 'message.received', 'payload': {
            'id': 'one', 'from': {'phone_number': '+15555550102'},
            'to': [{'phone_number': '+15555550101'}], 'text': 'Playdate'
        }}}).encode()
        ts = str(int(time.time()))
        headers = {'telnyx-timestamp': ts, 'telnyx-signature-ed25519': base64.b64encode(key.sign(ts.encode() + b'|' + raw)).decode()}
        with patch.dict(os.environ, {'TELNYX_PUBLIC_KEY': public, 'TELNYX_PHONE_NUMBER': '+15555550101'}), patch('cloud.PostgresStore') as store:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=raw))
            self.assertEqual(error.exception.code, 403)
            store.assert_not_called()
            with urlopen(Request(url, data=raw, headers=headers)) as response:
                self.assertEqual(response.status, 200)
            store.return_value.enqueue.assert_called_once_with('one', '+15555550102', 'Playdate')
            store.return_value.enqueue.side_effect = RuntimeError('database down')
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=raw, headers=headers))
            self.assertEqual(error.exception.code, 503)
