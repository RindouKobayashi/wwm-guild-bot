"""Shared filters and complete-window aggregates; no request bodies are returned."""
import math
import time

def window(since=0, until=None, retention=72*3600):
    end = time.time() if until is None else float(until)
    start = max(float(since), end - retention)
    if not math.isfinite(start) or not math.isfinite(end) or start > end:
        raise ValueError('Invalid time range')
    return start, end

def conditions(kind, since=0, until=None, search='', source='', level='', outcome='', http_status='',
               min_duration=0, retried='', endpoint_name=''):
    start, end = window(since, until, 14*86400 if kind == 'metric' else 72*3600)
    clauses = ['kind=?', 'time>=?', 'time<=?']
    args = [kind, start, end]
    if source:
        clauses.append('source=?'); args.append(source)
    if search:
        clauses.append('body LIKE ?'); args.append('%'+search[:100]+'%')
    for name, value in [('level',level),('outcome',outcome),('endpoint',endpoint_name)]:
        if value:
            clauses.append("json_extract(body,'$.data."+name+"')=?"); args.append(value)
    if http_status:
        status = int(http_status)
        if not 100 <= status <= 599:
            raise ValueError('Invalid HTTP status')
        clauses.append("json_extract(body,'$.data.status')=?"); args.append(status)
    duration = float(min_duration)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError('Invalid duration')
    if duration:
        clauses.append("json_extract(body,'$.data.duration_ms')>=?"); args.append(duration)
    if retried == 'true':
        clauses.append("json_array_length(json_extract(body,'$.data.attempts'))>1")
    elif retried not in ('', 'false'):
        raise ValueError('Invalid retry filter')
    return ' AND '.join(clauses), args

def aggregate(db, **filters):
    where, args = conditions('request', **filters)
    rows = db.execute("SELECT time,source,json_extract(body,'$.data.endpoint'),json_extract(body,'$.data.label'),"
        "json_extract(body,'$.data.outcome'),json_extract(body,'$.data.duration_ms'),"
        "json_array_length(json_extract(body,'$.data.attempts')),"
        "EXISTS(SELECT 1 FROM json_each(json_extract(events.body,'$.data.attempts')) WHERE json_extract(value,'$.error') IN ('TimeoutError','ServerTimeoutError')) "
        'FROM events WHERE '+where, args)
    start, end = window(filters.get('since',0), filters.get('until'))
    width = max(60, math.ceil((end-start)/120))
    buckets = {start+i*width: {'time':start+i*width,'count':0,'errors':0,'latency_sum':0.0}
               for i in range(max(1, math.ceil((end-start)/width)))}
    counts = {'total':0,'success':0,'error':0,'unknown':0,'retries':0,'timeouts':0,'activity_requests':0}
    durations, endpoints = [], {}
    for stamp, source, endpoint, label, outcome, duration, attempts, timeout in rows:
        if source == 'activity':
            counts['activity_requests'] += 1
            continue
        outcome = outcome if outcome in ('success','error') else 'unknown'
        duration = float(duration or 0)
        if not math.isfinite(duration) or duration < 0:
            duration = 0
        counts['total'] += 1; counts[outcome] += 1
        counts['retries'] += max(0,(attempts or 0)-1); counts['timeouts'] += int(timeout)
        durations.append(duration)
        group = endpoints.setdefault(endpoint or '/', {'endpoint':endpoint or '/', 'label':label or endpoint or '/', 'count':0,'errors':0})
        if label:
            group['label'] = label
        group['count'] += 1; group['errors'] += int(outcome == 'error')
        index = min(len(buckets)-1, max(0,int((stamp-start)//width)))
        bucket = buckets[start+index*width]
        bucket['count'] += 1; bucket['errors'] += int(outcome == 'error'); bucket['latency_sum'] += duration
    durations.sort()
    counts.update(median_ms=(durations[(len(durations)-1)//2]+durations[len(durations)//2])/2 if durations else None,
        p95_ms=durations[max(0,math.ceil(len(durations)*.95)-1)] if durations else None)
    for outcome in ('success','error','unknown'):
        counts[outcome+'_rate'] = counts[outcome]/counts['total'] if counts['total'] else None
    series = [dict(bucket, mean_ms=bucket['latency_sum']/bucket['count'] if bucket['count'] else None)
              for bucket in buckets.values()]
    for bucket in series:
        del bucket['latency_sum']
    return {'since':start,'until':end,'summary':counts,'series':series,
            'endpoints':sorted(endpoints.values(),key=lambda item:(-item['count'],item['endpoint']))}

def failure_reason(data):
    if data.get('status') in (401,403):
        return 'Authorization rejected; this alone does not prove token expiry.'
    errors = [item.get('error','') for item in data.get('attempts',[])] + [data.get('error','')]
    if any('Timeout' in str(error) for error in errors):
        return 'Upstream request timed out.'
    if any('Pack' in str(error) or 'Decode' in str(error) for error in errors):
        return 'Upstream response could not be decoded.'
    if data.get('application_code') not in (None,0):
        return 'Upstream application reported an error.'
    if data.get('outcome') == 'error':
        return 'Upstream request failed; inspect its attempts and correlated events.'
    if data.get('outcome') == 'unknown':
        return 'No recognized application success code was supplied.'
    return 'Request completed successfully.'
