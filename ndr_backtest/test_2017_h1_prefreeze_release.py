import ast, pathlib, unittest
P=pathlib.Path(__file__).with_name('independent_priority_radar.py'); TEXT=P.read_text(); TREE=ast.parse(TEXT)
def literal(name):
    for n in TREE.body:
        if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets): return ast.literal_eval(n.value)
    raise AssertionError(name)
class TestH12017PrefreezeRelease(unittest.TestCase):
    def test_version_build(self): self.assertIn('VERSION = "1.7.73"',TEXT); self.assertIn('2017-H1-HISTORICAL-REPLICATION-PREFREEZE-A',TEXT)
    def test_h1_exact(self):
        h=literal('H1_2017_HISTORICAL_REPLICATION_PREFREEZE_SPEC')['frozen_h1']; self.assertEqual(h['feature'],'close_location_in_post_t0_range'); self.assertEqual(h['direction'],'<='); self.assertAlmostEqual(h['threshold'],1/3); self.assertIn('t0 + 1 minute',h['confirmation_time'])
    def test_views(self):
        t=literal('H1_2017_HISTORICAL_REPLICATION_PREFREEZE_SPEC')['t0_and_population_parity']; self.assertEqual(t['mandatory_windows_minutes'],[30,60]); self.assertEqual(t['mandatory_selections'],['top_1','top_3']); self.assertIn('every one of the four',t['validation_aggregation'])
    def test_endpoint(self):
        s=literal('H1_2017_HISTORICAL_REPLICATION_PREFREEZE_SPEC'); p=s['primary_endpoint']; self.assertEqual((p['horizon_minutes'],p['target_pct'],p['adverse_pct']),(30,5.0,-5.0)); self.assertIn('TARGET_FIRST',p['metric'])
    def test_gates(self):
        g=literal('H1_2017_HISTORICAL_REPLICATION_PREFREEZE_SPEC')['mandatory_gates_per_view']; self.assertIn('+0.05',g['primary_improvement']); self.assertIn('<=',g['safety']); self.assertIn('0.25',g['retention'])
    def test_provenance(self):
        s=literal('H1_2017_HISTORICAL_REPLICATION_PREFREEZE_SPEC'); self.assertEqual(s['required_2017_sample_controls_result_sha256'],'623cb0867c6f16a9eb1e23263a2580ad94f6cded222984005e9353805ed16cd8'); self.assertEqual(s['required_2017_execution_spec_sha256'],'df14e99d8b58fe50f99a306f1771393f86f8b33330c3e791ad5ddd14f8e86c34'); self.assertEqual(s['required_2017_sampling_parity_sha256'],'5175e89a25256c65e813a8df3855831bf08f04ec2b90cbddad3b497da451cca2')
    def test_stop_rule(self):
        x=literal('H1_2017_HISTORICAL_REPLICATION_PREFREEZE_SPEC')['cross_year_stop_rule']; self.assertTrue(x['2017_is_final_authorized_additional_historical_h1_year']); self.assertTrue(x['cross_year_historical_replication_ends_after_2017_h1_execution_regardless_of_result']); self.assertTrue(x['no_automatic_2016_or_earlier']); self.assertTrue(x['no_pooled_2017_2018_rescue'])
    def test_firewall(self):
        x=' '.join(literal('H1_2017_HISTORICAL_REPLICATION_PREFREEZE_SPEC')['anti_posthoc_firewall']);
        for token in ['1/3','t0+1m','+5/-5','+5pp','25%','pooling 2017+2018','2016','Fresh Forward OOS']: self.assertIn(token,x)
    def test_protocol_only(self):
        self.assertIn('/research/2017-historical-replication/h1-prefreeze/protocol',TEXT); self.assertNotIn('/research/2017-historical-replication/h1-prefreeze/start',TEXT); self.assertTrue(literal('H1_2017_HISTORICAL_REPLICATION_PREFREEZE_SPEC')['protocol_only'])
    def test_route_before_main(self): self.assertLess(TEXT.index('/research/2017-historical-replication/h1-prefreeze/protocol'),TEXT.index('if __name__ == "__main__":'))
    def test_unittest_class(self): self.assertTrue(issubclass(TestH12017PrefreezeRelease,unittest.TestCase))
    def test_no_execution_authorized(self):
        s=literal('H1_2017_HISTORICAL_REPLICATION_PREFREEZE_SPEC'); self.assertFalse(s['alpaca_requests_authorized']); self.assertFalse(s['h1_computed']); self.assertFalse(s['historical_replication_h1_opened']); self.assertFalse(s['fresh_forward_oos_opened'])
if __name__=='__main__': unittest.main()
