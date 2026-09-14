import ast, pathlib, unittest
ROOT=pathlib.Path(__file__).resolve().parent; SRC=ROOT/'independent_priority_radar.py'; TEXT=SRC.read_text(); TREE=ast.parse(TEXT)
class TestSamplingParityPrefreezeRelease(unittest.TestCase):
    def test_version(self): self.assertIn('VERSION = "1.7.55"',TEXT)
    def test_protocol_route_exists(self): self.assertIn('/research/2018-backward-oos/sampling-parity/protocol',TEXT)
    def test_no_start_route_for_stage(self): self.assertNotIn('/research/2018-backward-oos/sampling-parity/start',TEXT)
    def test_h1_firewall(self): self.assertIn('"h1_execution_allowed":False',TEXT); self.assertIn('"h1_computed":False',TEXT)
    def test_exact_candidate_semantics_are_inherited(self):
        self.assertIn('HISTORICAL_CENSUS_SPEC["coarse_timeframe"]',TEXT); self.assertIn('HISTORICAL_CENSUS_SPEC["candidate_rule"]',TEXT); self.assertIn('HISTORICAL_CENSUS_SPEC["primary_threshold_pct"]',TEXT)
    def test_controls_are_inherited(self): self.assertIn('FEATURE_DISCOVERY_EXEC_SPEC["controls"]["hard_negative_matching"]',TEXT); self.assertIn('FEATURE_DISCOVERY_EXEC_SPEC["controls"]["random_control"]',TEXT)
    def test_support_gate_is_frozen(self): self.assertIn('"min_positive_events":537',TEXT); self.assertIn('"min_positive_symbols":100',TEXT); self.assertIn('2018_INSUFFICIENT_SAMPLE',TEXT)
    def test_route_before_main(self): self.assertLess(TEXT.index('/research/2018-backward-oos/sampling-parity/protocol'),TEXT.index('if __name__ == "__main__":'))
if __name__=='__main__': unittest.main()
