import ast
import re
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
SRC=ROOT/"independent_priority_radar.py"
TEXT=SRC.read_text(encoding="utf-8")

class TestObservedUniverseRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertIn('VERSION = "1.7.54"',TEXT)
        self.assertIn('2018-OBSERVED-UNIVERSE-CERTIFICATION-A',TEXT)
    def test_routes_before_main(self):
        main=TEXT.index('if __name__ == "__main__":')
        for route in [
            '/research/2018-backward-oos/observed-universe/protocol',
            '/research/2018-backward-oos/observed-universe/start',
            '/research/2018-backward-oos/observed-universe/status',
            '/research/2018-backward-oos/observed-universe/result']:
            self.assertLess(TEXT.index(route),main)
    def test_exact_provenance_frozen(self):
        self.assertIn('4eb227f00e6518fa7810db9fe4391c13d5701971c87710c48b0a46f5d1ee81ab',TEXT)
        self.assertIn('c9276080a32128f941195876745ec767ab7c72925428285f4b9825fd997eae7b',TEXT)
        self.assertIn('c2a8bd08d7023b093de7a98fd6713131b909f0512f3fa769ac84f203f2e6a6f5',TEXT)
    def test_retroactive_terminology(self):
        self.assertIn('"2018": "Observed Historical SIP Universe"',TEXT)
        self.assertIn('"2019_2026": "Observed Historical SIP Universe"',TEXT)
        self.assertIn('complete/certified US-market point-in-time Security Master universe',TEXT)
    def test_diagnostics_not_gate(self):
        self.assertIn('"diagnostics_are_coverage_proof": False',TEXT)
        self.assertIn('"diagnostics_are_pass_fail_gates": False',TEXT)
        self.assertIn('2019_2026_annual_mean',TEXT)
    def test_split_warmup_finding_and_strict_exclusion(self):
        self.assertIn('"finding": "NO_LONG_HISTORY_DEPENDENCY"',TEXT)
        self.assertIn('"dec2017_warmup_required_for_this_screen": False',TEXT)
        self.assertIn('"phase0b_strict_exclusion_still_required": True',TEXT)
    def test_h1_firewall(self):
        block=TEXT[TEXT.index('BACKWARD_OOS_2018_OBSERVED_CERT_SPEC ='):TEXT.index('BACKWARD_OOS_2018_OBSERVED_CERT_SPEC_SHA256')]
        for phrase in ['"h1_computed": False','"mfe_mae_computed": False','"target_adverse_computed": False','"candidate_selection_computed": False','"backward_oos_opened": False','"fresh_forward_oos_opened": False']:
            self.assertIn(phrase,block)
        self.assertIn('"alpaca_requests": False',block)
    def test_unittest_style(self):
        tree=ast.parse(Path(__file__).read_text())
        classes=[n for n in tree.body if isinstance(n,ast.ClassDef)]
        self.assertTrue(classes)
        self.assertTrue(any(any(isinstance(b,ast.Attribute) and b.attr=='TestCase' for b in c.bases) for c in classes))

if __name__ == "__main__":
    unittest.main()
