"""Interactive, repeatable Pi setup. Never starts the bot or modifies the root .env."""
import argparse,base64,getpass,json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
from dotenv import dotenv_values,set_key

BOT=Path(__file__).resolve().parents[2]
ACTIVITY=BOT/'activity'
PRODUCTION_ID='1488802246081773669'

def validate_bot(values):
    branch=values.get('GITHUB_BRANCH','main')
    if branch!='main':raise ValueError('The Pi root .env must have GITHUB_BRANCH=main for production. Change it locally if intended; setup will not overwrite it.')
    try:identity=base64.b64decode(values.get('DISCORD_API_TOKEN','').split('.')[0]+'===').decode()
    except Exception:identity=''
    if identity!=PRODUCTION_ID:raise ValueError('Root .env DISCORD_API_TOKEN must belong to Goose Overlord. Test tokens will not be used.')

def validate_assets():
    for filename in ('server.py','player_profiles.py','player_mappings.json','runtime_version.py'):
        if not (ACTIVITY/filename).is_file():raise ValueError(f'Missing Git runtime file: activity/{filename}. Pull the complete Activity update.')
    public=ACTIVITY/'public'
    for filename in ('index.html','style.css','app.js','discord-sdk.js','catalogue.json'):
        if not (public/filename).is_file():raise ValueError(f'Missing Git runtime asset: activity/public/{filename}. Push/pull the Activity public files too.')
    catalogue=json.loads((public/'catalogue.json').read_text(encoding='utf8'))
    for board in catalogue['boards']:
        if board.get('image'):
            target=(public/board['image'].lstrip('/')).resolve()
            if not target.is_relative_to(public.resolve()) or not target.is_file():raise ValueError(f'Missing/invalid artwork for {board["key"]}. Pull the public/art files.')

def configure_profile(bot_values):
    profile=ACTIVITY/'.env.production'
    existing=dotenv_values(profile) if profile.exists() else {}
    if existing.get('DISCORD_CLIENT_ID') not in (None,'',PRODUCTION_ID):raise ValueError('Existing production profile targets another application; refusing to overwrite it.')
    secret=existing.get('DISCORD_CLIENT_SECRET') or bot_values.get('DISCORD_CLIENT_SECRET')
    if not secret:
        print('Enter Goose Overlord OAuth2 CLIENT SECRET, not its bot token. Input is hidden.')
        secret=getpass.getpass('OAuth client secret: ').strip()
    if not secret or '\n' in secret or '\r' in secret:raise ValueError('A nonempty single-line OAuth secret is required.')
    if not profile.exists():
        descriptor=os.open(profile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(descriptor)
    profile.chmod(0o600)
    for key,value in {'DISCORD_CLIENT_ID':PRODUCTION_ID,'DISCORD_CLIENT_SECRET':secret,'TUNNEL_PROVIDER':'tailscale'}.items():set_key(profile,key,value)
    profile.chmod(0o600)
    print('Production Activity profile saved; root .env and test credentials unchanged.')

def install_tailscale():
    if shutil.which('tailscale'):return
    answer=input('Install official Tailscale client (uses sudo)? [y/N] ').strip().lower()
    if answer not in ('y','yes'):raise ValueError('Install Tailscale, then rerun setup: https://tailscale.com/docs/install/linux')
    with tempfile.TemporaryDirectory(prefix='wwm-tailscale-') as directory:
        script=Path(directory)/'install.sh'
        subprocess.run(['curl','--fail','--show-error','--silent','--location','https://tailscale.com/install.sh','--output',str(script)],check=True)
        try:
            subprocess.run(['sh',str(script)],check=True)
        except subprocess.CalledProcessError as error:
            raise ValueError('Tailscale installation failed. Check the installer output above: any broken APT repository can block apt update. Repair that repository and rerun setup; the saved Activity credentials are preserved. Do not disable APT signature verification.') from error

def tailscale_setup():
    status=subprocess.run(['tailscale','status','--json'],capture_output=True,text=True)
    state=json.loads(status.stdout) if status.returncode==0 else {}
    if state.get('BackendState')!='Running':
        print('Sign in using the URL printed by Tailscale. Keep this SSH session open.')
        subprocess.run(['sudo','tailscale','up'],check=True)
    # Permit the normal bot user to manage this device's Funnel without root.
    subprocess.run(['sudo','tailscale','set','--operator='+getpass.getuser()],check=True)
    state=json.loads(subprocess.check_output(['tailscale','status','--json'],text=True))
    host=state.get('Self',{}).get('DNSName','').rstrip('.')
    if state.get('BackendState')!='Running' or not host.endswith('.ts.net'):raise ValueError('Tailscale sign-in or MagicDNS hostname is not ready. Rerun setup after signing in.')
    return host

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true',help='Validate bot configuration and Git runtime assets only; no writes/install/login');args=parser.parse_args()
    values=dotenv_values(BOT/'.env')
    validate_bot(values);validate_assets()
    if args.check:print('Production bot identity and Git runtime assets OK. No changes made.');return
    if sys.platform!='linux' or os.geteuid()==0:raise ValueError('Run this setup on the Pi as the normal bot user, without sudo.')
    configure_profile(values);install_tailscale();host=tailscale_setup()
    subprocess.run([sys.executable,'-B',str(ACTIVITY/'run_stack.py'),'--environment','production','--check'],cwd=BOT,check=True)
    print('\nSetup complete. No bot has been started.')
    print('Stop any manually running guildbot.py first, then run:')
    print('  bash "Start WWM Bot and Activity.sh"')
    print('On first run, approve Funnel using the URL printed in the SSH terminal.')
    print('Goose Overlord Developer Portal: enable Activities, OAuth redirect https://127.0.0.1, root / URL Mapping:')
    print('  '+host)
    print('Keep tester mapped to this Windows computer; do not change its mapping.')
    print('Logs: activity/logs/production. Ctrl+C stops owned processes. Keep SSH open, or use tmux.')

if __name__=='__main__':
    try:main()
    except (ValueError,subprocess.CalledProcessError,KeyboardInterrupt) as error:
        print('Setup stopped: '+str(error),file=sys.stderr);sys.exit(1)
