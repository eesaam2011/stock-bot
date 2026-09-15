import ast, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent; SRC=ROOT/'independent_priority_radar.py'; TEXT=SRC.read_text(encoding='utf-8')
class TestSamplingParityPrefreezeRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.71"',TEXT); self.assertIn('2017-SAMPLING-METHODOLOGY-PARITY-PREFREEZE-A',TEXT)
    def test_protocol_route_before_main(self):
        main=TEXT.index('if __name__ == "__main__":'); self.assertLess(TEXT.index('/research/2017-historical-replication/sampling-parity/protocol'),main)
    def test_no_start_route(self):
        self.assertNotIn('/research/2017-historical-replication/sampling-parity/start',TEXT)
    def test_exact_cert_provenance(self):
        self.assertIn('56d3663d7c8ff9a5ef8ced98e1206709ea9a26163628c9cf2eaf45d90e930afb',TEXT); self.assertIn('25466729ee40cad2708ea406ddc57ed3e60f11f63ebc75a84e71d80df1df04d7',TEXT)
    def test_2018_r2_reference(self):
        self.assertIn('d0fd091937acf600b05c11158e76ef75e428e53d920d540b00b833d7f7ce3906',TEXT)
    def test_exact_coarse_census(self):
        block=TEXT[TEXT.index('BACKWARD_OOS_2017_SAMPLING_PARITY_SPEC ='):TEXT.index('BACKWARD_OOS_2017_SAMPLING_PARITY_SHA256')]
        self.assertIn('"coarse_timeframe":HISTORICAL_CENSUS_SPEC["coarse_timeframe"]',block); self.assertIn('uses 1Hour coarse bars',block)
    def test_sample_gate_unchanged(self):
        for x in ['"min_positive_events":537','"min_positive_symbols":100','2017_INSUFFICIENT_SAMPLE','"no_post_result_lowering":True']: self.assertIn(x,TEXT)
    def test_controls_exact_parity(self):
        for x in ['FEATURE_DISCOVERY_EXEC_SPEC["controls"]["hard_negative_matching"]','FEATURE_DISCOVERY_EXEC_SPEC["controls"]["random_control"]','2017 Historical Replication cohort only','"predictive_feature_matching_forbidden":True']: self.assertIn(x,TEXT)
    def test_corporate_action_parity(self):
        for x in ['adjacent coarse closes; flag max(close/prev, prev/close) >= 3.5','PHASE0B_FULL_SPEC["corporate_action_policy"]','still_ambiguous, never verified']: self.assertIn(x,TEXT)
    def test_replication_stop_rule(self):
        for x in ['"only_authorized_candidate_year":2017','"maximum_additional_h1_historical_years":1','"pooled_2017_2018_replication_test_authorized":False','"regime_dependence_claim_authorized":False']: self.assertIn(x,TEXT)
    def test_firewall(self):
        block=TEXT[TEXT.index('BACKWARD_OOS_2017_SAMPLING_PARITY_SPEC ='):TEXT.index('BACKWARD_OOS_2017_SAMPLING_PARITY_SHA256')]
        for x in ['"alpaca_requests":False','"2017_candidate_census_runs":False','"2017_phase0b_runs":False','"controls_constructed":False','"h1_computed":False','"h1_execution_allowed":False','"fresh_forward_oos_opened":False','"pooled_2017_2018_computed":False']: self.assertIn(x,block)
    def test_unittest_style(self):
        tree=ast.parse(Path(__file__).read_text()); classes=[n for n in tree.body if isinstance(n,ast.ClassDef)]; self.assertTrue(any(any(isinstance(b,ast.Attribute) and b.attr=='TestCase' for b in c.bases) for c in classes))
if __name__=='__main__': unittest.main()
