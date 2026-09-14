import ast
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = (ROOT / "independent_priority_radar.py").read_text()
TREE = ast.parse(SRC)

def literal_assignment(name):
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(name)

SPEC = literal_assignment("BACKWARD_OOS_2018_UNIVERSE_SPEC")
SHA = hashlib.sha256(json.dumps(SPEC, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

class Test2018UniverseReconstructionRelease(unittest.TestCase):
    def test_01_version_and_build(self):
        self.assertIn('VERSION = "1.7.53"', SRC)
        self.assertIn("2018-UNIVERSE-RECONSTRUCTION-CERTIFICATION", SRC)

    def test_02_parent_capability_result_is_pinned(self):
        self.assertEqual(
            SPEC["required_capability_probe_result_sha256"],
            "723f3d3ecf9fb91fd629f23bcda6dec768f41aa76e40899c3138841a72df5c36",
        )
        self.assertEqual(
            SPEC["required_capability_probe_spec_sha256"],
            "0b2f2986cafa901151d5c2359e56c5ff43838ffb492d3468a86ba22e6dc4c151",
        )

    def test_03_coverage_gate_is_prefrozen_and_fail_closed(self):
        self.assertEqual(SPEC["coverage_gate_pct_min"], 95.0)
        self.assertTrue(SPEC["fail_closed_without_independent_denominator"])
        self.assertIn("independent point-in-time 2018 security-master denominator", SPEC["coverage_gate_definition"])

    def test_04_h1_and_outcomes_remain_closed(self):
        fw = SPEC["firewall"]
        for key in (
            "h1_computed", "mfe_mae_computed", "target_adverse_computed",
            "ranking_computed", "candidate_selection_computed",
            "backward_oos_opened", "fresh_forward_oos_opened",
            "2019_2026_discovery_mutated",
        ):
            with self.subTest(key=key):
                self.assertFalse(fw[key])

    def test_05_protocol_routes_exist_before_main(self):
        for route in (
            "/research/2018-backward-oos/universe/protocol",
            "/research/2018-backward-oos/universe/start",
            "/research/2018-backward-oos/universe/status",
            "/research/2018-backward-oos/universe/result",
        ):
            with self.subTest(route=route):
                self.assertIn(route, SRC)
        self.assertLess(
            SRC.index("/research/2018-backward-oos/universe/protocol"),
            SRC.index('if __name__ == "__main__":'),
        )

    def test_06_no_silent_true_universe_claim(self):
        self.assertIn("RECONSTRUCTION_COMPLETE_COVERAGE_UNCERTIFIED", SRC)
        self.assertIn('"h1_execution_allowed": False', SRC)
        self.assertIn('"universe_coverage_certified": False', SRC)

    def test_07_spec_sha_is_deterministic(self):
        self.assertEqual(len(SHA), 64)
        self.assertIn("BACKWARD_OOS_2018_UNIVERSE_SPEC_SHA256", SRC)

if __name__ == "__main__":
    unittest.main()
