import unittest
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent
SRC=(ROOT/'independent_priority_radar.py').read_text()

class TestTargetAdverseExecutionRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.64"',SRC)
        self.assertIn('TARGET-ADVERSE-DECOMPOSITION-EXECUTION-A',SRC)
    def test_prefreeze_sha_pinned(self):
        self.assertIn('40991e6616f6fa308b56878a937afd1d5644dec472335bc47e73b3f044420923',SRC)
    def test_estimands_exact(self):
        self.assertIn('P(TARGET_FIRST | H1) - P(TARGET_FIRST | Baseline)',SRC)
        self.assertIn('P(ADVERSE_FIRST | Baseline) - P(ADVERSE_FIRST | H1)',SRC)
    def test_identity_fail_closed(self):
        self.assertIn('identity_tolerance',SRC)
        self.assertIn('frozen primary identity mismatch',SRC)
    def test_cluster_bootstrap(self):
        self.assertIn('"replicates":50000',SRC)
        self.assertIn('"seed":17632018',SRC)
        self.assertIn('"unit_of_resampling":"trading_session"',SRC)
    def test_no_component_competition(self):
        self.assertIn('component_competition_test_computed":False',SRC)
        self.assertIn('within_h1_target_vs_adverse_test_computed":False',SRC)
    def test_expected_counts_fail_closed(self):
        for x in ('expected_records_top3":746','expected_baseline_evaluable":613','expected_h1_count":203','expected_sessions":251'):
            self.assertIn(x,SRC)
    def test_routes_exist(self):
        base='/research/2018-backward-oos/h1-post-failure-diagnostic/target-adverse/'
        for x in ('protocol','start','status','result'):self.assertIn(base+x,SRC)
    def test_routes_before_main(self):
        main=SRC.index('if __name__ == "__main__":')
        self.assertLess(SRC.index('/target-adverse/protocol'),main)
        self.assertLess(SRC.index('/target-adverse/start'),main)
        self.assertLess(SRC.index('/target-adverse/status'),main)
        self.assertLess(SRC.index('/target-adverse/result'),main)
    def test_firewalls(self):
        self.assertIn('"alpaca_requests":False',SRC)
        self.assertIn('"fresh_forward_oos_opened":False',SRC)
        self.assertIn('"post_2018_market_data_read":False',SRC)
        self.assertIn('"successor_hypothesis_created":False',SRC)
    def test_h1_immutable(self):
        self.assertIn('"formal_decision":"H1_BACKWARD_OOS_FAIL"',SRC)
        self.assertIn('"formal_decision_is_immutable":True',SRC)
    def test_stop_review(self):
        self.assertIn('STOP_REVIEW immediately after the pre-frozen decomposition report',SRC)

if __name__ == '__main__':
    unittest.main()
