"""Offline scripted demo. Fixtures replace AI extraction; no texts are sent."""
from datetime import datetime
from household import Store, UTC
from transport import deliver
from test_household import create, WINDOW, NOW

store = Store(':memory:')
sequence = 0


def text(person, message, intent=None):
    global sequence
    sequence += 1
    print(f'\n{person} → {message}')
    store.enqueue(str(sequence), person, message)
    store.process(lambda *_: intent, NOW)
    while deliver(store, lambda p, t: print(f'  Mom Household → {p}: {t}') or 'demo'):
        pass


text('Alex', 'Playdate at Prospect Park for 3 parents including me. An hour Saturday, October 24. I am free 10 to noon.', create())
code = next(iter(store.load()['events']))
text('Sarah', f'{code} I am Sarah. Saturday 10 to noon works.', {'intent': 'availability', 'name': 'Sarah', 'windows': [WINDOW]})
text('Jess', f'{code} Jess here, Saturday 11 to 1.', {'intent': 'availability', 'name': 'Jess', 'windows': [['2026-10-24T11:00:00-04:00', '2026-10-24T13:00:00-04:00']]})
for person in ('Alex', 'Sarah', 'Jess'):
    text(person, 'YES')
print('\n— Day-before reminder —')
store.remind(datetime(2026, 10, 23, 15, tzinfo=UTC))
while deliver(store, lambda p, t: print(f'  Mom Household → {p}: {t}') or 'demo'):
    pass
text('Sarah', "Sorry, can't make the playdate", {'intent': 'decline'})
store.db.close()
