import ast, hashlib, json, re, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent
SRC=(ROOT/"independent_priority_radar.py").read_text()
class TestRankStructurePrefreezeRelease(unittest.TestCase):
    def test_version(self): self.assertIn('VERSION = "1.7.61"',SRC)
    def test_route_before_main(self):
        route='/research/2018-backward-oos/h1-post-failure-diagnostic/rank-structure-prefreeze/protocol'
        self.assertLess(SRC.index(route),SRC.index('if __name__ == "__main__":'))
    def test_protocol_only_no_start(self): self.assertNotIn('rank-structure-prefreeze/start',SRC)
    def test_required_result_sha(self): self.assertIn('7853bd2cbf66a907d72118923646715925322f1dbc8062f44538b102c3be1d4e',SRC)
    def test_cluster_bootstrap_frozen(self):
        self.assertIn('unit_of_resampling":"trading_session"',SRC);self.assertIn('"replicates":50000',SRC);self.assertIn('"confidence_level":0.95',SRC)
    def test_primary_contrast_frozen(self): self.assertIn('"left":"rank_1","right":"pooled_rank_2_3"',SRC)
    def test_zero_rule_frozen(self): self.assertIn('interval excludes zero',SRC);self.assertIn('interval includes zero',SRC)
    def test_h1_immutable(self): self.assertIn('"formal_decision":"H1_BACKWARD_OOS_FAIL"',SRC);self.assertIn('"formal_decision_is_immutable":True',SRC)
    def test_firewalls(self):
        for x in ['"alpaca_requests":False','"fresh_forward_oos_opened":False','"rank_outcomes_computed":False','"successor_hypothesis_created":False','"top1_promoted":False']:
            self.assertIn(x,SRC)
    def test_old_specs_preserved(self):
        self.assertIn('BACKWARD_OOS_2018_H1_POST_OOS_FREEZE_SPEC = {',SRC);self.assertIn('BACKWARD_OOS_2018_H1_UNCERTAINTY_DIAGNOSTIC_SPEC = {',SRC)
if __name__ == "__main__": unittest.main()
