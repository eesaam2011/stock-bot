import unittest, pathlib
ROOT=pathlib.Path(__file__).resolve().parent
CODE=(ROOT/'independent_priority_radar.py').read_text()
class Test2017IndependenceCapabilityProbeRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.68"',CODE); self.assertIn('2017-INDEPENDENCE-CAPABILITY-PROBE-A',CODE)
    def test_routes_before_main(self):
        self.assertLess(CODE.index('/research/2017-historical-replication/independence-capability/protocol'),CODE.index('if __name__ == "__main__":'))
        self.assertLess(CODE.index('/research/2017-historical-replication/independence-capability/start'),CODE.index('if __name__ == "__main__":'))
    def test_exact_v1752_thresholds(self):
        for x in ['"probe_sessions_present_pct_min":98.0','"sentinel_symbol_session_any_bar_pct_min":95.0','"warmup_symbols_with_any_bar_pct_min":95.0','"calendar_sessions_min":245','"calendar_sessions_max":255']:
            self.assertIn(x,CODE)
        self.assertIn('Exact transferable v1.7.52 capability thresholds',CODE)
    def test_target_and_warmup(self):
        self.assertIn('"target_period":{"start":"2017-01-01","end":"2017-12-31"}',CODE)
        self.assertIn('"warmup_period":{"start":"2016-12-01","end":"2016-12-31"',CODE)
    def test_observed_sip_not_complete_market(self):
        self.assertIn('Observed Historical SIP Universe',CODE); self.assertIn('complete-market coverage is NOT certified',CODE)
    def test_one_year_stop_rule(self):
        self.assertIn('"maximum_additional_h1_historical_years":1',CODE); self.assertIn('"only_authorized_candidate_year":2017',CODE)
        self.assertIn('no_automatic_2016_or_earlier_after_2017_result',CODE)
    def test_no_pooling_or_regime_claim(self):
        self.assertIn('"pooled_2017_2018_replication_test_authorized":False',CODE); self.assertIn('"regime_dependence_claim_authorized":False',CODE)
    def test_independence_gate_before_start(self):
        self.assertIn('INDEPENDENCE_PROVENANCE_PASS',CODE); self.assertIn('independence_gate_blocked',CODE)
    def test_provenance_periods(self):
        self.assertIn('2019-01-01',CODE); self.assertIn('2026-08-31',CODE); self.assertIn('feature warm-up only; never candidate/outcome evaluation',CODE)
    def test_evidence_freeze_hash(self):
        self.assertIn('2959cf3f39985eed67539e4286d3af39f9ceacc4a787af828b364d8d91e5579b',CODE)
    def test_h1_firewall(self):
        self.assertIn('"h1_computed":False',CODE); self.assertIn('"target_adverse_computed":False',CODE); self.assertIn('"ranking_computed":False',CODE); self.assertIn('"candidate_selection_computed":False',CODE)
    def test_fresh_oos_closed(self):
        self.assertIn('"fresh_forward_oos_opened":False',CODE); self.assertIn('"pooled_2017_2018_computed":False',CODE)
if __name__ == '__main__': unittest.main()
