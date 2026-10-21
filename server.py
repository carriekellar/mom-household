"""Run behind HTTPS reverse proxy; one instance with a persistent SQLite volume."""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from household import Store, UTC
from language import parse
from transport import deliver, verify

log = logging.getLogger('household')


def load_env():
    try:
        with open('.env') as file:
            for line in file:
                if '=' in line and not line.lstrip().startswith('#'):
                    key, value = line.strip().split('=', 1)
                    os.environ.setdefault(key, value.strip('"\''))
    except FileNotFoundError:
        pass


def database():
    return Store(os.getenv('DATABASE_PATH', 'household.sqlite3'))


def worker():
    store = database()
    with store.db:
        store.db.execute("UPDATE outbox SET status='needs_review' WHERE status='sending'")
    while True:
        try:
            # Drain a bounded batch so reminders still run during incoming traffic.
            for _ in range(20):
                if not store.process(parse, datetime.now(UTC)):
                    break
            store.remind(datetime.now(UTC))
            for _ in range(100):
                if not deliver(store):
                    break
        except Exception as exc:
            log.error('Worker failure: %s', type(exc).__name__)
            # Provider outages do not drop inbound messages. Limit poison retries.
            with store.db:
                row = store.db.execute("SELECT sid,attempts FROM inbox WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
                if row:
                    store.db.execute("UPDATE inbox SET attempts=attempts+1,status=? WHERE sid=?",
                                     ('needs_review' if row['attempts'] >= 2 else 'pending', row['sid']))
            time.sleep(5)
        time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # No phone numbers, webhook bodies, or credentials in access logs.

    def reply(self, code):
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        self.reply(200 if self.path == '/health' else 404)

    def do_POST(self):
        if self.path != '/sms':
            return self.reply(404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 32768:
                return self.reply(413)
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
            verify(raw, self.headers, os.environ['TELNYX_PUBLIC_KEY'])
        except Exception:
            return self.reply(403)
        store = database()
        try:
            data = json.loads(raw)['data']
            payload = data['payload']
            if data['event_type'] == 'message.received':
                phone, body = payload['from']['phone_number'], payload.get('text') or ''
                if not re.fullmatch(r'\+[1-9]\d{7,14}', phone) or len(body) > 1600:
                    return self.reply(400)
                if not any(t['phone_number'] == os.environ['TELNYX_PHONE_NUMBER'] for t in payload['to']):
                    return self.reply(400)
                store.enqueue(payload['id'], phone, body)
            elif data['event_type'] == 'message.finalized':
                statuses = [t.get('status') for t in payload.get('to', [])]
                with store.db:
                    store.db.execute('UPDATE outbox SET status=? WHERE provider_sid=?',
                                     ('delivered' if statuses and all(s == 'delivered' for s in statuses) else 'failed', payload['id']))
            self.reply(200)
        except (KeyError, ValueError, TypeError):
            self.reply(400)
        finally:
            store.db.close()


if __name__ == '__main__':
    load_env()
    required = ['OPENAI_API_KEY', 'TELNYX_API_KEY', 'TELNYX_PUBLIC_KEY', 'TELNYX_PHONE_NUMBER']
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise SystemExit('Set these in .env: ' + ', '.join(missing))
    logging.basicConfig(level=logging.INFO)
    database().db.close()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8000'))), Handler).serve_forever()
