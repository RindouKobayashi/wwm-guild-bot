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
    # Compatibility for internal callers; analytics now uses indexed scalars.
    from .metadata import aggregate as indexed_aggregate
    return indexed_aggregate(db, **filters)


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
