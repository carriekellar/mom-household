"""Bounded Vercel handlers; all durable work lives in Supabase."""
import hmac
import json
import os
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from household import UTC
from language import parse
from postgres import PostgresStore
from transport import verify


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code):
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.end_headers()


class SMSHandler(Handler):
    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 32768:
                return self.reply(413)
            raw = self.rfile.read(length)
            verify(raw, self.headers, os.environ['TELNYX_PUBLIC_KEY'])
        except Exception:
            return self.reply(403)
        store = None
        try:
            data = json.loads(raw)['data']
            payload = data['payload']
            store = PostgresStore()
            if data['event_type'] == 'message.received':
                phone, body = payload['from']['phone_number'], payload.get('text') or ''
                if not re.fullmatch(r'\+[1-9]\d{7,14}', phone) or not isinstance(body, str) or len(body) > 1600:
                    return self.reply(400)
                if not any(t['phone_number'] == os.environ['TELNYX_PHONE_NUMBER'] for t in payload['to']):
                    return self.reply(400)
                store.enqueue(payload['id'], phone, body)
            elif data['event_type'] == 'message.finalized':
                statuses = [t.get('status') for t in payload.get('to', [])]
                store.finalized(payload['id'], 'delivered' if statuses and all(s == 'delivered' for s in statuses) else 'failed')
        except (KeyError, ValueError, TypeError):
            return self.reply(400)
        except Exception:
            return self.reply(503)  # Provider retries; inbox IDs deduplicate.
        finally:
            if store:
                store.db.close()
        self.reply(200)


class ProcessHandler(Handler):
    def do_POST(self):
        secret = os.getenv('CRON_SECRET', '')
        if not secret or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + secret):
            return self.reply(401)
        store = None
        try:
            store = PostgresStore()
            with store.worker() as locked:
                if locked:
                    deadline = time.monotonic() + 200
                    store.remind(datetime.now(UTC))
                    for _ in range(3):
                        if time.monotonic() > deadline:
                            break
                        if not store.process(parse, datetime.now(UTC)):
                            break
                    for _ in range(100):
                        if time.monotonic() > deadline:
                            break
                        if not store.deliver():
                            break
        except Exception:
            return self.reply(503)
        finally:
            if store:
                store.db.close()
        self.reply(200)
