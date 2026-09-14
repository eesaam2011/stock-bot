import ast, hashlib, pathlib, re, unittest
P=pathlib.Path(__file__).with_name('independent_priority_radar.py'); S=P.read_text(); T=ast.parse(S)
class ReleaseTests(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.56-R1"',S); self.assertIn('2018-CENSUS-PHASE0B-CONTROLS-B',S)
    def test_frozen_historical_hash_pins_present(self):
        
        spans=[('HISTORICAL_CENSUS_SPEC = {','HISTORICAL_CENSUS_SHA256 =','cb7dca1ea4736b713f9a0d7fc51ebf1d86b2f92deef47c8b970813c4dd243775'),('PHASE0B_FULL_SPEC = {','PHASE0B_FULL_SHA256 =','5285429df58a66fee01ecc66a4841c7b2fffbc63e99145b0f3de1bdb2d76971e'),('FEATURE_DISCOVERY_EXEC_SPEC = {','FEATURE_DISCOVERY_EXEC_SHA256 =','fcc5959050c7e598e978925fd469e90825812ca1f0ab537bf3d985657c654715')]
        for a,z,h in spans:self.assertEqual(hashlib.sha256(S[S.index(a):S.index(z)].encode()).hexdigest(),h)
    def test_parity_hash_pinned(self): self.assertIn('d0fd091937acf600b05c11158e76ef75e428e53d920d540b00b833d7f7ce3906',S)
    def test_census_exact_mechanics(self):
        self.assertIn('"coarse_timeframe":HISTORICAL_CENSUS_SPEC["coarse_timeframe"]',S); self.assertIn('"threshold_pct":HISTORICAL_CENSUS_SPEC["primary_threshold_pct"]',S)
    def test_sample_gate_frozen(self): self.assertIn('"sample_gate":{"min_positive_events":537,"min_positive_symbols":100',S)
    def test_h1_firewall(self):
        self.assertIn('"h1_computed":False,"h1_execution_allowed":False',S); self.assertNotIn('/research/2018-backward-oos/execution/h1',S)
    def test_routes_before_main(self):
        main=S.index('if __name__ == "__main__":');
        for p in ['/research/2018-backward-oos/execution/protocol','/research/2018-backward-oos/execution/start','/research/2018-backward-oos/execution/status','/research/2018-backward-oos/execution/result','/research/2018-backward-oos/execution/pause']: self.assertLess(S.index(p),main)
    def test_exact_control_constants(self):
        self.assertIn('j*7919',S); self.assertIn('base+104729',S); self.assertIn('Phase 0B failed clean candidates from the 2018 Backward-OOS cohort only',S); self.assertIn('historical_control_methodology_source',S)
    def test_2018_only_metadata_uses_original_functions(self):
        self.assertIn('radar._instrument_name_classification(amap.get(sym))',S); self.assertIn('radar._resolve_ambiguous_asset_name',S)
    def test_pause_and_resume_keys(self):
        for k in ['census_completed_sessions','phase0b_completed_sessions','alpaca_logical_requests']: self.assertIn(k,S)
if __name__=='__main__': unittest.main()
