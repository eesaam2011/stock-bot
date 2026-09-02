import json
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("NDR_BT_REDIS_PREFIX", "next_day_radar_backtest_v3")

from evidence_first_engine import EvidenceFirstEngine, load_protocol
from ndr_backtest_engine import BacktestCollector


class FakeRedis:
    def __init__(self):
        self.values = {
            "next_day_radar_backtest_v3:manifest": json.dumps({
                "sessions": ["2026-08-27", "2026-08-28"],
                "development_sessions": ["2026-08-27"],
                "holdout_sessions": ["2026-08-28"],
                "universe_symbols": ["AAA"],
            })
        }

    def get_json(self, key, default=None):
        return json.loads(self.values[key]) if key in self.values else default

    def set_json(self, key, value):
        self.values[key] = json.dumps(value)

    def command(self, *parts):
        if parts[0] == "GET":
            return self.values.get(parts[1])
        if parts[0] == "SET":
            self.values[parts[1]] = str(parts[2])
            return "OK"
        if parts[0] == "HMGET":
            return [None] * (len(parts) - 2)
        if parts[0] == "HSET":
            for index in range(2, len(parts), 2):
                self.values[f"{parts[1]}::{parts[index]}"] = parts[index + 1]
            return (len(parts) - 2) // 2
        if parts[0] == "SMEMBERS":
            return ["AAA"]
        raise AssertionError(parts)


class NoopAlpaca:
    def bars(self, symbols, start, end, feed, adjustment="raw"):
        return {symbol: [] for symbol in symbols}


def make_bar(stamp, open_, high, low, close, volume=100, vwap=None):
    return {
        "t": stamp.isoformat().replace("+00:00", "Z"),
        "o": open_, "h": high, "l": low, "c": close,
        "v": volume, "vw": close if vwap is None else vwap,
    }


def orb_bars(session="2026-08-27", with_retest=False):
    start = datetime(2026, 8, 27, 13, 30, tzinfo=timezone.utc)
    rows = []
    opening = [10.0, 10.1, 10.15, 10.1, 10.12]
    for index, close in enumerate(opening):
        rows.append(make_bar(start + timedelta(minutes=index), close - .03, close + .08, close - .08, close, 100))
    # OR high is 10.23. Breakout close exceeds the 0.2% buffer and volume ratio.
    rows.append(make_bar(start + timedelta(minutes=5), 10.18, 10.42, 10.17, 10.38, 220))
    if with_retest:
        rows.append(make_bar(start + timedelta(minutes=6), 10.35, 10.37, 10.22, 10.25, 90))
        rows.append(make_bar(start + timedelta(minutes=7), 10.25, 10.45, 10.24, 10.41, 150))
    else:
        rows.append(make_bar(start + timedelta(minutes=6), 10.39, 10.60, 10.35, 10.55, 100))
    for index in range(len(rows), 70):
        rows.append(make_bar(start + timedelta(minutes=index), 10.5, 10.7, 10.45, 10.55, 100))
    return rows


class EvidenceFirstTests(unittest.TestCase):
    def setUp(self):
        collector = BacktestCollector(FakeRedis(), NoopAlpaca())
        self.engine = EvidenceFirstEngine(collector)

    def test_protocol_has_stable_fingerprint_and_no_live_actions(self):
        _, first = load_protocol()
        _, second = load_protocol()
        self.assertEqual(first, second)
        record = self.engine.protocol_record()
        self.assertFalse(record["alerts_enabled"])
        self.assertFalse(record["orders_enabled"])

    def test_protocol_lock_rejects_rule_changes(self):
        self.engine.lock_protocol()
        key = self.engine.key("protocol_lock")
        bad = self.engine.redis.get_json(key)
        bad["protocol_sha256"] = "changed"
        self.engine.redis.set_json(key, bad)
        with self.assertRaises(RuntimeError):
            self.engine.lock_protocol()

    def test_orb_uses_completed_opening_range_and_detects_breakout(self):
        signals = self.engine.detect("2026-08-27", orb_bars())
        self.assertIsNotNone(signals["ORB_5M"])
        self.assertEqual(signals["ORB_5M"]["signal_ts"], "2026-08-27T13:35:00Z")

    def test_retest_requires_touch_then_later_reclaim(self):
        signals = self.engine.detect("2026-08-27", orb_bars(with_retest=True))
        signal = signals["BREAKOUT_RETEST_RECLAIM"]
        self.assertIsNotNone(signal)
        self.assertEqual(signal["retest_ts"], "2026-08-27T13:36:00Z")
        self.assertEqual(signal["signal_ts"], "2026-08-27T13:37:00Z")

    def test_entry_is_next_bar_open(self):
        bars = orb_bars()
        signal = self.engine.detect("2026-08-27", bars)["ORB_5M"]
        result = self.engine.simulate("2026-08-27", "AAA", "ORB_5M", bars, signal)
        self.assertTrue(result["tradable"])
        self.assertEqual(result["entry_ts"], "2026-08-27T13:36:00Z")
        self.assertEqual(result["entry"], 10.39)

    def test_same_bar_stop_and_target_is_stop_first(self):
        bars = orb_bars()
        signal = self.engine.detect("2026-08-27", bars)["ORB_5M"]
        entry_index = next(i for i, bar in enumerate(bars) if bar["t"] == "2026-08-27T13:36:00Z")
        bars[entry_index + 1].update({"h": 12.0, "l": 9.0})
        result = self.engine.simulate("2026-08-27", "AAA", "ORB_5M", bars, signal)
        self.assertEqual(result["status"], "stop_exit")

    def test_future_bar_change_does_not_change_signal(self):
        bars = orb_bars()
        first = self.engine.detect("2026-08-27", bars)["ORB_5M"]
        changed = [dict(bar) for bar in bars]
        changed[-1].update({"h": 999, "l": .01, "c": 500, "v": 999999})
        second = self.engine.detect("2026-08-27", changed)["ORB_5M"]
        self.assertEqual(first, second)

    def test_development_session_list_never_opens_legacy_holdout(self):
        self.assertEqual(self.engine._development_sessions(), ["2026-08-27"])

    def test_readiness_reports_full_universe_without_opening_holdout(self):
        result = self.engine.readiness()
        self.assertTrue(result["ready_for_decisive_development_run"])
        self.assertEqual(result["development_sessions"], 1)
        self.assertEqual(result["full_universe_count"], 1)
        self.assertTrue(result["legacy_holdout_locked"])
        self.assertFalse(result["alerts_enabled"])

    def test_profitable_metrics_do_not_pass_adoption_on_noncausal_universe(self):
        rows = []
        for day in range(1, 26):
            session = f"2026-07-{day:02d}"
            for index in range(4):
                rows.append({"path": "ORB_5M", "session": session, "symbol": f"A{index}",
                             "entry_ts": f"{session}T14:00:00Z", "tradable": True,
                             "status": "target_2r_exit", "gross_return_pct": 3.0})
        result = self.engine.analyze(rows, universe_causal=False)["ORB_5M"]
        self.assertTrue(result["development_metric_gate_passed"])
        self.assertFalse(result["eligible_for_forward_holdout"])
        self.assertFalse(result["live_approved"])


if __name__ == "__main__":
    unittest.main()
