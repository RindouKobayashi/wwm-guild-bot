"""Run only the synthetic WWM checks; never discover the live API probe scripts."""
from pathlib import Path
import sys
import unittest

BOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'guildbot.py').is_file())
packages = BOT / '.venv/Lib/site-packages'
if packages.is_dir():
    sys.path.insert(0, str(packages))
suite = unittest.defaultTestLoader.discover(str(BOT / 'maintenance/checks/tests'), pattern='test_wwm_offline_improvements.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
