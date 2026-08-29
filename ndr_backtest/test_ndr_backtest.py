import json
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("NDR_BT_REDIS_PREFIX", "next_day_radar_backtest_v3")
from ndr_backtest_engine import BacktestCollector


class FakeRedis:
    def __init__(self):
        self.values = {"next_day_radar_backtest_v3:manifest": json.dumps({
            "sessions": ["2026-08-28"], "development_sessions": [], "holdout_sessions": ["2026-08-28"]
        })}
    def get_json(self, key, default=None):
        return json.loads(self.values[key]) if key in self.values else default
    def set_json(self, key, value): self.values[key] = json.dumps(value)
    def command(self, *parts):
        if parts[0] == "GET": return self.values.get(parts[1])
        if parts[0] == "SET": self.values[parts[1]] = str(parts[2]); return "OK"
        if parts[0] == "SCARD": return 1
        raise AssertionError(parts)


def bars(count=80):
    start = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
    out = []
    for i in range(count):
        price = 5 + i * .015
        out.append({"t": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
                    "o": price-.01, "h": price+.03, "l": price-.02, "c": price,
                    "v": 1000+i*40, "n": 20+i, "vw": price-.005, "feed": "sip"})
    return out


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.engine = BacktestCollector(FakeRedis(), object())
        self.engine.opp_min = 0
        self.engine.fail_max = 100

    def test_strict_never_substitutes_missing_point_in_time_data(self):
        result = self.engine.replay("2026-08-28", "TEST", bars(), "strict")
        self.assertIsNone(result["breakout_ready"])
        self.assertIn("strict_feature_unavailable", result["block_reasons"])
        self.assertIn("point_in_time_float", result["unavailable_features"])

    def test_approx_signal_is_causal(self):
        def known_good(history, mode):
            price = float(history[-1]["c"])
            return {"price": price, "vwap": price-.01, "change_pct": 5,
                    "resistance": price-.01, "opportunity": 95,
                    "failure_pressure": 10, "demand_efficiency": 90,
                    "price_acceptance": 90, "volume_acceleration": 2,
                    "spread_pct": .5, "extension_risk": 5,
                    "unavailable_features": []}
        self.engine.features = known_good
        original = bars()
        first = self.engine.replay("2026-08-28", "TEST", original, "approx")["breakout_ready"]
        self.assertIsNotNone(first)
        changed = [dict(x) for x in original]
        changed[-1].update({"h": 99, "c": 99})
        second = self.engine.replay("2026-08-28", "TEST", changed, "approx")["breakout_ready"]
        self.assertEqual(first["ts"], second["ts"])
        self.assertEqual(first["price"], second["price"])

    def test_feed_merge_uses_boats_only_overnight(self):
        sip = [{"t": "2026-08-28T02:00:00Z", "c": 1}, {"t": "2026-08-28T14:00:00Z", "c": 2}]
        boats = [{"t": "2026-08-28T02:00:00Z", "c": 3}, {"t": "2026-08-28T14:00:00Z", "c": 4}]
        merged = self.engine.merge_bars(sip, boats)
        self.assertEqual([x["c"] for x in merged], [3, 2])

    def test_holdout_partition_is_separate(self):
        self.assertEqual(self.engine.partition("2026-08-28"), "holdout")


if __name__ == "__main__": unittest.main()
