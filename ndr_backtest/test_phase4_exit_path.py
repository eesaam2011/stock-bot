import unittest
from pathlib import Path

from phase4_exit_path_engine import Phase4ExitPathEngine, load_phase4_protocol


def bar(minute, open_, high, low, close):
    return {"t": f"2026-07-01T14:{minute:02d}:00Z", "o": open_, "h": high, "l": low, "c": close}


class Phase4ExitPathTests(unittest.TestCase):
    def setUp(self):
        self.case = {"case_id": "x", "session": "2026-07-01", "symbol": "AAA"}

    def test_protocol_is_embedded_and_safe(self):
        protocol, fingerprint = load_phase4_protocol(Path("missing-phase4-protocol.json"))
        self.assertEqual(protocol["protocol_id"], "NDR-PHASE4-EXIT-PATH-2026-09-03-A")
        self.assertFalse(protocol["safety"]["alerts_enabled"])
        self.assertEqual(len(fingerprint), 64)

    def test_fixed_target_uses_next_bar_open_and_cost(self):
        bars = [bar(1, 10, 10.1, 9.9, 10), bar(2, 10, 10.3, 9.95, 10.2)]
        result = Phase4ExitPathEngine._simulate_policy(
            self.case, bars, {"id": "TP2_STOP3", "target_pct": 2.0, "hard_stop_pct": 3.0}
        )
        self.assertEqual(result["entry"], 10)
        self.assertEqual(result["status"], "target")
        self.assertAlmostEqual(result["net_return_pct"], 1.75)

    def test_stop_wins_same_bar_ambiguity(self):
        bars = [bar(1, 10, 10.1, 9.9, 10), bar(2, 10, 10.4, 9.6, 10)]
        result = Phase4ExitPathEngine._simulate_policy(
            self.case, bars, {"id": "TP3_STOP3", "target_pct": 3.0, "hard_stop_pct": 3.0}
        )
        self.assertEqual(result["status"], "stop")
        self.assertAlmostEqual(result["net_return_pct"], -3.25)

    def test_breakeven_activates_for_next_bar(self):
        bars = [bar(1, 10, 10.25, 9.9, 10.2), bar(2, 10.2, 10.3, 9.95, 10.0)]
        result = Phase4ExitPathEngine._simulate_policy(
            self.case, bars, {"id": "TP5_BE2_STOP3", "target_pct": 5.0, "hard_stop_pct": 3.0, "breakeven_after_pct": 2.0}
        )
        self.assertEqual(result["status"], "stop")
        self.assertAlmostEqual(result["net_return_pct"], -0.25)


if __name__ == "__main__":
    unittest.main()
