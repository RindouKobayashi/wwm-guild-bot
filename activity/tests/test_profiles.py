import base64,importlib.util,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('activity_stack',Path(__file__).resolve().parents[1]/'run_stack.py')
stack=importlib.util.module_from_spec(spec);spec.loader.exec_module(stack)

def token(identity):return base64.b64encode(identity.encode()).decode().rstrip('=')+'.offline-test'

class ProfileTests(unittest.TestCase):
    def config(self,environment,client):
        values=[{'DISCORD_CLIENT_ID':client,'DISCORD_CLIENT_SECRET':'offline','TUNNEL_PROVIDER':'tailscale'},
                {'DISCORD_API_TOKEN':token('production'),'DISCORD_API_TOKEN_DEV':token('12345')}]
        with patch.object(stack,'dotenv_values',side_effect=values),patch.object(Path,'exists',return_value=True):
            return stack.configuration(environment)
    def test_test_profile_uses_dev(self):self.assertEqual(self.config('test','12345')[1],'dev')
    def test_production_profile_rejects_tester(self):
        with self.assertRaisesRegex(ValueError,'wrong bot'):self.config('production','12345')
