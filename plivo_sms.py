"""Plivo SMS transport. Webhook URL is pinned, never inferred from proxy headers."""
import base64
import hashlib
import hmac
import json
import os
import re
from urllib.parse import parse_qs
from urllib.request import Request, urlopen


def number(value):
    value = '+' + str(value).lstrip('+')
    if not re.fullmatch(r'\+[1-9]\d{7,14}', value):
        raise ValueError('Invalid phone number')
    return value


def verify(headers):
    url = os.environ['PLIVO_WEBHOOK_URL']
    token = os.environ['PLIVO_AUTH_TOKEN']
    nonce = headers.get('X-Plivo-Signature-V2-Nonce', '')
    signature = headers.get('X-Plivo-Signature-V2', '')
    if not token or not nonce or not url.startswith('https://'):
        raise ValueError('Missing webhook configuration')
    expected = base64.b64encode(hmac.new(token.encode(), (url + nonce).encode(), hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(expected, signature):
        raise ValueError('Invalid signature')


def decode(raw):
    values = parse_qs(raw.decode('utf-8'), keep_blank_values=True, strict_parsing=True, max_num_fields=50)
    if any(len(v) != 1 for v in values.values()):
        raise ValueError('Duplicate fields')
    p = {k: v[0] for k, v in values.items()}
    sid = p['MessageUUID']
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,100}', sid):
        raise ValueError('Invalid message ID')
    status = p['Status']
    if status == 'received':
        if number(p['To']) != number(os.environ['PLIVO_PHONE_NUMBER']):
            raise ValueError('Wrong recipient')
        body = p.get('Text', '')
        if len(body) > 1600:
            raise ValueError('Message too long')
        return ('received', sid, number(p['From']), body)
    if status in ('delivered', 'failed', 'undelivered', 'rejected'):
        if number(p['From']) != number(os.environ['PLIVO_PHONE_NUMBER']):
            raise ValueError('Wrong sender')
        return ('finalized', sid, None, 'delivered' if status == 'delivered' else 'failed')
    if status in ('queued', 'sent'):
        return ('ignored', sid, None, None)
    raise ValueError('Unknown message status')


def send(phone, text):
    auth_id = os.environ['PLIVO_AUTH_ID']
    if not re.fullmatch(r'[a-zA-Z0-9]+', auth_id):
        raise ValueError('Invalid Auth ID')
    credentials = base64.b64encode((auth_id + ':' + os.environ['PLIVO_AUTH_TOKEN']).encode()).decode()
    body = {'src': number(os.environ['PLIVO_PHONE_NUMBER']), 'dst': number(phone),
            'text': text, 'url': os.environ['PLIVO_WEBHOOK_URL'], 'method': 'POST'}
    req = Request(f'https://api.plivo.com/v1/Account/{auth_id}/Message/',
                  data=json.dumps(body).encode(), headers={'Authorization': 'Basic ' + credentials, 'Content-Type': 'application/json'})
    with urlopen(req, timeout=15) as response:
        return json.load(response)['message_uuid'][0]
