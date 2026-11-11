"""Actual SSH, remote reader and Linux PTY checks in a disposable Pi checkout."""
import asyncio
import base64
import io
import json
from pathlib import Path
import shlex
import zipfile
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from control_panel.device_transport import SSHWorker,profiles

async def main():
    profile=profiles().get('pi')
    if not profile:raise RuntimeError('Configure the approved pi target first.')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in (ROOT/'control_panel').glob('*.py'):archive.write(path,'control_panel/'+path.name)
        archive.write(ROOT/'control_panel/tests/test_device.py','control_panel/tests/test_device.py')
    worker=SSHWorker();state={};directory=None
    try:
        await worker.call(worker.connect('check',profile,state))
        if state.get('state')!='connected':raise RuntimeError(json.dumps(state))
        stage="""import base64,io,os,pathlib,sys,tempfile,zipfile
root=pathlib.Path(tempfile.mkdtemp(prefix='wwm-device-check-'))
with zipfile.ZipFile(io.BytesIO(base64.b64decode(sys.stdin.buffer.read()))) as archive:archive.extractall(root)
repo=pathlib.Path.home()/'Desktop/wwm-guild-bot'
(root/'.venv').symlink_to(repo/'.venv',target_is_directory=True)
log=root/'activity/logs/production/bot.log';log.parent.mkdir(parents=True);log.write_text('SSH reader fixture\\nAuthorization: Bearer remote-fixture-secret\\n')
(root/'logs').mkdir()
print(str(root))
"""
        async def command(code,input=None):
            return await worker.connection('check').run('python3 -c '+shlex.quote(code),input=input,encoding=None,check=False,timeout=90)
        result=await worker.call(command(stage,base64.b64encode(buffer.getvalue())));directory=result.stdout.decode().strip()
        temporary=dict(profile,repository=directory)
        listing=await worker.call(worker.read('check',temporary,dict(action='list')))
        key=next(i['id'] for i in listing['items'] if i['name'].endswith('bot.log'))
        page=await worker.call(worker.read('check',temporary,dict(action='read',id=key)))
        assert 'SSH reader fixture' in page['text'] and 'remote-fixture-secret' not in page['text']
        raw=await worker.call(worker.read('check',temporary,dict(action='read',id=key,raw=True)))
        assert 'remote-fixture-secret' in raw['text']
        await worker.call(worker.terminal('check','probe',24,80))
        await worker.call(worker.term_write('probe',b"unset HISTFILE; printf '%s%s\\n' WWM-SSH- FIXTURE\r"))
        output=b''
        for _ in range(50):
            output+=await asyncio.wait_for(worker.call(worker.term_read('probe')),10)
            if b'WWM-SSH-FIXTURE' in output:break
        assert b'WWM-SSH-FIXTURE' in output
        await worker.call(worker.term_resize('probe',30,100));await worker.call(worker.term_close('probe'))
        run="""import os,pathlib,subprocess
directory=pathlib.Path(DIRECTORY)
repo=pathlib.Path.home()/'Desktop/wwm-guild-bot'
env=dict(os.environ,PYTHONPATH=str(directory)+os.pathsep+str(repo),SSLKEYLOGFILE='')
result=subprocess.run([str(repo/'.venv/bin/python'),'-B','-m','unittest','discover','-s',str(directory/'control_panel/tests'),'-p','test_device.py','-v'],cwd=directory,env=env,timeout=60)
raise SystemExit(result.returncode)
""".replace('DIRECTORY',repr(directory))
        result=await worker.call(command(run));print(result.stdout.decode());print(result.stderr.decode());assert result.exit_status==0, 'Linux device tests failed'
        print('Actual SSH shell, resize, remote redacted/raw fixture reads and Linux device acceptance tests passed.')
    finally:
        if directory:
            cleanup="import pathlib,shutil; p=pathlib.Path("+repr(directory)+"); assert p.parent==pathlib.Path('/tmp') and p.name.startswith('wwm-device-check-') and not p.is_symlink(); shutil.rmtree(p)"
            await worker.call(command(cleanup))
        await worker.shutdown()

if __name__=='__main__':asyncio.run(main())
