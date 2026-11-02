"""Run mocked API/action acceptance tests in a disposable Pi directory.

Usage (on the development machine): python control_panel/tests/pi_tools_check.py
No production configuration, history, services or Discord messages are changed.
"""
import base64
import io
from pathlib import Path
import shlex
import subprocess
import zipfile

root=Path(__file__).resolve().parents[2]
buffer=io.BytesIO()
with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
    for path in (root/'control_panel').glob('*.py'):
        archive.write(path,str(path.relative_to(root)).replace('\\','/'))
    archive.write(root/'control_panel/tests/test_tools.py','control_panel/tests/test_tools.py')
    for name in ('leaderboard_cog.py','schedule_cog.py','market_cog.py'):
        archive.write(root/'cogs'/name,'cogs/'+name)
remote='''import base64,io,os,pathlib,subprocess,sys,tempfile,zipfile
repo=pathlib.Path.home()/'Desktop/wwm-guild-bot'
with tempfile.TemporaryDirectory(prefix='wwm-tools-check-') as directory:
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(sys.stdin.buffer.read()))) as archive:archive.extractall(directory)
    pathlib.Path(directory,'logs').mkdir()
    env=dict(os.environ,PYTHONPATH=directory+os.pathsep+str(repo),SSLKEYLOGFILE='')
    result=subprocess.run([str(repo/'.venv/bin/python'),'-B','-m','unittest','discover','-s',str(pathlib.Path(directory,'control_panel/tests')),'-p','test_tools.py','-q'],cwd=directory,env=env,timeout=90)
    sys.exit(result.returncode)
'''
result=subprocess.run(['ssh','-o','HostKeyAlias=wwm-production','-o','BatchMode=yes','-o','ConnectTimeout=10',
    'smolsus@wwm-production.tail265e5f.ts.net','python3 -c '+shlex.quote(remote)],input=base64.b64encode(buffer.getvalue()),timeout=110)
raise SystemExit(result.returncode)
