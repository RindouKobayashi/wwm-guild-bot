"""Identify the loaded Activity runtime across test and production."""
import hashlib
from pathlib import Path


def runtime_version():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ('server.py','player_profiles.py','player_mappings.json','runtime_version.py',
                 'public/index.html','public/app.js','public/style.css','public/catalogue.json'):
        # Normalize Git's Windows/Linux text line endings.
        digest.update(name.encode())
        digest.update((root/name).read_bytes().replace(b'\r\n',b'\n'))
    return digest.hexdigest()[:16]
