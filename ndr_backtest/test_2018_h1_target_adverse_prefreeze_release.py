import unittest
from pathlib import Path
P=Path(__file__).with_name('independent_priority_radar.py')
S=P.read_text()
class TestTargetAdversePrefreezeRelease(unittest.TestCase):
    def test_version(self): self.assertIn('VERSION = "1.7.63"',S)
    def test_build(self): self.assertIn('2018-H1-TARGET-ADVERSE-DECOMPOSITION-PREFREEZE-A',S)
    def test_route(self): self.assertIn('/target-adverse-prefreeze/protocol',S)
    def test_protocol_only(self):
        self.assertIn('No Start endpoint exists in v1.7.63 by design.',S)
        self.assertNotIn('/target-adverse-prefreeze/start',S)
    def test_exact_estimands(self):
        self.assertIn('P(TARGET_FIRST | H1) - P(TARGET_FIRST | Baseline)',S)
        self.assertIn('P(ADVERSE_FIRST | Baseline) - P(ADVERSE_FIRST | H1)',S)
    def test_identity(self): self.assertIn('delta_target + delta_adverse_reduction = frozen primary improvement',S)
    def test_bootstrap(self):
        self.assertIn('"replicates":50000',S); self.assertIn('"unit_of_resampling":"trading_session"',S)
    def test_no_competition(self): self.assertIn('No delta_target-minus-delta_adverse_reduction contrast',S)
    def test_provenance(self): self.assertIn('2b5f80bffb42d7cc0061a5d2627672e659874d519a2b87d622375c70d727ce45',S)
    def test_firewalls(self):
        self.assertIn('"fresh_forward_oos_opened":False',S); self.assertIn('"alpaca_requests":False',S)
    def test_route_before_main(self): self.assertLess(S.index('/target-adverse-prefreeze/protocol'),S.index('if __name__ == "__main__":'))
    def test_unittest_shape(self): self.assertTrue(issubclass(TestTargetAdversePrefreezeRelease,unittest.TestCase))
if __name__=='__main__': unittest.main()
