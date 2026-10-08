import importlib.util,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('funnel_stack',Path(__file__).resolve().parents[1]/'run_stack.py')
stack=importlib.util.module_from_spec(spec);spec.loader.exec_module(stack)

class FunnelTests(unittest.TestCase):
    def route(self,enabled=True,origin='http://127.0.0.1:8768'):
        return {'AllowFunnel':{'pi.tailtest.ts.net:443':enabled},'Web':{'pi.tailtest.ts.net:443':{'Handlers':{'/':{'Proxy':origin}}}}}
    def test_private_serve_is_not_reused(self):self.assertFalse(stack.funnel_matches(self.route(False),'pi.tailtest.ts.net','http://127.0.0.1:8768'))
    def test_foreground_public_funnel_is_reused(self):self.assertTrue(stack.funnel_matches({'Foreground':{'session':self.route()}},'pi.tailtest.ts.net','http://127.0.0.1:8768'))
    def test_other_port_is_not_reused(self):self.assertFalse(stack.funnel_matches(self.route(origin='http://127.0.0.1:8767'),'pi.tailtest.ts.net','http://127.0.0.1:8768'))
