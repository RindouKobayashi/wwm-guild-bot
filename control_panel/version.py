"""Safe deployment identity; never includes remotes or uncommitted file contents."""
import hashlib
import json
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def versions():
    revision = None
    try:
        result = subprocess.run(['git','rev-parse','--short=12','HEAD'],cwd=ROOT,capture_output=True,text=True,timeout=2)
        if result.returncode == 0:
            revision = result.stdout.strip()
    except (OSError,subprocess.TimeoutExpired):
        pass
    files = ['guildbot.py','settings.py','utility/wwm.py','activity/server.py','control_panel/server.py',
             'control_panel/telemetry.py','control_panel/jobs.py','control_panel/storage.py','control_panel/public/index.html']
    files += sorted(str(path.relative_to(ROOT)).replace('\\','/') for path in (ROOT/'cogs').glob('*_cog.py'))
    files += sorted(str(path.relative_to(ROOT)).replace('\\','/') for path in (ROOT/'control_panel').glob('*.py'))
    hashes = {}
    for name in files:
        path = ROOT/name
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'missing'
    asset = None
    try:
        pointer = json.loads((ROOT/'assets/wwm/current.json').read_text())
        asset = pointer.get('manifest_sha256') or pointer.get('release') or pointer.get('version')
        if isinstance(asset,str):
            asset = asset[:20]
    except (OSError,ValueError,AttributeError):
        pass
    return {'revision':revision,'python':platform.python_version(),'build_id':hashes['control_panel/public/index.html'][:12],
            'runtime_id':hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()[:12],
            'asset_version':asset or 'Unknown'}
