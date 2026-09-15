import ast, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parent; SRC=ROOT/'independent_priority_radar.py'; TEXT=SRC.read_text(encoding='utf-8')
class TestObservedUniverseCertificationRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.70-R1"',TEXT);self.assertIn('2017-OBSERVED-UNIVERSE-CERTIFICATION-A-R1',TEXT)
    def test_routes_before_main(self):
        main=TEXT.index('if __name__ == "__main__":')
        for route in ['/research/2017-historical-replication/observed-universe/protocol','/research/2017-historical-replication/observed-universe/start','/research/2017-historical-replication/observed-universe/status','/research/2017-historical-replication/observed-universe/result']:self.assertLess(TEXT.index(route),main)
    def test_exact_provenance(self):
        for sha in ['628601d5f71cc38e309530604e608ba145a7be494fcb72da8d6e7c5172cab141','7b6069a33cfa9ccca53c358932e51ea0a644c20731501a5de9a1f950e3af94a3','1f6d11ec1b3de059a4b9cae2891f135a811c6f68478f0a09438e57fceb4b798a','c2a8bd08d7023b093de7a98fd6713131b909f0512f3fa769ac84f203f2e6a6f5']:self.assertIn(sha,TEXT)
    def test_frozen_counts(self):
        for s in ['"source_union_symbols":31973','"symbols_observed_in_sip":6393','"symbols_with_dec2016_warmup_presence":5854']:self.assertIn(s,TEXT)
    def test_no_stricter_standard(self):
        self.assertIn('"independent_security_master_required":False',TEXT);self.assertIn('"coverage_pct_threshold":None',TEXT);self.assertIn('no stricter independent Security Master standard is imposed on 2017',TEXT)
    def test_diagnostics_not_gate(self):
        self.assertIn('"diagnostics_are_coverage_proof":False',TEXT);self.assertIn('"diagnostics_are_pass_fail_gates":False',TEXT)
    def test_corporate_actions_not_overclaimed(self):
        self.assertIn('"screen_is_complete_corporate_action_certificate":False',TEXT);self.assertIn('"phase0b_strict_exclusion_still_required":True',TEXT);self.assertIn('"all_2017_corporate_actions_certified":False',TEXT)
    def test_h1_firewall(self):
        block=TEXT[TEXT.index('BACKWARD_OOS_2017_OBSERVED_CERT_SPEC ='):TEXT.index('BACKWARD_OOS_2017_OBSERVED_CERT_SPEC_SHA256')]
        for s in ['"h1_computed":False','"target_adverse_computed":False','"ranking_computed":False','"candidate_selection_computed":False','"fresh_forward_oos_opened":False','"pooled_2017_2018_computed":False']:self.assertIn(s,block)
    def test_stop_rule(self):
        self.assertIn('"maximum_additional_h1_historical_years":1',TEXT);self.assertIn('"no_automatic_2016_or_earlier_after_2017_result":True',TEXT);self.assertIn('"pooled_2017_2018_replication_test_authorized":False',TEXT)
    def test_zero_market_data(self):
        self.assertIn('zero market-data requests',TEXT);self.assertIn('"alpaca_requests":False',TEXT)
    def test_stop_review(self):
        self.assertIn('Only a separately frozen 2017 sampling/methodology-parity pre-freeze may be considered; H1 remains locked.',TEXT)
    def test_v1769_writer_v1770_reader_warmup_key_symmetry(self):
        self.assertIn('"warmup_dec_2016_present":any(m=="2016-12" for m in months)',TEXT)
        self.assertIn('v.get("warmup_dec_2016_present")',TEXT)
        self.assertNotIn('v.get("dec2016_warmup_presence")',TEXT)
    def test_unittest_style(self):
        tree=ast.parse(Path(__file__).read_text());classes=[n for n in tree.body if isinstance(n,ast.ClassDef)];self.assertTrue(any(any(isinstance(b,ast.Attribute) and b.attr=='TestCase' for b in c.bases) for c in classes))
if __name__=='__main__':unittest.main()
