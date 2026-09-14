import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "independent_priority_radar.py"
TEXT = SRC.read_text(encoding="utf-8")
TREE = ast.parse(TEXT)


class Test2018ProbeRelease(unittest.TestCase):

    def test_release_version_and_build(self):
        self.assertIn('VERSION = "1.7.52"', TEXT)
        self.assertIn('2018-BACKWARD-OOS-CAPABILITY-PROBE-A', TEXT)

    def test_probe_routes_registered_before_main(self):
        main_pos = TEXT.index('if __name__ == "__main__":')
        for route in [
            '/research/2018-backward-oos/probe/protocol',
            '/research/2018-backward-oos/probe/start',
            '/research/2018-backward-oos/probe/status',
            '/research/2018-backward-oos/probe/result',
        ]:
            with self.subTest(route=route):
                self.assertIn(route, TEXT[:main_pos])

    def test_probe_firewall_contains_no_h1_formula_or_outcomes(self):
        start = TEXT.index('# v1.7.52 — 2018 Backward-OOS Capability Probe')
        block = TEXT[start:TEXT.index('if __name__ == "__main__":')]
        for forbidden in [
            'close_location_in_post_t0_range',
            'outcome_metrics(',
            'TARGET_FIRST',
            'ADVERSE_FIRST',
            'mfe_pct',
            'mae_pct',
        ]:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, block)

    def test_warmup_is_2017_and_target_is_2018(self):
        self.assertIn('"start": "2017-12-01"', TEXT)
        self.assertIn('"end": "2017-12-31"', TEXT)
        self.assertIn('"start": "2018-01-01"', TEXT)
        self.assertIn('"end": "2018-12-31"', TEXT)

    def test_frozen_thresholds_are_explicit(self):
        for token in [
            '"probe_sessions_present_pct_min": 98.0',
            '"sentinel_symbol_session_any_bar_pct_min": 95.0',
            '"warmup_symbols_with_any_bar_pct_min": 95.0',
            '"lookahead_errors_allowed": 0',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, TEXT)

    def test_capability_pass_does_not_certify_universe_or_h1(self):
        for token in [
            '"universe_coverage_certified":False',
            '"backward_oos_opened":False',
            '"h1_computed":False',
            '"fresh_forward_oos_opened":False',
        ]:
            with self.subTest(token=token):
                self.assertIn(token, TEXT)


if __name__ == "__main__":
    unittest.main()
