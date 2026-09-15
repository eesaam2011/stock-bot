import ast
import re
import unittest
from pathlib import Path
SRC=Path(__file__).resolve().parent/'independent_priority_radar.py'
TEXT=SRC.read_text();TREE=ast.parse(TEXT)
class TestH1BackwardOOSExecutionRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.58"',TEXT);self.assertIn('2018-H1-BACKWARD-OOS-EXECUTION-A',TEXT)
    def test_prefreeze_sha_exact(self):self.assertIn('"required_prefreeze_sha256":"ff0eae7b6d091313f3ccbf44047af42c8dcbf644ec891229a3eb41b7b711fe27"',TEXT)
    def test_source_result_sha_exact(self):self.assertIn('"required_2018_sample_controls_result_sha256":"fa0b4341e8df45528986535cc3670233dc49b968a32b1995f85a789b6a0dd7c2"',TEXT)
    def test_h1_rule_exact(self):
        self.assertIn('"feature":"close_location_in_post_t0_range"',TEXT);self.assertIn('"threshold":1.0/3.0',TEXT);self.assertIn('"confirmation_offset_minutes":1',TEXT)
    def test_primary_gates_exact(self):
        self.assertIn('"horizon_minutes":30,"target_pct":5.0,"adverse_pct":-5.0',TEXT);self.assertIn('"primary_improvement_min_absolute":0.05',TEXT);self.assertIn('"retention_min":0.25',TEXT)
    def test_four_views_exact(self):
        for s in ('{"window_minutes":30,"selection":"top_1"}','{"window_minutes":30,"selection":"top_3"}','{"window_minutes":60,"selection":"top_1"}','{"window_minutes":60,"selection":"top_3"}'):self.assertIn(s,TEXT)
    def test_same_bar_ambiguous_logic_present(self):
        self.assertIn('order="SAME_BAR_AMBIGUOUS"',TEXT);self.assertIn('th==ah',TEXT)
    def test_all_four_required(self):self.assertIn('overall=overall and passed',TEXT);self.assertIn('H1_BACKWARD_OOS_PASS" if overall else "H1_BACKWARD_OOS_FAIL',TEXT)
    def test_fresh_oos_locked(self):self.assertIn('"fresh_forward_oos_opened":False',TEXT);self.assertIn('"2019_2026_results_mutated":False',TEXT)
    def test_routes_before_main(self):
        main=TEXT.index('if __name__ == "__main__":')
        for r in ('/research/2018-backward-oos/h1-execution/protocol','/research/2018-backward-oos/h1-execution/start','/research/2018-backward-oos/h1-execution/status','/research/2018-backward-oos/h1-execution/result'):self.assertLess(TEXT.index(r),main)
if __name__=='__main__':unittest.main()
