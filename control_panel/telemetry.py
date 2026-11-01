"""Opt-in bounded transport; network work happens on a daemon, never the bot loop."""
import contextvars
import json
import logging
import os
import queue
import threading
import time
import urllib.request
import uuid
import re
import traceback
from pathlib import Path
from .privacy import endpoint, preview, REQUEST_FIELDS

parent_id = contextvars.ContextVar('panel_parent', default=None)
attempts = contextvars.ContextVar('panel_attempts', default=None)
_queue = queue.Queue(maxsize=512)
_started = False
_lock = threading.Lock()
_dropped = 0
_boot = uuid.uuid4().hex

def enabled():
    return bool(os.environ.get('WWM_PANEL_INGEST_SECRET'))

def emit(kind, data):
    global _started, _dropped
    if not enabled():
        return
    try:
        event = {'kind': kind, 'time': time.time(), 'source': os.environ.get('WWM_PANEL_SOURCE', 'bot'),
                 'environment': os.environ.get('WWM_PANEL_ENV', 'test'), 'instance': os.environ.get('WWM_PANEL_PROCESS_ID', _boot),
                 'pid': os.getpid(), 'data': data}
        # Reject non-JSON values before they enter the transport thread.
        json.dumps(event, allow_nan=False)
        _queue.put_nowait(event)
        with _lock:
            if not _started:
                _started = True
                threading.Thread(target=_worker, daemon=True, name='panel-telemetry').start()
    except (queue.Full, TypeError, ValueError):
        _dropped += 1

def _worker():
    global _dropped
    carry = None
    while True:
        first = carry if carry is not None else _queue.get()
        carry = None
        batch = [first]
        size = len(json.dumps(first).encode())
        while len(batch) < 64:
            try:
                item = _queue.get_nowait()
                item_size = len(json.dumps(item).encode())
                if size + item_size > 512*1024:
                    carry = item
                    break
                batch.append(item)
                size += item_size
            except queue.Empty:
                break
        body = json.dumps({'events': batch, 'dropped': _dropped}, allow_nan=False).encode()
        try:
            request = urllib.request.Request('http://127.0.0.1:8770/internal/ingest', data=body,
                headers={'Authorization': 'Bearer ' + os.environ['WWM_PANEL_INGEST_SECRET'], 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=1) as response:
                response.read(100)
        except Exception:
            _dropped += len(batch)

def attempt(**data):
    records = attempts.get()
    if records is not None:
        records.append(data)

def observe_wwm(function):
    """Wrap without changing exceptions, retry count, or returned objects."""
    import functools
    @functools.wraps(function)
    async def wrapped(url, payload, *args, **kwargs):
        if not enabled():
            return await function(url, payload, *args, **kwargs)
        started = time.time()
        records = []
        token = attempts.set(records)
        result = None
        error = None
        try:
            result = await function(url, payload, *args, **kwargs)
            return result
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            attempts.reset(token)
            # Only exact configured WWM endpoints may supply decoded previews.
            import settings
            known = {getattr(settings, key) for key in dir(settings)
                     if key.endswith('_URL') and isinstance(getattr(settings, key), str)}
            known -= {getattr(settings, 'WWM_CLUB_CHAT_URL', '')}
            capture = url in known and kwargs.get('raw_payload') is None
            status = records[-1].get('status') if records else None
            code = result.get('code') if isinstance(result, dict) else None
            outcome = ('error' if error or result is None or (status is not None and status != 200)
                       or (code is not None and code != 0) else 'success' if code == 0 else 'unknown')
            emit('request', {'id': uuid.uuid4().hex, 'parent_id': parent_id.get(), 'endpoint': endpoint(url),
                'method': 'POST', 'started': started, 'duration_ms': round((time.time()-started)*1000, 2),
                'status': status, 'application_code': code if isinstance(code, (int, str)) else None,
                'outcome': outcome, 'error': error, 'attempts': records,
                'request': preview(payload, REQUEST_FIELDS) if capture else {'omitted': 'Metadata-only endpoint'},
                'response': preview(result) if capture else {'omitted': 'Metadata-only endpoint'},
                'request_bytes': records[-1].get('request_bytes') if records else None,
                'response_bytes': records[-1].get('response_bytes') if records else None})
    return wrapped

class StructuredHandler(logging.Handler):
    def emit(self, record):
        # Free-form messages may include player/chat/credential data. Only fixed
        # templates, module, level and exception class leave this process.
        # Literal diagnostic templates are useful without interpolating args.
        # f-strings and other free-form messages are intentionally summarized.
        template = str(record.msg)
        safe = record.args and not re.search(r'chat|message|content|token|password|secret', template, re.I)
        message = template[:300] if safe else 'Application event in ' + record.module
        if record.name.startswith('discord') and re.search(r'rate.?limit|429', template, re.I):
            message = 'Discord rate limit reported; library manages retry timing'
        frames = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                  for frame in traceback.extract_tb(record.exc_info[2])[-12:]] if record.exc_info else []
        emit('log', {'level': record.levelname, 'module': record.module,
                     'message': message,
                     'exception': record.exc_info[0].__name__ if record.exc_info else None,
                     'traceback': frames,
                     'request_id': parent_id.get()})

def attach_logging():
    if not enabled():
        return
    handler = StructuredHandler()
    for name in ('bot', 'discord', 'aiohttp.server'):
        logger = logging.getLogger(name)
        if not any(isinstance(h, StructuredHandler) for h in logger.handlers):
            logger.addHandler(handler)

async def heartbeat(bot=None):
    import asyncio
    while True:
        data = {'connected': bool(bot and bot.is_ready()), 'gateway_latency_ms':
                round(bot.latency*1000, 2) if bot and bot.is_ready() else None, 'dropped': _dropped}
        if bot:
            tasks = []
            from discord.ext.tasks import Loop
            for cog in bot.cogs.values():
                for name in dir(type(cog)):
                    value = getattr(cog, name, None)
                    if isinstance(value, Loop):
                        tasks.append({'name': type(cog).__name__ + '.' + name,
                                      'running': value.is_running(), 'failed': value.failed()})
            data['tasks'] = tasks
        emit('heartbeat', data)
        await asyncio.sleep(5)
