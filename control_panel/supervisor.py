"""Shared process ownership for the CLI and panel. No shell command execution."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
import logging.handlers
import threading
import psutil
from activity.run_stack import BOT, HERE, configuration, funnel_matches
from .locks import FileLock

def run_json(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError('Tailscale command failed. Check device login and serving permissions.')
    return json.loads(result.stdout or '{}')

def external_bot():
    """Conservative across checkouts: never start another bot beside a manual one."""
    for process in psutil.process_iter(['pid', 'cmdline', 'name']):
        try:
            args = process.info['cmdline'] or []
            if 'python' in (process.info['name'] or '').lower() and any(Path(arg).name == 'guildbot.py' for arg in args[1:]):
                return True
        except (psutil.Error, OSError):
            continue
    return False

class Supervisor:
    def __init__(self, environment, ingest_secret=''):
        self.environment = environment
        self.client, self.branch, self.tunnel, self.provider, profile = configuration(environment)
        self.port = 8767 if environment == 'test' else 8768
        self.origin = f'http://127.0.0.1:{self.port}'
        self.env = dict(os.environ, GITHUB_BRANCH=self.branch, WWM_PANEL_ENV=environment)
        self.env.update({k: profile[k] for k in ('DISCORD_CLIENT_ID', 'DISCORD_CLIENT_SECRET')})
        if ingest_secret:
            self.env['WWM_PANEL_INGEST_SECRET'] = ingest_secret
        self.children = {}; self.handles = {}; self.lock = None
        self.logs = HERE/'logs'/environment
        self.logs.mkdir(parents=True, exist_ok=True)
        self.guard = asyncio.Lock()
        self.hostname = ''
        self.last = {}
        self.readers = {}
        self.process_metrics = {}
        self.identities = {}
        self.expected_running = set()
        self.progress = []

    def matches_process(self, name, pid, instance):
        """Windows venv launchers may forward to a child Python interpreter."""
        child = self.children.get(name)
        if not child or child.poll() is not None or self.identities.get(name) != instance:
            return False
        try:
            process = psutil.Process(int(pid))
            return process.pid == child.pid or child.pid in [parent.pid for parent in process.parents()]
        except (psutil.Error, ValueError, TypeError):
            return False

    async def activity_state(self):
        import aiohttp
        from activity.runtime_version import runtime_version
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as http:
                async with http.get(self.origin+'/api/config') as response:
                    state = await response.json()
            valid = (state.get('service') == 'wwm-activity' and state.get('mode') == 'discord'
                     and state.get('client_id') == self.client and state.get('runtime_version') == runtime_version())
            return 'Healthy' if valid else 'Degraded'
        except Exception:
            return 'Offline'

    async def states(self):
        activity = await self.activity_state()
        bot_external = await asyncio.to_thread(external_bot)
        result = {}
        for name in ('bot', 'activity', 'tunnel'):
            child = self.children.get(name)
            owned = bool(child and child.poll() is None)
            external = False
            if not owned:
                external = bot_external if name == 'bot' else activity != 'Offline' if name == 'activity' else False
            result[name] = {'state': 'Healthy' if owned else 'Unknown' if external else 'Stopped',
                            'expected_running':name in self.expected_running,
                            'exit_code':child.poll() if child else None,
                            'ownership': 'owned' if owned else 'external' if external else 'none',
                            'pid': child.pid if owned else None, 'local_health': activity if name == 'activity' else None}
            if owned:
                try:
                    proc = self.process_metrics.setdefault(child.pid, psutil.Process(child.pid))
                    result[name].update(uptime_seconds=int(time.time()-proc.create_time()), memory_bytes=proc.memory_info().rss,
                                        cpu_percent=proc.cpu_percent())
                except psutil.Error:
                    result[name]['state'] = 'Unknown'
        if self.provider == 'tailscale':
            try:
                ts = await asyncio.to_thread(run_json, [str(self.tunnel), 'status', '--json'])
                self.hostname = ts.get('Self', {}).get('DNSName', '').rstrip('.')
                routes = await asyncio.to_thread(run_json, [str(self.tunnel), 'funnel', 'status', '--json'])
                matches = funnel_matches(routes, self.hostname, self.origin)
                result['tunnel']['configured'] = matches
                result['tunnel']['public_reachability'] = 'Unknown — not externally verified'
                if matches and result['tunnel']['ownership'] != 'owned':
                    result['tunnel'].update(ownership='external', state='Unknown')
                elif result['tunnel']['ownership'] == 'owned' and not matches:
                    result['tunnel']['state'] = 'Degraded'
            except Exception:
                result['tunnel']['state'] = 'Unknown'
        self.last = result
        return result

    def spawn(self, name, command):
        path = self.logs/(name + '.log')
        handle = logging.handlers.RotatingFileHandler(path, maxBytes=5*1024*1024, backupCount=2, encoding='utf8')
        handle.setFormatter(logging.Formatter('%(message)s'))
        identity = uuid.uuid4().hex
        env = dict(self.env, WWM_PANEL_SOURCE=name, WWM_PANEL_PROCESS_ID=identity)
        try:
            process = subprocess.Popen(command, cwd=BOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt')
        except Exception:
            handle.close(); raise
        old = self.handles.pop(name, None)
        if old:
            old.close()
        self.children[name], self.handles[name] = process, handle
        self.identities[name] = identity
        self.expected_running.add(name)
        def drain():
            try:
                while True:
                    line = process.stdout.readline(8192)
                    if not line:
                        break
                    text = line.decode('utf8', 'replace').rstrip()
                    handle.handle(logging.LogRecord(name, logging.INFO, '', 0, text, (), None))
            except (OSError, ValueError):
                pass
            finally:
                process.stdout.close()
        reader = threading.Thread(target=drain, daemon=True, name='panel-output-'+name)
        self.readers[name] = reader
        reader.start()

    async def start(self, name):
        states = await self.states()
        if states[name]['ownership'] == 'owned':
            return
        if states[name]['ownership'] == 'external':
            raise RuntimeError('Service is externally managed. Stop its original launcher before using panel controls.')
        if not self.lock:
            self.lock = FileLock(self.logs/'stack.lock')
        if name == 'bot':
            if await asyncio.to_thread(external_bot):
                raise RuntimeError('Another bot is running. No duplicate was started.')
            self.spawn(name, [sys.executable, '-B', 'guildbot.py'])
        elif name == 'activity':
            self.spawn(name, [sys.executable, '-B', 'activity/server.py', '--discord', '--port', str(self.port)])
            for _ in range(40):
                if self.children[name].poll() is not None:
                    raise RuntimeError('Activity exited during startup. Inspect local logs.')
                if await self.activity_state() == 'Healthy':
                    return
                await asyncio.sleep(.25)
            await self.stop(name)
            raise RuntimeError('Activity did not become healthy within the startup timeout.')
        elif name == 'tunnel':
            if await self.activity_state() != 'Healthy':
                raise RuntimeError('Start a matching healthy Activity before its tunnel.')
            if self.provider == 'tailscale':
                if not self.hostname.endswith('.ts.net'):
                    raise RuntimeError('Tailscale hostname unavailable. Sign in to Tailscale.')
                routes = await asyncio.to_thread(run_json, [str(self.tunnel), 'serve', 'status', '--json'])
                key = self.hostname + ':443'
                for candidate in [routes, *routes.get('Foreground', {}).values()]:
                    if key in candidate.get('Web', {}) or candidate.get('TCP', {}).get('443'):
                        raise RuntimeError('HTTPS 443 already has a route. Preserve it and stop its owner before starting a tunnel.')
                self.spawn(name, [str(self.tunnel), 'funnel', '--https=443', self.origin])
            else:
                self.spawn(name, [str(self.tunnel), 'tunnel', '--url', self.origin, '--no-autoupdate'])
        await asyncio.sleep(.5)
        if self.children[name].poll() is not None:
            raise RuntimeError('Service exited during startup. Inspect local logs.')

    async def stop(self, name):
        getattr(self,'expected_running',set()).discard(name)
        child = self.children.get(name)
        if not child or child.poll() is not None:
            if self.last.get(name, {}).get('ownership') == 'external':
                raise RuntimeError('External services cannot be stopped from the panel.')
            return 'Already stopped'
        graceful = True
        try:
            if name in ('bot', 'activity') and name in getattr(self, 'identities', {}) and child.stdin:
                message = json.dumps({'action':'shutdown','instance':self.identities[name]})+'\n'
                child.stdin.write(message.encode()); child.stdin.flush()
            else:
                child.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGTERM)
            await asyncio.to_thread(child.wait, 10)
        except (subprocess.TimeoutExpired, OSError):
            graceful = False
            # Capture only current descendants before terminating the parent;
            # psutil checks process birth time before sending termination.
            try:
                descendants = psutil.Process(child.pid).children(recursive=True)
            except psutil.Error:
                descendants = []
            for process in reversed(descendants):
                try:
                    process.terminate()
                except psutil.Error:
                    pass
            child.terminate()
            try:
                await asyncio.to_thread(child.wait, 3)
            except subprocess.TimeoutExpired:
                child.kill(); await asyncio.to_thread(child.wait, 3)
            _, alive = await asyncio.to_thread(psutil.wait_procs, descendants, timeout=2)
            for process in alive:
                try:
                    process.kill()
                except psutil.Error:
                    pass
        reader = self.readers.pop(name, None)
        if reader:
            await asyncio.to_thread(reader.join, 2)
        self.handles.pop(name).close()
        if child.stdin:
            child.stdin.close()
        if not any(p.poll() is None for p in self.children.values()) and self.lock:
            self.lock.close(); self.lock = None
        return 'Graceful shutdown' if graceful else 'Forced termination after graceful shutdown timeout'

    async def action(self, service, action):
        async with self.guard:
            self.progress = []
            messages = []
            targets = ['activity', 'tunnel', 'bot'] if service == 'stack' else [service]
            states = await self.states()
            if action != 'stop' and any(states[t]['ownership'] == 'external' for t in targets):
                raise RuntimeError('Stack contains externally managed services. Stop their original launcher before taking ownership.')
            if action in ('stop', 'restart'):
                # Individual destructive actions refuse external ownership. A
                # stack stop leaves external services alone and stops owned ones.
                for target in reversed(targets):
                    if service == 'stack' and states[target]['ownership'] == 'external':
                        messages.append(target + ': external service left running')
                        continue
                    self.progress.append({'service':target,'action':'stop','state':'running','time':time.time()})
                    try:
                        messages.append(target + ': ' + await self.stop(target))
                    except BaseException:
                        self.progress[-1]['state'] = 'failed'
                        raise
                    self.progress[-1]['state'] = 'completed'
            started = []
            try:
                if action in ('start', 'restart'):
                    for target in targets:
                        self.progress.append({'service':target,'action':'start','state':'running','time':time.time()})
                        await self.start(target)
                        self.progress[-1]['state'] = 'completed'
                        if states[target]['ownership'] != 'owned' or action == 'restart':
                            started.append(target)
                        messages.append(target + ': process started; readiness shown separately')
            except BaseException:
                if self.progress:
                    self.progress[-1]['state'] = 'failed'
                for target in reversed(started):
                    await self.stop(target)
                raise
            finally:
                if not any(p.poll() is None for p in self.children.values()) and self.lock:
                    self.lock.close(); self.lock = None
            return '; '.join(messages)

    async def close(self):
        for name in ('bot', 'tunnel', 'activity'):
            if name in self.children:
                await self.stop(name)
                handle = self.handles.pop(name, None)
                if handle:
                    handle.close()
        if self.lock:
            self.lock.close(); self.lock = None
