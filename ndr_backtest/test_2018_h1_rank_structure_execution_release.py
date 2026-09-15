import ast, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent
SRC=(ROOT/'independent_priority_radar.py').read_text()
class TestRankStructureExecutionRelease(unittest.TestCase):
    def test_version(self): self.assertIn('VERSION = "1.7.62"',SRC)
    def test_routes_before_main(self):
        main=SRC.index('if __name__ == "__main__":')
        for route in ['/rank-structure/protocol','/rank-structure/start','/rank-structure/status','/rank-structure/result']:
            self.assertLess(SRC.index(route),main)
    def test_prefreeze_sha_exact(self): self.assertIn('762bff8c5ee26cebc3e0029178099724dfe0bf38c371e3d6bc72e535aba17cae',SRC)
    def test_prior_result_sha_exact(self): self.assertIn('7853bd2cbf66a907d72118923646715925322f1dbc8062f44538b102c3be1d4e',SRC)
    def test_primary_contrast_exact(self): self.assertIn('"left":"rank_1","right":"pooled_rank_2_3"',SRC)
    def test_cluster_bootstrap_exact(self):
        self.assertIn('"unit_of_resampling":"trading_session"',SRC);self.assertIn('"replicates":50000',SRC);self.assertIn('"seed":17612018',SRC);self.assertIn('"confidence_level":0.95',SRC)
    def test_decision_rule_exact(self): self.assertIn('point estimate is positive AND 95% session-cluster bootstrap interval excludes zero',SRC)
    def test_source_count_gates(self):
        for x in ['"expected_records_top3":746','"expected_baseline_evaluable":613','"expected_h1_count":203','"expected_sessions":251']: self.assertIn(x,SRC)
    def test_no_alpaca_in_worker(self):
        node=ast.parse(SRC);fn=next(n for n in node.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='_boos18h1r_worker');text=ast.get_source_segment(SRC,fn)
        self.assertNotIn('alpaca.',text);self.assertNotIn('requests.',text)
    def test_stop_review_firewall(self):
        for x in ['"fresh_forward_oos_opened":False','"top1_promoted":False','"alternative_rank_contrasts":False','"successor_hypothesis_created":False']: self.assertIn(x,SRC)
    def test_h1_immutable(self): self.assertIn('"formal_decision":"H1_BACKWARD_OOS_FAIL"',SRC);self.assertIn('"formal_decision_is_immutable":True',SRC)
    def test_old_prefreeze_preserved(self): self.assertIn('BACKWARD_OOS_2018_H1_RANK_STRUCTURE_PREFREEZE_SPEC = {',SRC)
if __name__=='__main__': unittest.main()
