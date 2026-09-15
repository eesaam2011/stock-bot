import ast, hashlib, json, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
SRC=ROOT/'independent_priority_radar.py'
TEXT=SRC.read_text()
TREE=ast.parse(TEXT)

def literal(name):
    for n in TREE.body:
        if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(name)

class TestH1BackwardOOSPrefreezeRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.57"',TEXT)
        self.assertIn('2018-H1-BACKWARD-OOS-PREFREEZE-A',TEXT)
    def test_h1_is_exactly_frozen(self):
        s=literal('BACKWARD_OOS_2018_H1_PREFREEZE_SPEC'); h=s['frozen_h1']
        self.assertEqual(h['feature'],'close_location_in_post_t0_range')
        self.assertAlmostEqual(h['threshold'],1/3)
        self.assertEqual(h['direction'],'<=')
        self.assertIn('first completed 1-minute candle after t0',h['confirmation_time'])
    def test_primary_and_gates(self):
        s=literal('BACKWARD_OOS_2018_H1_PREFREEZE_SPEC'); p=s['primary_endpoint']; g=s['mandatory_gates_per_view']
        self.assertEqual((p['horizon_minutes'],p['target_pct'],p['adverse_pct']),(30,5.0,-5.0))
        self.assertIn('+0.05',g['primary_improvement']); self.assertIn('0.25',g['retention'])
    def test_all_four_views_are_mandatory(self):
        s=literal('BACKWARD_OOS_2018_H1_PREFREEZE_SPEC'); t=s['t0_and_population_parity']
        self.assertEqual(t['mandatory_windows_minutes'],[30,60]); self.assertEqual(t['mandatory_selections'],['top_1','top_3'])
        self.assertIn('every one of the four',t['validation_aggregation'])
    def test_required_prior_result_is_exact(self):
        s=literal('BACKWARD_OOS_2018_H1_PREFREEZE_SPEC')
        self.assertEqual(s['required_2018_sample_controls_result_sha256'],'fa0b4341e8df45528986535cc3670233dc49b968a32b1995f85a789b6a0dd7c2')
        self.assertEqual(s['required_2018_execution_spec_sha256'],'fecebaff1d2c3b32c60a0eea67b60d9a28a47ec1ac8d50c35484e7b9321aa6d6')
    def test_protocol_only_no_start_route(self):
        self.assertIn('/research/2018-backward-oos/h1-prefreeze/protocol',TEXT)
        self.assertNotIn('/research/2018-backward-oos/h1-prefreeze/start',TEXT)
    def test_firewalls(self):
        s=literal('BACKWARD_OOS_2018_H1_PREFREEZE_SPEC')
        self.assertFalse(s['alpaca_requests_authorized']); self.assertFalse(s['h1_computed']); self.assertFalse(s['backward_oos_h1_opened']); self.assertFalse(s['fresh_forward_oos_opened'])
    def test_route_before_main(self):
        self.assertLess(TEXT.index('/research/2018-backward-oos/h1-prefreeze/protocol'),TEXT.index('if __name__ == "__main__":'))
    def test_historical_specs_not_rewritten(self):
        # Frozen historical source declarations remain before the new v1.7.57 block; this release only appends a new protocol.
        self.assertIn('FEATURE_DISCOVERY_EXEC_SPEC = {',TEXT)
        self.assertIn('EARLY_FEATURE_ENTRY_CONFIRMATION_RESEARCH_EXEC_SPEC =',TEXT)
        self.assertIn('BACKWARD_OOS_2018_SAMPLING_PARITY_SPEC = {',TEXT)
    def test_antiposthoc_contains_core_bans(self):
        s=literal('BACKWARD_OOS_2018_H1_PREFREEZE_SPEC'); x=' '.join(s['anti_posthoc_firewall'])
        for token in ['1/3','t0+1m','+5/-5','25%','best Top1/Top3','Fresh Forward OOS']:
            self.assertIn(token,x)

if __name__=='__main__':
    unittest.main()
