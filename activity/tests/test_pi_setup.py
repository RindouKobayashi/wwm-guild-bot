import base64,importlib.util,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('pi_setup',Path(__file__).resolve().parents[1]/'deploy/setup_pi.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)

def values(identity=setup.PRODUCTION_ID,branch='main'):
    return {'DISCORD_API_TOKEN':base64.b64encode(identity.encode()).decode().rstrip('=')+'.offline','GITHUB_BRANCH':branch}

class PiSetupTests(unittest.TestCase):
    def test_accepts_production_bot(self):setup.validate_bot(values())
    def test_rejects_tester_and_dev_branch(self):
        for config in (values('1289424877916393547'),values(branch='dev')):
            with self.assertRaises(ValueError):setup.validate_bot(config)
    def test_git_runtime_is_complete(self):setup.validate_assets()
    def test_rerun_preserves_secret_without_prompt(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(setup,'ACTIVITY',Path(directory)):
            profile=Path(directory)/'.env.production';profile.write_text('DISCORD_CLIENT_ID=1488802246081773669\nDISCORD_CLIENT_SECRET=offline-secret\n')
            with patch.object(setup.getpass,'getpass',side_effect=AssertionError('must not prompt')):
                setup.configure_profile(values())
            self.assertEqual(setup.dotenv_values(profile)['DISCORD_CLIENT_SECRET'],'offline-secret')
    def test_refuses_foreign_profile_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(setup,'ACTIVITY',Path(directory)):
            profile=Path(directory)/'.env.production';text='DISCORD_CLIENT_ID=1289424877916393547\n';profile.write_text(text)
            with self.assertRaises(ValueError):setup.configure_profile(values())
            self.assertEqual(profile.read_text(),text)
