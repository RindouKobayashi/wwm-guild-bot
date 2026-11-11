"""Private, bounded in-process energy decisions; never emitted to telemetry."""
import time
items={}
cycle={}

def report(user_id,reason,**fields):
    items[str(user_id)]=dict(time=time.time(),reason=reason,**fields)
    while len(items)>1000:items.pop(next(iter(items)))

def cycle_state(reason):
    cycle.update(time=time.time(),reason=reason)

def snapshot():
    now=time.time()
    return dict(items={key:value for key,value in items.items() if now-value['time']<86400},cycle=dict(cycle))
