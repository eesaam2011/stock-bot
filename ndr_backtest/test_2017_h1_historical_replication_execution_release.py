import ast, hashlib, json, pathlib, unittest
P=pathlib.Path(__file__).with_name('independent_priority_radar.py')
S=P.read_text()
T=ast.parse(S)

def literal(name):
    for n in T.body:
        if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(name)

class Test2017H1HistoricalReplicationExecutionRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.74"',S); self.assertIn('2017-H1-HISTORICAL-REPLICATION-EXECUTION-A',S)
    def test_prefreeze_hash_exact(self): self.assertEqual(literal('H1_2017_HISTORICAL_REPLICATION_EXEC_SPEC')['required_prefreeze_sha256'],'38beb5d7114dbaf50d4733d083d664773117b8c29b5a0a6e303eee278b231d67')
    def test_source_result_hash_exact(self): self.assertEqual(literal('H1_2017_HISTORICAL_REPLICATION_EXEC_SPEC')['required_2017_sample_controls_result_sha256'],'623cb0867c6f16a9eb1e23263a2580ad94f6cded222984005e9353805ed16cd8')
    def test_source_exec_hash_exact(self): self.assertEqual(literal('H1_2017_HISTORICAL_REPLICATION_EXEC_SPEC')['required_2017_execution_spec_sha256'],'df14e99d8b58fe50f99a306f1771393f86f8b33330c3e791ad5ddd14f8e86c34')
    def test_h1_exact(self):
        h=literal('H1_2017_HISTORICAL_REPLICATION_EXEC_SPEC')['h1']; self.assertEqual(h['threshold'],1/3); self.assertEqual(h['direction'],'<='); self.assertIn('t0+1m',h['confirmation_time'])
    def test_views_exact(self): self.assertEqual(literal('H1_2017_HISTORICAL_REPLICATION_EXEC_SPEC')['mandatory_views'],[{'window_minutes':30,'selection':'top_1'},{'window_minutes':30,'selection':'top_3'},{'window_minutes':60,'selection':'top_1'},{'window_minutes':60,'selection':'top_3'}])
    def test_gates_exact(self):
        g=literal('H1_2017_HISTORICAL_REPLICATION_EXEC_SPEC')['gates']; self.assertEqual(g['primary_improvement_min_absolute'],.05); self.assertEqual(g['retention_min'],.25); self.assertTrue(g['adverse_first_no_increase'])
    def test_primary_exact(self):
        e=literal('H1_2017_HISTORICAL_REPLICATION_EXEC_SPEC')['primary_endpoint']; self.assertEqual((e['horizon_minutes'],e['target_pct'],e['adverse_pct']),(30,5.0,-5.0))
    def test_stop_rule(self):
        r=literal('H1_2017_HISTORICAL_REPLICATION_EXEC_SPEC')['cross_year_stop_rule']; self.assertTrue(r['cross_year_historical_replication_ends_after_2017_h1_execution_regardless_of_result']); self.assertTrue(r['no_automatic_2016_or_earlier']); self.assertTrue(r['no_pooled_2017_2018_rescue'])
    def test_firewall(self):
        f=literal('H1_2017_HISTORICAL_REPLICATION_EXEC_SPEC')['firewall']; self.assertFalse(f['fresh_forward_oos_opened']); self.assertFalse(f['pooled_2017_2018_computed']); self.assertFalse(f['threshold_tuning'])
    def test_routes_before_main(self):
        main=S.index('if __name__ == "__main__":'); self.assertLess(S.index('/research/2017-historical-replication/h1-execution/protocol'),main); self.assertLess(S.index('/research/2017-historical-replication/h1-execution/start'),main)
    def test_decisions_frozen(self): self.assertIn('H1_2017_HISTORICAL_REPLICATION_PASS',S); self.assertIn('H1_2017_HISTORICAL_REPLICATION_FAIL',S)
    def test_unittest_shape(self):
        self.assertTrue(issubclass(Test2017H1HistoricalReplicationExecutionRelease,unittest.TestCase))

if __name__=='__main__': unittest.main()
