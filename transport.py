"""SMS provider dispatch, legacy Telnyx adapter, and durable delivery."""
import base64
import json
import os
import time
from urllib.request import Request, urlopen


def verify(raw, headers, public_key, now=None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    timestamp = headers.get('telnyx-timestamp', '')
    if abs((time.time() if now is None else now) - int(timestamp)) > 300:
        raise ValueError('Expired signature')
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
    key.verify(base64.b64decode(headers.get('telnyx-signature-ed25519', ''), validate=True),
               timestamp.encode() + b'|' + raw)


def send(phone, text):
    if provider() == "twilio":
        from twilio_sms import send as twilio_send
        return twilio_send(phone, text)
    if provider() == "plivo":
        from plivo_sms import send as plivo_send
        return plivo_send(phone, text)
    body = {'from': os.environ['TELNYX_PHONE_NUMBER'], 'to': phone, 'text': text}
    request = Request('https://api.telnyx.com/v2/messages', data=json.dumps(body).encode(),
                      headers={'Authorization': 'Bearer ' + os.environ['TELNYX_API_KEY'], 'Content-Type': 'application/json'})
    with urlopen(request, timeout=15) as response:
        return json.load(response)['data']['id']


def deliver(store, sender=send):
    row = store.db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    if not row:
        return False
    state = store.load()
    e = state['events'].get(row['event'])
    stale = e and (e['version'] != row['version'] or e['members'].get(row['phone'], {}).get('rsvp') == 'declined')
    if row['phone'] in state['blocked'] or stale:
        with store.db:
            store.db.execute("UPDATE outbox SET status='suppressed' WHERE id=?", (row['id'],))
        return True
    # Persist before network I/O. Ambiguous sends are never blindly retried: that
    # could send duplicate invitations/reminders. Operator reconciles with the SMS provider.
    with store.db:
        store.db.execute("UPDATE outbox SET status='sending' WHERE id=?", (row['id'],))
    try:
        provider_id = sender(row['phone'], row['body'])
    except Exception as exc:
        with store.db:
            store.db.execute("UPDATE outbox SET status='needs_review', error=? WHERE id=?",
                             (type(exc).__name__, row['id']))
    else:
        with store.db:
            store.db.execute("UPDATE outbox SET status='accepted', provider_sid=? WHERE id=?", (provider_id, row['id']))
    return True


def provider():
    value = os.getenv('SMS_PROVIDER', 'twilio')
    if value not in ('twilio', 'plivo', 'telnyx'):
        raise ValueError('Unknown SMS_PROVIDER')
    return value


def verify_webhook(raw, headers):
    if provider() == 'twilio':
        from twilio_sms import verify as twilio_verify
        return twilio_verify(raw, headers)
    if provider() == 'plivo':
        from plivo_sms import verify as plivo_verify
        plivo_verify(headers)
    else:
        verify(raw, headers, os.environ['TELNYX_PUBLIC_KEY'])


def decode_webhook(raw):
    if provider() == 'twilio':
        from twilio_sms import decode
        return decode(raw)
    if provider() == 'plivo':
        from plivo_sms import decode
        return decode(raw)
    import re
    data = json.loads(raw)['data']
    p = data['payload']
    if data['event_type'] == 'message.received':
        phone, body = p['from']['phone_number'], p.get('text') or ''
        if not re.fullmatch(r'\+[1-9]\d{7,14}', phone) or not isinstance(body, str) or len(body) > 1600:
            raise ValueError('Invalid SMS')
        if not any(t['phone_number'] == os.environ['TELNYX_PHONE_NUMBER'] for t in p['to']):
            raise ValueError('Wrong recipient')
        return ('received', p['id'], phone, body)
    if data['event_type'] == 'message.finalized':
        statuses = [t.get('status') for t in p.get('to', [])]
        return ('finalized', p['id'], None, 'delivered' if statuses and all(s == 'delivered' for s in statuses) else 'failed')
    return ('ignored', None, None, None)


def phone_number():
    return os.getenv(provider().upper() + '_PHONE_NUMBER', '')


def required_settings():
    return {
        'twilio': ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE_NUMBER', 'TWILIO_WEBHOOK_URL'],
        'plivo': ['PLIVO_AUTH_ID', 'PLIVO_AUTH_TOKEN', 'PLIVO_PHONE_NUMBER', 'PLIVO_WEBHOOK_URL'],
        'telnyx': ['TELNYX_API_KEY', 'TELNYX_PUBLIC_KEY', 'TELNYX_PHONE_NUMBER'],
    }[provider()]
