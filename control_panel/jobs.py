"""Explicit job instrumentation; wraps coroutine bodies, not discord.py internals."""
import asyncio
import functools
import time
import uuid
import contextvars
from .telemetry import emit, enabled, parent_id

states = {}
current_job = contextvars.ContextVar('panel_job',default=None)

def report_error():
    state = states.get(current_job.get())
    if state and state['phase']=='running':
        state['handled_errors'] = state.get('handled_errors',0)+1

def observe_job(name):
    def decorate(function):
        @functools.wraps(function)
        async def run(*args, **kwargs):
            if not enabled():
                return await function(*args, **kwargs)
            started = time.time()
            previous = states.get(name, {})
            state = {'name':name, 'id':uuid.uuid4().hex, 'phase':'running', 'last_start':started,
                     'last_success':previous.get('last_success'), 'consecutive_failures':previous.get('consecutive_failures',0)}
            states[name] = state
            token = parent_id.set(state['id'])
            job_token = current_job.set(name)
            emit('job',dict(state))
            try:
                result = await function(*args, **kwargs)
                if state.get('handled_errors'):
                    state.update(phase='failed',consecutive_failures=state['consecutive_failures']+1)
                else:
                    state.update(phase='completed',last_success=time.time(),consecutive_failures=0)
                return result
            except asyncio.CancelledError:
                state.update(phase='cancelled')
                raise
            except BaseException as error:
                state.update(phase='failed',exception=type(error).__name__,
                             consecutive_failures=state['consecutive_failures']+1)
                raise
            finally:
                state.update(duration_ms=round((time.time()-started)*1000,2),finished=time.time())
                emit('job',dict(state))
                parent_id.reset(token)
                current_job.reset(job_token)
        run.panel_job_name = name
        return run
    return decorate
