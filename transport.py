"""Telnyx SMS adapter and signed webhook validation."""
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
    # could send duplicate invitations/reminders. Operator reconciles with Telnyx.
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
