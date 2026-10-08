"""Run an explicit test/production bot and Activity with its configured tunnel."""
import argparse,base64,json,os,signal,subprocess,sys,time,urllib.request
from pathlib import Path
from dotenv import dotenv_values

HERE=Path(__file__).resolve().parent
BOT=HERE.parent

def funnel_matches(config,hostname,origin):
    """Private Serve is not proof of public Funnel, including foreground routes."""
    key=hostname+':443'
    candidates=[config,*config.get('Foreground',{}).values()]
    return any(candidate.get('AllowFunnel',{}).get(key) is True and
               candidate.get('Web',{}).get(key,{}).get('Handlers',{}).get('/',{}).get('Proxy')==origin
               for candidate in candidates if isinstance(candidate,dict))

def linux_bot_running():
    """Refuse another manually started bot owned by this Linux user."""
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():continue
        try:
            if directory.stat().st_uid!=os.getuid():continue
            argv=(directory/'cmdline').read_bytes().split(b'\0')
            if argv and b'python' in Path(os.fsdecode(argv[0])).name.encode() and any(Path(os.fsdecode(arg)).name=='guildbot.py' for arg in argv[1:] if arg):return True
        except (OSError,ValueError):continue
    return False

def configuration(environment='test'):
    profile=HERE/('.env.'+environment)
    if not profile.exists():raise ValueError(f'Missing {profile.name}; copy its example and configure locally.')
    activity=dotenv_values(profile);bot=dotenv_values(BOT/'.env')
    client=activity.get('DISCORD_CLIENT_ID','')
    if not client.isdigit() or not activity.get('DISCORD_CLIENT_SECRET'):
        raise ValueError(f'Fill activity/{profile.name} with the Activity OAuth ID and secret first.')
    branches=[]
    for key,branch in [('DISCORD_API_TOKEN','main'),('DISCORD_API_TOKEN_DEV','dev')]:
        try:identity=base64.b64decode(bot.get(key,'').split('.')[0]+'===').decode()
        except Exception:continue
        if identity==client:branches.append(branch)
    if len(branches)!=1:raise ValueError('Activity ID does not uniquely match the configured production/test bot tokens.')
    expected='main' if environment=='production' else 'dev'
    if branches[0]!=expected:raise ValueError(f'{environment} profile points to the wrong bot. Refusing to start.')
    provider=activity.get('TUNNEL_PROVIDER','tailscale')
    if provider not in ('tailscale','cloudflare'):raise ValueError('TUNNEL_PROVIDER must be tailscale or cloudflare.')
    tunnel=(Path('C:/Program Files/Tailscale/tailscale.exe') if os.name=='nt' else Path('/usr/bin/tailscale')) if provider=='tailscale' else (HERE/'tools/cloudflared.exe' if os.name=='nt' else Path('/usr/local/bin/cloudflared'))
    import shutil
    if not tunnel.exists():
        found=shutil.which('tailscale' if provider=='tailscale' else 'cloudflared')
        if not found:raise ValueError(f'Install {provider} first.')
        tunnel=Path(found)
    for file in ['public/app.js','public/discord-sdk.js','public/catalogue.json']:
        if not (HERE/file).exists():raise ValueError('Missing Activity runtime file: '+file)
    return client,branches[0],tunnel,provider,activity

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');parser.add_argument('--activity-only',action='store_true');parser.add_argument('--environment',choices=['test','production'],default='test');args=parser.parse_args()
    client,branch,tunnel,provider,activity=configuration(args.environment)
    port=8767 if args.environment=='test' else 8768
    origin=f'http://127.0.0.1:{port}'
    print(f'Configuration OK: {"test" if branch!="main" else "production"} bot, application {client}',flush=True)
    if args.check:return
    if sys.platform=='linux' and not args.activity_only and linux_bot_running():
        raise ValueError('A guildbot.py process is already running for this user. Stop the manual bot first; no duplicate was started.')
    def stop_signal(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop_signal)
    if hasattr(signal,'SIGHUP'):signal.signal(signal.SIGHUP,stop_signal)
    logs=HERE/'logs'/args.environment;logs.mkdir(parents=True,exist_ok=True)
    # OS file lock is released even after a crash; a second stack cannot start another bot.
    lock=open(logs/'stack.lock','a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:raise ValueError('The combined launcher is already running. Use its existing window.')
    children=[];handles=[];env=os.environ.copy();env['GITHUB_BRANCH']=branch
    env.update({key:activity[key] for key in ('DISCORD_CLIENT_ID','DISCORD_CLIENT_SECRET')})
    def spawn(command,label):
        handle=open(logs/(label+'.log'),'wb' if label=='tunnel' else 'ab');handles.append(handle)
        process=subprocess.Popen(command,cwd=BOT,env=env,stdout=handle,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        children.append(process);return process
    try:
        ready=False
        try:
            with urllib.request.urlopen(origin+'/api/config',timeout=2) as r:state=json.load(r)
            if state.get('service')!='wwm-activity' or state.get('mode')!='discord' or state.get('client_id')!=client:
                raise ValueError(f'Port {port} belongs to a different Activity configuration. Stop it first.')
            ready=True
        except urllib.error.URLError:pass
        if not ready:
            service=spawn([sys.executable,'-B','activity/server.py','--discord','--port',str(port)],'discord')
            for _ in range(40):
                if service.poll() is not None:raise ValueError('Activity exited. See activity/logs/discord.log.')
                try:
                    with urllib.request.urlopen(origin+'/api/config',timeout=1) as r:state=json.load(r)
                    ready=state.get('mode')=='discord' and state.get('client_id')==client
                    if ready:break
                except urllib.error.URLError:pass
                time.sleep(.25)
            if not ready:raise ValueError('Activity did not become ready.')
        # Refuse a separately launched guildbot.py in this checkout on Windows.
        if os.name=='nt' and not args.activity_only:
            check=subprocess.run(['powershell.exe','-NoProfile','-Command',
                "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'guildbot\\.py' }).Count"],
                capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW,check=True)
            if int(check.stdout.strip() or '0'):raise ValueError('A guildbot.py process is already running. Stop it before using the combined launcher.')
        if provider=='tailscale':
            status=subprocess.run([str(tunnel),'status','--json'],capture_output=True,text=True,check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            ts=json.loads(status.stdout)
            if ts.get('BackendState')!='Running':raise ValueError('Sign in to Tailscale first, then rerun this launcher.')
            hostname=ts.get('Self',{}).get('DNSName','').rstrip('.')
            if not hostname.endswith('.ts.net'):raise ValueError('Tailscale HTTPS device hostname is not available.')
            print(f'Stable {args.environment} Activity URL: https://{hostname}',flush=True)
            print('Set this once in the matching Discord application URL Mapping. Reboots retain this hostname.',flush=True)
            # Reuse an already-running route only when it serves this application.
            existing=subprocess.run([str(tunnel),'funnel','status','--json'],capture_output=True,text=True,check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            reused=funnel_matches(json.loads(existing.stdout),hostname,origin)
            if reused:print('Existing matching Funnel reused; it will remain running after this launcher exits.',flush=True)
            else:spawn([str(tunnel),'funnel','--https=443',origin],'tunnel')
        else:spawn([str(tunnel),'tunnel','--url',origin,'--no-autoupdate'],'tunnel')
        if not args.activity_only:spawn([sys.executable,'-B','guildbot.py'],'bot')
        print(f'{"Activity + tunnel" if args.activity_only else "Bot + Activity + tunnel"} ready. Logs: {logs}. Ctrl+C stops processes started here.',flush=True)
        print('First Funnel use may need HTTPS/Funnel approval at the URL in the tunnel log.',flush=True)
        if provider=='tailscale':print('Local HTTPS access is not a public-access test. Funnel DNS may take time to propagate; Discord cannot load the Activity until the hostname has public DNS records.',flush=True)
        position=0
        while all(p.poll() is None for p in children):
            if (logs/'tunnel.log').exists() and not (provider=='tailscale' and reused):
                with open(logs/'tunnel.log','r',encoding='utf8',errors='replace') as stream:
                    stream.seek(position);chunk=stream.read();position=stream.tell()
                    for line in chunk.splitlines():
                        if 'https://' in line:print(line,flush=True)
            time.sleep(.5)
        raise ValueError('A component stopped; check activity/logs. Stopping the other owned components.')
    except KeyboardInterrupt:print('\nStopping the stack...',flush=True)
    finally:
        for child in reversed(children):
            if child.poll() is None:child.terminate()
        for child in reversed(children):
            try:child.wait(timeout=8)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        for handle in handles:handle.close()
        lock.close()

if __name__=='__main__':
    try:main()
    except Exception as error:print(str(error),file=sys.stderr);sys.exit(1)
