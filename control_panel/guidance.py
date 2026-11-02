"""Deterministic next steps; observations are not claims about root cause."""
def explain(data):
    status=data.get('status');error=str(data.get('error','')).lower()
    if status in (401,403) or 'auth' in error:
        return dict(title='Authentication rejected',detail='The API rejected this request. That does not prove token expiry. Check the authentication mode and local saved-login readiness.',diagnostic='auth')
    if status==429 or 'rate_limit' in error:
        return dict(title='API rate limited',detail='The upstream service asked for fewer requests. Inspect retry attempts and wait before running another lookup.',diagnostic=None)
    if 'timeout' in error or any('timeout' in str(a.get('error','')).lower() for a in data.get('attempts',[])):
        return dict(title='Request timed out',detail='The upstream call did not finish within its deadline. Compare nearby requests and host health before retrying.',diagnostic='checklist')
    if 'decode' in error:
        return dict(title='Response could not be decoded',detail='The response did not match the expected format. Check the configured profile or inspect the endpoint and attempt status.',diagnostic='configuration')
    if data.get('outcome')=='error':
        return dict(title='Request failed',detail='Inspect the request attempts and correlated logs. A local health checklist can distinguish host problems from upstream failures.',diagnostic='checklist')
    return dict(title='Review request context',detail='Compare the request outcome, timing and related events. Missing or omitted data is not proof of a failure.',diagnostic=None)
