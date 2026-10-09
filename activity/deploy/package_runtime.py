"""Build a Pi runtime ZIP without credentials, saved-player snapshots or Windows dependencies."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
target = root / 'dist' / 'wwm-activity-runtime.zip'
files = ['server.py', 'player_profiles.py', 'player_mappings.json', 'runtime_version.py', '.env.example', '.env.test.example', '.env.production.example', 'public/index.html', 'public/app.js',
         'public/discord-sdk.js', 'public/style.css', 'public/catalogue.json',
         'deploy/run-discord.sh', 'deploy/install-service.sh', 'deploy/PI-SETUP.md']
paths = [root / name for name in files] + sorted((root / 'public/art').glob('*.png'))
for path in paths:
    if not path.is_file():
        raise SystemExit(f'Missing runtime file: {path.relative_to(root)}')
target.parent.mkdir(exist_ok=True)
with ZipFile(target, 'w', ZIP_DEFLATED) as bundle:
    for path in paths:
        bundle.write(path, 'activity/' + path.relative_to(root).as_posix())
print(f'Packaged {len(paths)} files: {target}')
