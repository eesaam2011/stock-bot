import unittest, pathlib, re, json, hashlib
ROOT=pathlib.Path(__file__).resolve().parent
CODE=(ROOT/'independent_priority_radar.py').read_text()
class TestEvidenceConsolidationFreezeRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.67"',CODE); self.assertIn('H1-EVIDENCE-CONSOLIDATION-FREEZE-A',CODE)
    def test_route_before_main(self):
        self.assertLess(CODE.index('h1-post-diagnostic-evidence-freeze/protocol'),CODE.index('if __name__ == "__main__":'))
    def test_protocol_only(self):
        self.assertIn('No Start endpoint exists in v1.7.67',CODE); self.assertNotIn('h1-post-diagnostic-evidence-freeze/start',CODE)
    def test_formal_fail_immutable(self): self.assertIn('"formal_decision":"H1_BACKWARD_OOS_FAIL"',CODE)
    def test_no_meta_analysis(self):
        self.assertIn('"meta_analysis":False',CODE); self.assertIn('"combined_p_value":False',CODE); self.assertIn('"combined_confidence_interval":False',CODE)
    def test_no_vote_score(self): self.assertIn('"vote_or_score":False',CODE)
    def test_no_next_path(self): self.assertIn('"next_research_path_selected":False',CODE); self.assertIn('"next_research_path":"UNDECIDED',CODE)
    def test_required_hashes(self):
        for h in ['edf7d9402b10ddf24ee1c6e9ce36e2867379de9059bc80422c73d66b88ebd9fa','7853bd2cbf66a907d72118923646715925322f1dbc8062f44538b102c3be1d4e','2b5f80bffb42d7cc0061a5d2627672e659874d519a2b87d622375c70d727ce45','5d414991d494204f3c17a4b2b52d8c1dc52002d907bbea0e730c73f9b68b5b4f','7cec8eb9225d7d219ccf4613e4edcdc366e6c85656da3ce8175dafabc75c50e0']:
            self.assertIn(h,CODE)
    def test_four_diagnostic_classifications(self):
        self.assertIn('NO_ESTABLISHED_RANK_SEPARATION',CODE); self.assertIn('NEITHER_COMPONENT_ESTABLISHED',CODE); self.assertIn('TEMPORAL_BREADTH_UNRESOLVED',CODE)
    def test_temporal_numbers_preserved(self): self.assertIn('1.2611464968152881,7.547169811320756,8.076422058184974,1.7735849056603803',CODE)
    def test_target_adverse_numbers_preserved(self): self.assertIn('1.808114819309059',CODE); self.assertIn('2.8078014127403765',CODE)
    def test_firewall(self): self.assertIn('"fresh_forward_oos_opened":False',CODE); self.assertIn('"successor_hypothesis_created":False',CODE); self.assertIn('"diagnostics_recomputed":False',CODE)
if __name__ == '__main__': unittest.main()
