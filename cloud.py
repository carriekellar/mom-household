"""Bounded Vercel handlers; all durable work lives in Supabase."""
import hmac
import os
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from household import UTC
from language import parse
from postgres import PostgresStore
from transport import verify_webhook, decode_webhook


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
            verify_webhook(raw, self.headers)
        except Exception:
            return self.reply(403)
        store = None
        try:
            kind, sid, phone, value = decode_webhook(raw)
            store = PostgresStore()
            if kind == 'received':
                store.enqueue(sid, phone, value)
            elif kind == 'finalized':
                store.finalized(sid, value)
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
