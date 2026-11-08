"""Read-only calendar download. An unguessable link exposes only a confirmed plan."""
import hmac
import re
from datetime import timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit
from household import stamp
from postgres import PostgresStore


def escape(value):
    return str(value).replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def fold(line):
    # RFC 5545: fold at 75 octets, without splitting a UTF-8 character.
    lines, current = [], ''
    for char in line:
        if len((current + char).encode()) > 75:
            lines.append(current)
            current = ' '
        current += char
    return '\r\n'.join(lines + [current])


def calendar_bytes(event):
    start = stamp(event['slot'])
    end = start + timedelta(minutes=event['minutes'])
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Work Wife//Scheduling//EN',
             'CALSCALE:GREGORIAN', 'BEGIN:VEVENT',
             f"UID:{event['calendar_token']}@work-wife-app.vercel.app",
             'DTSTAMP:' + stamp(event.get('calendar_generated_at', event['slot'])).strftime('%Y%m%dT%H%M%SZ'),
             'DTSTART:' + start.strftime('%Y%m%dT%H%M%SZ'),
             'DTEND:' + end.strftime('%Y%m%dT%H%M%SZ'),
             'SEQUENCE:' + str(event['version']),
             'SUMMARY:' + escape(event['title']), 'LOCATION:' + escape(event['location']),
             'DESCRIPTION:Planned with Work Wife. Updates arrive by SMS; this saved event does not sync automatically.',
             'STATUS:CONFIRMED', 'END:VEVENT', 'END:VCALENDAR']
    return ('\r\n'.join(fold(line) for line in lines) + '\r\n').encode()


class CalendarHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not write bearer URLs to application logs.

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        tokens = query.get('token', [])
        if len(tokens) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{43}', tokens[0]):
            return self.reply(404)
        store = None
        try:
            store = PostgresStore()
            event = next((e for e in store.load()['events'].values()
                          if hmac.compare_digest(e.get('calendar_token', ''), tokens[0])), None)
            if not event:
                return self.reply(404)
            if event['status'] != 'confirmed':
                return self.reply(409, b'This plan is no longer confirmed. Check your Work Wife texts.')
            return self.reply(200, calendar_bytes(event))
        except Exception:
            return self.reply(503)
        finally:
            if store:
                store.db.close()

    def reply(self, status, body=b''):
        self.send_response(status)
        self.send_header('Content-Type', 'text/calendar; charset=utf-8' if status == 200 else 'text/plain; charset=utf-8')
        if status == 200:
            self.send_header('Content-Disposition', 'attachment; filename="work-wife.ics"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Robots-Tag', 'noindex, nofollow')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
