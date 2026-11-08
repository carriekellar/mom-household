"""Opt-in synthetic OpenAI checks: python eval_scheduling.py (no SMS or DB writes)."""
from language import parse

BASE = {'now':'2026-11-08T13:00:00-05:00','event':None,'conversation':[]}
EVENT = {'title':'Kentucky trip','expected':3,'minutes':0,'schedule_mode':'dates',
         'duration_unit':'days','duration_count':2,'timezone':'America/New_York',
         'date_bounds':['2026-11-24','2026-11-30'],'planning_window':'November 24–30, 2026'}


def run():
    p=parse('Two-day Kentucky trip for 3 adults including me in the last week of November. I am free that weekend, probably.', BASE)
    assert (p['schedule_mode'],p['duration_unit'],p['duration_count'])==('dates','days',2),p
    assert p['date_windows']==[['2026-11-28','2026-11-29']] and not p['question'],p
    print('PASS two-day trip creation')
    ctx={**BASE,'event':EVENT,'current_availability':{},'conversation':[{'user':'The last week'},{'assistant':'Which dates work for you?'}]}
    p=parse('The weekend probably',ctx)
    assert p['date_windows']==[['2026-11-28','2026-11-29']] and not p['question'] and p['tentative'],p
    print('PASS contextual weekend follow-up')
    ctx['current_availability']={'date_windows':[['2026-11-28','2026-11-28'],['2026-11-29','2026-11-29']]}
    ctx['conversation']=[]
    p=parse('Actually Sunday does not work anymore.',ctx)
    assert p['intent']=='availability' and p['availability_action']=='remove' and p['date_windows']==[['2026-11-29','2026-11-29']] and not p['question'],p
    print('PASS personal date exclusion')
    p=parse('Beach day for 4 adults including me in December. Any Saturday except December 12 works for me.',BASE)
    assert p['schedule_mode']=='dates' and p['duration_count']==1 and not p['question'],p
    from datetime import date,timedelta
    dates=set()
    for a,b in p['date_windows']:
        start,end=date.fromisoformat(a),date.fromisoformat(b)
        dates.update((start+timedelta(days=i)).isoformat() for i in range((end-start).days+1))
    assert dates=={'2026-12-05','2026-12-19','2026-12-26'},p
    print('PASS day outing and exclusion')
    p=parse('Two-night trip for 2 adults including me to Kentucky, November 27 through 29. Those dates work for me.',BASE)
    assert p['duration_unit']=='nights' and p['duration_count']==2 and not p['question'],p
    assert p['date_windows']==[['2026-11-27','2026-11-29']],p
    print('PASS nights versus calendar days')
    p=parse('Coffee for two adults including me on November 14 for an hour. I am free 10am to noon.',BASE)
    assert p['schedule_mode']=='timed' and p['minutes']==60 and p['windows'] and not p['question'],p
    print('PASS timed-event regression')


if __name__=='__main__':
    from server import load_env
    load_env()
    run()
