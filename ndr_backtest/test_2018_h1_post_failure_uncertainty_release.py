import ast, hashlib, json, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent; SRC=ROOT/'independent_priority_radar.py'; TEXT=SRC.read_text(); TREE=ast.parse(TEXT)
def assignment(name):
    for n in TREE.body:
        if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets): return ast.literal_eval(n.value)
    raise AssertionError(name)
class TestH1PostFailureUncertaintyRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertEqual(assignment('VERSION'),'1.7.60'); self.assertIn('POST-FAILURE-UNCERTAINTY-DIAGNOSTIC-A',assignment('BUILD'))
    def test_provenance_pins(self):
        s=assignment('BACKWARD_OOS_2018_H1_UNCERTAINTY_DIAGNOSTIC_SPEC')
        self.assertEqual(s['required_post_oos_freeze_sha256'],'ed9e501246a445052988f61d3ca8063e2e0d413ec7a6e4a7e61061b6b066d482')
        self.assertEqual(s['required_h1_result_sha256'],'edf7d9402b10ddf24ee1c6e9ce36e2867379de9059bc80422c73d66b88ebd9fa')
    def test_exact_question_scope(self):
        s=assignment('BACKWARD_OOS_2018_H1_UNCERTAINTY_DIAGNOSTIC_SPEC'); q=s['scope']
        self.assertEqual((q['window_minutes'],q['selection']),(30,'top_3')); self.assertEqual(q['expected_sessions'],251)
        self.assertEqual((q['expected_baseline_evaluable'],q['expected_h1_count']),(613,203)); self.assertAlmostEqual(q['frozen_primary_improvement'],0.046159162320494385)
    def test_cluster_method_frozen(self):
        m=assignment('BACKWARD_OOS_2018_H1_UNCERTAINTY_DIAGNOSTIC_SPEC')['method']
        self.assertEqual(m['unit_of_resampling'],'trading_session'); self.assertEqual(m['replicates'],50000); self.assertEqual(m['seed'],17602018); self.assertEqual(m['confidence_level'],0.95)
    def test_formal_fail_immutable(self):
        s=assignment('BACKWARD_OOS_2018_H1_UNCERTAINTY_DIAGNOSTIC_SPEC'); self.assertEqual(s['formal_decision'],'H1_BACKWARD_OOS_FAIL'); self.assertTrue(s['formal_decision_is_immutable'])
    def test_firewall(self):
        f=assignment('BACKWARD_OOS_2018_H1_UNCERTAINTY_DIAGNOSTIC_SPEC')['firewall']; self.assertTrue(all(v is False for v in f.values()))
    def test_stop_before_other_diagnostics(self):
        s=assignment('BACKWARD_OOS_2018_H1_UNCERTAINTY_DIAGNOSTIC_SPEC'); self.assertIn('STOP_REVIEW',s['stop_rule']); self.assertIn('Rank',s['stop_rule'])
    def test_no_alpaca_in_new_block(self):
        b=TEXT[TEXT.index('# v1.7.60 — 2018 H1 Post-Failure Diagnostic'):TEXT.index('if __name__ == "__main__":')]
        self.assertNotIn('_fd_fetch_session_rows(',b); self.assertNotIn('_ctr_fetch_1m(',b); self.assertNotIn('radar.alpaca.',b)
    def test_routes_before_main(self):
        self.assertLess(TEXT.index('/h1-post-failure-diagnostic/protocol'),TEXT.index('if __name__ == "__main__":'))
        self.assertLess(TEXT.index('/h1-post-failure-diagnostic/start'),TEXT.index('if __name__ == "__main__":'))
    def test_unittest_style(self):
        self.assertTrue(issubclass(TestH1PostFailureUncertaintyRelease,unittest.TestCase))
if __name__=='__main__': unittest.main()
