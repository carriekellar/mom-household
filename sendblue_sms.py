"""Sendblue private messaging adapter. Never logs payloads or API credentials."""
import hmac
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def verify(raw, headers):
    secret = os.getenv('SENDBLUE_WEBHOOK_SECRET', '')
    supplied = next((v for k, v in headers.items() if k.lower() == 'sb-signing-secret'), '')
    if not secret or not hmac.compare_digest(secret.encode(), supplied.encode()):
        raise ValueError('Invalid Sendblue webhook secret')


def phone(value):
    if not isinstance(value, str) or not re.fullmatch(r'\+[1-9]\d{7,14}', value):
        raise ValueError('Invalid phone number')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise ValueError('Invalid message handle')
    return 'sendblue:' + value


def decode(raw):
    p = json.loads(raw)
    if not isinstance(p, dict):
        raise ValueError('Invalid message')
    if p.get('group_id') or p.get('message_type') != 'message':
        return ('ignored', None, None, None)
    sid = identifier(p.get('message_handle'))
    own = phone(os.environ['SENDBLUE_PHONE_NUMBER'])
    source, dest = phone(p.get('from_number')), phone(p.get('to_number'))
    if p.get('is_outbound') is True:
        if source != own:
            raise ValueError('Wrong sending line')
        status = p.get('status')
        if status in ('DELIVERED', 'ERROR', 'DECLINED'):
            return ('finalized', sid, None, 'delivered' if status == 'DELIVERED' else 'failed')
        return ('ignored', sid, None, None)
    if p.get('is_outbound') is not False or dest != own or source == own:
        raise ValueError('Wrong recipient or direction')
    if p.get('status') != 'RECEIVED':
        return ('ignored', sid, None, None)
    body = p.get('content')
    if not isinstance(body, str) or len(body) > 1600:
        raise ValueError('Invalid message content')
    # Sendblue has provider-side opt-out detection (including CANCEL).
    if p.get('opted_out') is True or body.strip().upper() in ('STOP', 'UNSUBSCRIBE', 'CANCEL', 'OPT OUT', 'REVOKE', 'END', 'QUIT'):
        body = 'STOP'
    elif p.get('reply_to') or p.get('thread_originator'):
        # Until persisted inline-thread routing is installed, never interpret an
        # old thread's YES/NO against a different currently selected plan.
        if body.strip().upper() not in ('START', 'UNSTOP', 'HELP'):
            body = 'PLANS'
    if not body.strip():
        return ('ignored', sid, None, None)
    return ('received', sid, source, body)


def api(path, payload=None):
    headers = {'sb-api-key-id': os.environ['SENDBLUE_API_KEY_ID'],
               'sb-api-secret-key': os.environ['SENDBLUE_API_SECRET_KEY'],
               'Content-Type': 'application/json', 'User-Agent': 'WorkWife/1.0'}
    request = Request('https://api.sendblue.com/api/' + path,
                      data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get('status') in ('ERROR', 'DECLINED') or result.get('error_code') not in (None, 0):
        raise ValueError('Sendblue request rejected')
    return result


def send(recipient, text):
    recipient = phone(recipient)
    own = phone(os.environ['SENDBLUE_PHONE_NUMBER'])
    # Fail closed on unsupported or inconclusive lookups. Ordinary Sendblue
    # messages can still fall back after this check; it is not a no-SMS guarantee.
    lookup = api('evaluate-service?' + urlencode({'number': recipient}))
    if lookup.get('number') != recipient or lookup.get('service') != 'iMessage':
        raise ValueError('iMessage support not confirmed')
    result = api('send-message', {'number': recipient, 'from_number': own, 'content': text})
    return identifier(result.get('message_handle'))
