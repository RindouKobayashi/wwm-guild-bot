"""Run an explicit test/production bot and Activity with its configured tunnel."""
import argparse,base64,json,os,signal,subprocess,sys,time,urllib.request
from pathlib import Path
from dotenv import dotenv_values

HERE=Path(__file__).resolve().parent
BOT=HERE.parent
sys.path.insert(0,str(BOT))

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
    for file in ['server.py','player_profiles.py','player_mappings.json','runtime_version.py','public/app.js','public/discord-sdk.js','public/catalogue.json']:
        if not (HERE/file).exists():raise ValueError('Missing Activity runtime file: '+file)
    return client,branches[0],tunnel,provider,activity

def main():
    # Shared with the private panel; preserve the CLI and environment preflight.
    parser=argparse.ArgumentParser()
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--activity-only',action='store_true')
    parser.add_argument('--environment',choices=['test','production'],default='test')
    args=parser.parse_args()
    client,*_=configuration(args.environment)
    print(f'Configuration OK: {args.environment} bot, application {client}',flush=True)
    if args.check:return
    import asyncio
    from control_panel.supervisor import Supervisor
    async def run():
        stack=Supervisor(args.environment)
        loop=asyncio.get_running_loop()
        finished=asyncio.Event()
        def stop(*_):loop.call_soon_threadsafe(finished.set)
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        if hasattr(signal,'SIGBREAK'):signal.signal(signal.SIGBREAK,stop)
        if hasattr(signal,'SIGHUP'):signal.signal(signal.SIGHUP,stop)
        try:
            for name in ['activity','tunnel']+([] if args.activity_only else ['bot']):
                await stack.start(name)
            print(f'Stack ready. Logs: {stack.logs}. Ctrl+C stops owned processes.',flush=True)
            if stack.hostname:
                print(f'Activity URL: https://{stack.hostname}',flush=True)
                print('Public reachability has not been externally verified.',flush=True)
            while not finished.is_set():
                if any(child.poll() is not None for child in stack.children.values()):
                    raise RuntimeError('A managed component stopped. Stopping the remaining owned components.')
                try:await asyncio.wait_for(finished.wait(),.5)
                except asyncio.TimeoutError:pass
        finally:await stack.close()
    asyncio.run(run())

if __name__=='__main__':
    try:main()
    except Exception as error:print(str(error),file=sys.stderr);sys.exit(1)
