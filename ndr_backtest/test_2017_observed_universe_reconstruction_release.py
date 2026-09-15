import ast, hashlib, json, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
SRC=ROOT/'independent_priority_radar.py'
TEXT=SRC.read_text()
TREE=ast.parse(TEXT)

def assign(name):
    for n in TREE.body:
        if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(name)

class Test2017ObservedUniverseReconstructionRelease(unittest.TestCase):
    def test_version(self): self.assertIn('VERSION = "1.7.69"',TEXT)
    def test_build(self): self.assertIn('2017-OBSERVED-UNIVERSE-RECONSTRUCTION-A',TEXT)
    def test_required_probe_result_sha(self): self.assertIn('9ec2d5010a7d3bb9cf950c604f720009cffa9202362d484f786724f84dd23aab',TEXT)
    def test_required_probe_spec_sha(self): self.assertIn('3a5995464d3dca0db635f75ac1315f8f25624f2a9303e0d20604370b8526991d',TEXT)
    def test_target_and_warmup(self):
        spec=assign('BACKWARD_OOS_2017_UNIVERSE_SPEC');self.assertEqual(spec['target_period'],{'start':'2017-01-01','end':'2017-12-31'});self.assertEqual(spec['warmup_period']['start'],'2016-12-01')
    def test_v1753_parity(self):
        spec=assign('BACKWARD_OOS_2017_UNIVERSE_SPEC');p=spec['methodology_parity'];self.assertTrue(p['same_source_union_method']);self.assertEqual(p['same_timeframe'],'1Month');self.assertTrue(p['no_stricter_security_master_standard'])
    def test_observed_not_complete_market(self):
        spec=assign('BACKWARD_OOS_2017_UNIVERSE_SPEC');self.assertFalse(spec['methodology_parity']['complete_market_claim']);self.assertIn('Observed Historical SIP Universe',spec['methodology_parity']['universe_label'])
    def test_h1_firewall(self):
        spec=assign('BACKWARD_OOS_2017_UNIVERSE_SPEC');self.assertFalse(spec['firewall']['h1_computed']);self.assertFalse(spec['firewall']['candidate_selection_computed']);self.assertFalse(spec['firewall']['fresh_forward_oos_opened'])
    def test_stop_rule(self):
        spec=assign('BACKWARD_OOS_2017_UNIVERSE_SPEC');r=spec['historical_replication_stop_rule'];self.assertEqual(r['maximum_additional_h1_historical_years'],1);self.assertFalse(r['pooled_2017_2018_replication_test_authorized'])
    def test_routes_before_main(self):
        route=TEXT.index('/research/2017-historical-replication/universe/protocol');main=TEXT.index('if __name__ == "__main__":');self.assertLess(route,main)
    def test_protocol_zero_requests(self): self.assertIn('"alpaca_requests_made":0',TEXT)
    def test_no_start_authorizes_h1(self): self.assertIn('"h1_execution_allowed":False',TEXT)

if __name__=='__main__': unittest.main()
