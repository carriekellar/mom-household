"""Calendar-date matching, distinct from elapsed-hour scheduling."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def date_plan(event):
    return event.get('schedule_mode', 'timed') == 'dates'


def duration_text(event):
    if not date_plan(event):
        return f"{event['minutes']} min"
    unit = event['duration_unit']
    return f"{event['duration_count']} {unit}"


def day_count(event):
    return event['duration_count'] + (event['duration_unit'] == 'nights')


def normalize_ranges(ranges, event, now):
    if not ranges or len(ranges) > 40:
        raise ValueError('Missing date availability')
    today = now.astimezone(ZoneInfo(event['timezone'])).date()
    days = set()
    bounds = event.get('date_bounds')
    for a, b in ranges:
        start, end = date.fromisoformat(a), date.fromisoformat(b)
        if start <= today or end < start or end > today + timedelta(days=90):
            raise ValueError('Invalid date availability')
        if bounds and (start < date.fromisoformat(bounds[0]) or end > date.fromisoformat(bounds[1])):
            raise ValueError('Availability outside planning window')
        days.update(start + timedelta(days=i) for i in range((end-start).days+1))
    return [[d.isoformat(), d.isoformat()] for d in sorted(days)]


def match_dates(event, now):
    groups = [{date.fromisoformat(a) for a, b in m.get('date_windows', [])}
              for m in event['members'].values()]
    if not groups or any(not g for g in groups):
        return None
    common = set.intersection(*groups)
    today = now.astimezone(ZoneInfo(event['timezone'])).date()
    for start in sorted(common):
        if start > today and all(start + timedelta(days=i) in common for i in range(day_count(event))):
            return datetime.combine(start, time(), ZoneInfo(event['timezone'])).isoformat()
    return None


def event_end(event):
    start = datetime.fromisoformat(event['slot'])
    if date_plan(event):
        start = start.astimezone(ZoneInfo(event['timezone']))
        return datetime.combine(start.date()+timedelta(days=day_count(event)), time(), ZoneInfo(event['timezone']))
    return start + timedelta(minutes=event['minutes'])


def reminder_schedule(event):
    start = datetime.fromisoformat(event['slot'])
    if date_plan(event):
        day = start.astimezone(ZoneInfo(event['timezone'])).date()-timedelta(days=1)
        return [('day-before', datetime.combine(day, time(9), ZoneInfo(event['timezone'])))]
    return [(str(hours), start-timedelta(hours=hours)) for hours in (24, 1)]
