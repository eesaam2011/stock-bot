import pathlib, unittest
P=pathlib.Path(__file__).with_name('independent_priority_radar.py')
S=P.read_text(); MARK='# v1.7.72 — 2017 Candidate Census'; T=S[S.index(MARK):]
class Test2017CensusPhase0BControlsRelease(unittest.TestCase):
    def test_version_build(self): self.assertIn('VERSION = "1.7.72"',S); self.assertIn('2017-CENSUS-PHASE0B-CONTROLS-A',S)
    def test_prefreeze_sha_frozen(self): self.assertIn('"required_sampling_parity_sha256":"5175e89a25256c65e813a8df3855831bf08f04ec2b90cbddad3b497da451cca2"',T)
    def test_cert_sha_frozen(self): self.assertIn('"required_observed_universe_result_sha256":"56d3663d7c8ff9a5ef8ced98e1206709ea9a26163628c9cf2eaf45d90e930afb"',T)
    def test_period_and_universe(self): self.assertIn('"period":["2017-01-01","2017-12-31"]',T); self.assertIn('observed_in_2017=true only',T)
    def test_census_parity(self): self.assertIn('"coarse_timeframe":HISTORICAL_CENSUS_SPEC["coarse_timeframe"]',T); self.assertIn('"threshold_pct":HISTORICAL_CENSUS_SPEC["primary_threshold_pct"]',T)
    def test_sample_gate_unchanged(self): self.assertIn('"sample_gate":{"min_positive_events":537,"min_positive_symbols":100,"decision_if_below":"2017_INSUFFICIENT_SAMPLE"}',T)
    def test_controls_2017_only(self): self.assertIn('2017 Historical Replication cohort only',T); self.assertIn('Historical Replication 2017',T)
    def test_h1_firewall(self): self.assertIn('"h1_computed":False,"h1_execution_allowed":False',T); self.assertIn('"pooled_2017_2018_computed":False',T)
    def test_routes_exist(self):
        for r in ['/research/2017-historical-replication/execution/protocol','/research/2017-historical-replication/execution/start','/research/2017-historical-replication/execution/status','/research/2017-historical-replication/execution/result','/research/2017-historical-replication/execution/pause']: self.assertIn(r,T)
    def test_routes_before_main(self): self.assertLess(S.index('/research/2017-historical-replication/execution/protocol'),S.rfind('if __name__ == "__main__":'))
    def test_observed_2017_reader(self): self.assertIn('get("observed_in_2017")',T); self.assertNotIn('get("observed_in_2018")',T)
    def test_2017_calendar_bounds(self): self.assertIn('date(2017,1,1)<=',T); self.assertIn('<=date(2017,12,31)',T)
    def test_no_h1_measurement(self): self.assertNotIn('close_location_in_post_t0_range',T); self.assertNotIn('TARGET_FIRST',T); self.assertNotIn('ADVERSE_FIRST',T)
if __name__=='__main__': unittest.main()
