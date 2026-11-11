"""SSH runs on its own event loop; no credentials, terminal text or banners are logged."""
import asyncio
import ipaddress
import json
import re
import shlex
import socket
import threading
from pathlib import Path
from .file_reader import PROTOCOL

CONFIG=Path(__file__).parent/'devices.local.json'

def profiles(path=CONFIG):
    if not path.exists():return {}
    if path.stat().st_size>65536:raise ValueError('Device profiles exceed 64 KiB.')
    rows=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(rows,list) or len(rows)>10:raise ValueError('Configure at most ten SSH targets.')
    result={}
    for row in rows:
        if not isinstance(row,dict):raise ValueError('Invalid device profile.')
        key=row.get('id','');host=row.get('hostname','');user=row.get('username','');repo=row.get('repository','')
        if key=='local' or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',key) or key in result:raise ValueError('Invalid target ID.')
        if not re.fullmatch(r'[a-zA-Z0-9.-]+\.ts\.net',host):raise ValueError('SSH targets must use exact Tailscale DNS names.')
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',user) or not repo.startswith('/') or '\x00' in repo or '\n' in repo:raise ValueError('Invalid target account or repository.')
        if row.get('environment') not in ('test','production'):raise ValueError('Target environment must be test or production.')
        alias=row.get('host_key_alias',host)
        if not re.fullmatch(r'[a-zA-Z0-9._-]+',alias):raise ValueError('Invalid trusted host-key alias.')
        row=dict(row,host_key_alias=alias,known_hosts=str(Path(row.get('known_hosts',str(Path.home()/'.ssh/known_hosts'))).expanduser()))
        if not Path(row['known_hosts']).is_absolute():raise ValueError('known_hosts must be an absolute host-side path.')
        row.setdefault('name',key);row.setdefault('confirmation',host.split('.')[0])
        result[key]=row
    return result

class SSHWorker:
    def __init__(self):
        self.loop=asyncio.new_event_loop();self.thread=threading.Thread(target=self.loop.run_forever,daemon=True,name='panel-ssh');self.thread.start()
        self.connections={};self.processes={}
    async def call(self,coroutine):
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine,self.loop))
    async def connect(self,key,profile,state):
        try:import asyncssh
        except ImportError:
            state.update(state='disconnected',message='Install requirements-panel.txt on this panel host to enable SSH connections.');return
        worker=self
        class Client(asyncssh.SSHClient):
            def auth_banner_received(self,message,lang):
                match=re.search(r'https://login\.tailscale\.com/a/[A-Za-z0-9_-]+',message)
                if match:state.update(state='authentication_required',authentication_url=match[0],message='Complete the Tailscale check, then this connection will continue.')
        transport_socket=None
        try:
            # Restrict approved DNS targets to Tailscale address space. AsyncSSH
            # validates the configured host key separately from this network check.
            rows=await asyncio.wait_for(self.loop.getaddrinfo(profile['hostname'],22,type=socket.SOCK_STREAM),5)
            networks=[ipaddress.ip_network('100.64.0.0/10'),ipaddress.ip_network('fd7a:115c:a1e0::/48')]
            if not rows or any(not any(ipaddress.ip_address(row[4][0]) in n for n in networks) for row in rows):raise ValueError('Target did not resolve to Tailscale.')
            transport_socket=socket.socket(rows[0][0],socket.SOCK_STREAM);transport_socket.setblocking(False)
            await asyncio.wait_for(self.loop.sock_connect(transport_socket,rows[0][4]),10)
            connection=await asyncssh.connect(profile['hostname'],22,sock=transport_socket,username=profile['username'],host_key_alias=profile['host_key_alias'],
                known_hosts=profile['known_hosts'],client_factory=Client,client_keys=[],agent_path=None,password=None,config=[],
                connect_timeout=180,login_timeout=180,keepalive_interval=15,keepalive_count_max=3,encoding=None)
            worker.connections[key]=connection;state.update(state='connected',message='SSH connected. File access requires the matching reader helper on this target.')
            state.pop('authentication_url',None)
        except asyncio.CancelledError:
            if transport_socket:transport_socket.close()
            raise
        except asyncssh.HostKeyNotVerifiable:state.update(state='host_verification_failed',message='The SSH host key is missing or changed. Verify it from the host; verification was not bypassed.')
        except (asyncssh.PermissionDenied,asyncssh.KeyExchangeFailed):state.update(state='access_denied',message='SSH access was denied. Check the target user and Tailscale SSH policy.')
        except Exception:state.update(state='disconnected',message='SSH connection failed. Check Tailscale, trusted host keys and the target account.')
        finally:
            if key not in self.connections and transport_socket:transport_socket.close()
    def connection(self,key):
        value=self.connections.get(key)
        if not value or value.is_closed():raise ValueError('SSH connection is no longer available. Reconnect explicitly.')
        return value
    async def read(self,key,profile,request):
        command=shlex.join([profile['repository']+'/.venv/bin/python','-B','-m','control_panel.file_reader','--environment',profile['environment']])
        command='cd '+shlex.quote(profile['repository'])+' && '+command
        process=await self.connection(key).create_process(command,encoding=None,request_pty=False)
        try:
            process.stdin.write(json.dumps(dict(request,protocol=PROTOCOL)).encode()+b'\n');process.stdin.write_eof()
            output=await asyncio.wait_for(process.stdout.read(3*1024*1024+1),8)
            if len(output)>3*1024*1024:raise ValueError('Remote reader response exceeded its limit.')
            result=json.loads(output)
            if result.get('protocol',PROTOCOL)!=PROTOCOL:raise ValueError('Deploy the matching file reader on the target.')
            if result.get('error'):raise ValueError(result['error'])
            return result
        except (json.JSONDecodeError,UnicodeError):raise ValueError('Remote reader unavailable. Deploy the matching repository release on the Pi; its panel need not be running.') from None
        finally:process.close();await process.wait_closed()
    async def terminal(self,key,session,rows,cols):
        process=await self.connection(key).create_process(term_type='xterm-256color',term_size=(cols,rows),encoding=None,request_pty=True)
        self.processes[session]=process
    async def term_read(self,session):return await self.processes[session].stdout.read(16384)
    async def term_write(self,session,data):self.processes[session].stdin.write(data);await self.processes[session].stdin.drain()
    async def term_resize(self,session,rows,cols):self.processes[session].change_terminal_size(cols,rows)
    async def term_close(self,session):
        process=self.processes.pop(session,None)
        if process:process.close();await process.wait_closed()
    async def disconnect(self,key):
        connection=self.connections.pop(key,None)
        if connection:connection.close();await connection.wait_closed()
    async def close(self):
        for key in list(self.connections):await self.disconnect(key)
    async def shutdown(self):
        await self.call(self.close());self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.to_thread(self.thread.join,2)
