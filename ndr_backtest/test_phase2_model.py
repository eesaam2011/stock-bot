import unittest
import json

import numpy as np

from phase2_model_engine import Phase2ModelEngine, load_phase2_protocol


class JoinedRedis:
    def __init__(self):
        field = "2026-06-10|AAA|2026-06-10T14:00:00Z"
        self.hashes = {
            "next_day_radar_backtest_v3:pcprofit:v2:cases": {
                field: {"session": "2026-06-10", "partition": "development", "symbol": "AAA",
                        "t": "2026-06-10T14:00:00Z", "price_change_pct_last45m": 8.0,
                        "return_pct": 2.0, "recomputed_mfe_pct": 12.0, "has_plan": True},
            },
            "next_day_radar_backtest_v3:pcprofit_er45:v1:cases": {
                field: {"session": "2026-06-10", "partition": "development", "symbol": "AAA",
                        "t": "2026-06-10T14:00:00Z", "er45": 0.5,
                        "return_pct": 2.0, "has_plan": True},
            },
        }

    def get_json(self, key, default=None):
        return default

    def set_json(self, key, value):
        pass

    def command(self, *parts):
        if parts[0] == "HSCAN":
            pairs = []
            for field, value in self.hashes.get(parts[1], {}).items():
                pairs.extend((field, json.dumps(value)))
            return ["0", pairs]
        if parts[0] == "HLEN":
            return len(self.hashes.get(parts[1], {}))
        raise AssertionError(parts)


class JoinedCollector:
    prefix = "next_day_radar_backtest_v3"

    def __init__(self):
        self.redis = JoinedRedis()

    def key(self, suffix):
        return f"{self.prefix}:{suffix}"

    def iter_results(self):
        yield {
            "mode": "approx", "session": "2026-06-10", "symbol": "AAA",
            "breakout_ready": {"phase": "REGULAR", "ts": "2026-06-10T14:00:00Z",
                               "price": 2.0, "opportunity": 90.0, "failure_pressure": 20.0},
        }


class Phase2ModelTests(unittest.TestCase):
    def test_dataset_joins_existing_cases_without_fetching_market_data(self):
        rows = Phase2ModelEngine(JoinedCollector()).build_dataset()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["explosion_ge10"])
        self.assertEqual(rows[0]["net_return_pct"], 1.75)
        self.assertEqual(rows[0]["features"]["price_change_x_er45"], 4.0)

    def test_protocol_is_stable_and_never_enables_live_actions(self):
        protocol, first = load_phase2_protocol()
        _, second = load_phase2_protocol()
        self.assertEqual(first, second)
        self.assertFalse(protocol["safety"]["alerts_enabled"])
        self.assertFalse(protocol["safety"]["orders_enabled"])
        self.assertFalse(protocol["safety"]["live_approved"])
        self.assertFalse(protocol["model"]["holdout_used_for_selection"])

    def test_regularized_logistic_learns_direction_without_future_fields(self):
        rng = np.random.default_rng(7)
        X = rng.normal(size=(600, len(Phase2ModelEngine.FEATURE_NAMES)))
        y = (1.7 * X[:, 0] + 1.2 * X[:, 1] - 0.8 * X[:, 5] > 0).astype(float)
        model = Phase2ModelEngine._fit(X, y, l2=1.0)
        probability = Phase2ModelEngine._predict(model, X)
        self.assertTrue(model["converged"])
        self.assertGreater(Phase2ModelEngine._auc(y.astype(int), probability), 0.95)
        self.assertGreater(model["beta"][1], 0)
        self.assertGreater(model["beta"][2], 0)
        self.assertLess(model["beta"][6], 0)

    def test_performance_uses_net_returns_and_session_cluster_bootstrap(self):
        rows = []
        for day in range(20):
            for trade in range(10):
                rows.append({"session": f"2026-07-{day + 1:02d}", "net_return_pct": 1.0 if trade < 7 else -1.0})
        result = Phase2ModelEngine._performance(rows, bootstrap_samples=500)
        self.assertEqual(result["trades"], 200)
        self.assertEqual(result["active_sessions"], 20)
        self.assertGreater(result["profit_factor"], 1.15)
        self.assertGreaterEqual(result["session_cluster_bootstrap_positive_probability"], 0.95)

    def test_chronological_folds_never_train_on_or_after_validation(self):
        rows = []
        for day in range(1, 31):
            for index in range(2):
                rows.append({"session": f"2026-06-{day:02d}", "t": f"2026-06-{day:02d}T14:0{index}:00Z"})
        for train, valid in Phase2ModelEngine._chronological_folds(rows):
            self.assertLess(max(row["session"] for row in train), min(row["session"] for row in valid))


if __name__ == "__main__":
    unittest.main()
