"""Twilio SMS adapter; callback signatures cover the pinned URL and form fields."""
import base64
import json
import os
import re
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from twilio.request_validator import RequestValidator


def fields(raw):
    values = parse_qs(raw.decode('utf-8'), keep_blank_values=True, strict_parsing=True, max_num_fields=100)
    if any(len(v) != 1 for v in values.values()):
        raise ValueError('Duplicate fields')
    return {k: v[0] for k, v in values.items()}


def webhook_url():
    url = os.environ['TWILIO_WEBHOOK_URL']
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.fragment or parsed.query:
        raise ValueError('Configure an exact HTTPS callback URL without query or fragment')
    return url


def number(value):
    if not re.fullmatch(r'\+[1-9]\d{7,14}', value):
        raise ValueError('Invalid phone number')
    return value


def verify(raw, headers):
    token = os.environ['TWILIO_AUTH_TOKEN']
    if not token or not RequestValidator(token).validate(webhook_url(), fields(raw), headers.get('X-Twilio-Signature', '')):
        raise ValueError('Invalid Twilio signature')


def decode(raw):
    p = fields(raw)
    if p['AccountSid'] != os.environ['TWILIO_ACCOUNT_SID']:
        raise ValueError('Wrong account')
    sid = p['MessageSid']
    if not re.fullmatch(r'(SM|MM)[0-9a-fA-F]{32}', sid):
        raise ValueError('Invalid message SID')
    status = p.get('MessageStatus') or p.get('SmsStatus')
    if status == 'received':
        if number(p['To']) != number(os.environ['TWILIO_PHONE_NUMBER']):
            raise ValueError('Wrong recipient')
        body = p.get('Body', '')
        if len(body) > 1600:
            raise ValueError('Message too long')
        # Advanced Opt-Out can use configurable keywords. Map provider events to
        # our canonical commands so local suppression matches Twilio's state.
        body = {'STOP': 'STOP', 'START': 'START', 'HELP': 'HELP'}.get(p.get('OptOutType'), body)
        return ('received', sid, number(p['From']), body)
    if status in ('delivered', 'undelivered', 'failed', 'canceled'):
        if number(p['From']) != number(os.environ['TWILIO_PHONE_NUMBER']):
            raise ValueError('Wrong sender')
        return ('finalized', sid, None, 'delivered' if status == 'delivered' else 'failed')
    if status in ('accepted', 'scheduled', 'queued', 'sending', 'sent'):
        return ('ignored', sid, None, None)
    raise ValueError('Unknown message status')


def send(phone, text):
    account = os.environ['TWILIO_ACCOUNT_SID']
    if not re.fullmatch(r'AC[0-9a-fA-F]{32}', account):
        raise ValueError('Invalid Account SID')
    credentials = base64.b64encode((account + ':' + os.environ['TWILIO_AUTH_TOKEN']).encode()).decode()
    body = {'From': number(os.environ['TWILIO_PHONE_NUMBER']), 'To': number(phone),
            'Body': text, 'StatusCallback': webhook_url()}
    req = Request(f'https://api.twilio.com/2010-04-01/Accounts/{account}/Messages.json',
                  data=urlencode(body).encode(), headers={'Authorization': 'Basic ' + credentials,
                  'Content-Type': 'application/x-www-form-urlencoded'})
    with urlopen(req, timeout=15) as response:
        return json.load(response)['sid']
