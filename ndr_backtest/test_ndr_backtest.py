import json
import math
import os
import time
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("NDR_BT_REDIS_PREFIX", "next_day_radar_backtest_v3")
from ndr_backtest_engine import BacktestCollector, avg, clamp


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


class BatchRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.values["next_day_radar_backtest_v3:detail_cursor"] = json.dumps(
            {"session_index": 0, "sscan_cursor": "1220", "processed": 4348})
        self.calls = []
        self.existing = json.dumps({"preserved": True})
    def command(self, *parts):
        self.calls.append(parts)
        if parts[0] == "SSCAN": return ["1250", ["AAA", "BBB"]]
        if parts[0] == "HMGET": return [self.existing, None, None, None]
        if parts[0] == "HSET": return len(parts[2:]) // 2
        if parts[0] == "SADD": return len(parts) - 2
        return super().command(*parts)


class BatchAlpaca:
    def bars(self, symbols, start, end, feed):
        data = bars(40)
        return {symbol: data for symbol in symbols}


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
        def known_good(history, mode, running=None):
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

    def test_running_feature_optimization_is_numerically_identical(self):
        history = bars(80)
        running = {
            "reference": float(history[0]["o"]),
            "volume": sum(float(x["v"]) for x in history),
            "vwap_numerator": sum(float(x["vw"])*float(x["v"]) for x in history),
        }
        optimized = BacktestCollector.features(history, "approx", running)
        closes=[float(x["c"]) for x in history]; highs=[float(x["h"]) for x in history]; vols=[float(x.get("v",0)) for x in history]
        price,ref=closes[-1],float(history[0]["o"]); total=sum(vols); vwap=sum(float(x.get("vw") or x["c"])*float(x.get("v",0)) for x in history)/total
        recent=history[-12:]; prior=history[-24:-12]; rv=avg(float(x.get("v",0)) for x in recent); pv=avg((float(x.get("v",0)) for x in prior),max(1,rv)); accel=rv/max(1,pv)
        window=closes[-30:]; lo,hi=min(window),max(window); span=max(1e-9,hi-lo); acceptance=clamp(100*avg(1 if x>=lo+.55*span else 0 for x in window)); closepos=clamp(100*(price-lo)/span)
        demand=clamp(.5*closepos+.25*acceptance+.25*min(100,accel*40)); resistance=max(highs[-21:-1] or highs[-1:]); reclaim=100 if price>=resistance*.998 else clamp(50+(price/resistance-1)*1000)
        pullback=clamp(100-max(0,(hi-price)/max(hi,1e-9)*500)); change=(price/ref-1)*100; extension=clamp(max(0,change-12)*4+max(0,(price/max(vwap,1e-9)-1)*100-8)*5)
        trajectory=clamp(50+(closes[-1]/max(closes[max(0,len(closes)-10)],1e-9)-1)*700); continuity=clamp(avg(1 if float(x.get("n",0))>0 else 0 for x in recent)*100)
        spread=clamp(avg((float(x["h"])-float(x["l"]))/max(float(x["c"]),1e-9)*100 for x in recent),0,25); spreadq=clamp(100-spread*15)
        participation=clamp(avg([min(100,accel*45),continuity,min(100,math.log10(total+1)*18)])); persistence=clamp(avg([acceptance,pullback,trajectory])); liquidity=clamp(avg([spreadq,continuity])); context=clamp(avg([100-extension,trajectory]))
        legacy={"price":price,"vwap":vwap,"change_pct":change,"resistance":resistance,"opportunity":clamp(.24*participation+.34*demand+.18*persistence+.10*liquidity+.14*context),"failure_pressure":clamp(.30*(100-demand)+.25*(100-acceptance)+.20*(100-pullback)+.15*(100-reclaim)+.10*(100-spreadq)),"demand_efficiency":demand,"price_acceptance":acceptance,"volume_acceleration":accel,"spread_pct":spread,"extension_risk":extension}
        for key, value in legacy.items(): self.assertAlmostEqual(optimized[key], value, places=10, msg=key)

    def test_optimized_minute_replay_is_fast(self):
        started = time.perf_counter()
        self.engine.replay("2026-08-28", "TEST", bars(1500), "approx")
        self.assertLess(time.perf_counter()-started, 1.0)

    def test_batch_write_preserves_cursor_and_existing_results(self):
        redis = BatchRedis()
        engine = BacktestCollector(redis, BatchAlpaca())
        engine.delay = 0
        engine.detail_replay_step()
        hmget = [x for x in redis.calls if x[0] == "HMGET"]
        hset = [x for x in redis.calls if x[0] == "HSET"]
        sadd = [x for x in redis.calls if x[0] == "SADD"]
        self.assertEqual(len(hmget), 1)
        self.assertEqual(len(hset), 1)
        self.assertNotIn("2026-08-28|AAA|strict", hset[0])
        self.assertEqual(len(sadd), 1)
        cursor = redis.get_json("next_day_radar_backtest_v3:detail_cursor")
        self.assertEqual(cursor, {"session_index": 0, "sscan_cursor": "1250", "processed": 4350})


if __name__ == "__main__": unittest.main()
