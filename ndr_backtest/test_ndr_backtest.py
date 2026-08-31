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
        if parts[0] == "HMGET":
            if parts[1].endswith(":results"): return [self.existing, None, None, None]
            return [None] * (len(parts)-2)
        if parts[0] == "HSET": return len(parts[2:]) // 2
        if parts[0] == "SADD": return len(parts) - 2
        return super().command(*parts)


class BatchAlpaca:
    def bars(self, symbols, start, end, feed):
        data = bars(40)
        return {symbol: data for symbol in symbols}


class ReportRedis(FakeRedis):
    def command(self, *parts):
        if parts[0] == "HSCAN":
            if parts[1].endswith(":results"):
                return ["0", ["old", json.dumps({"symbol": "OLD"})]]
            if parts[1].endswith(":results:2026-08-28"):
                return ["0", ["new", json.dumps({"symbol": "NEW"})]]
            return ["0", []]
        return super().command(*parts)


class RawRedis(FakeRedis):
    def __init__(self):
        super().__init__();self.row={"session":"2026-08-28","symbol":"AAA","mode":"approx","breakout_ready":None}
    def command(self,*parts):
        if parts[0]=="HGET":
            return json.dumps(self.row) if parts[1].endswith(":results:2026-08-28") and parts[2].endswith("|AAA|approx") else None
        if parts[0]=="HMGET":
            return [json.dumps(dict(self.row,mode="approx")) if field.endswith("|AAA|approx") and parts[1].endswith(":results:2026-08-28") else None for field in parts[2:]]
        if parts[0]=="HSCAN":
            return ["0",["2026-08-28|AAA|approx",json.dumps(self.row)]] if parts[1].endswith(":results:2026-08-28") else ["0",[]]
        return super().command(*parts)


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
        self.assertEqual(len(hmget), 2)
        self.assertEqual(len(hset), 1)
        self.assertNotIn("2026-08-28|AAA|strict", hset[0])
        self.assertEqual(hset[0][1], "next_day_radar_backtest_v3:results:2026-08-28")
        self.assertEqual(len(sadd), 1)
        cursor = redis.get_json("next_day_radar_backtest_v3:detail_cursor")
        self.assertEqual(cursor, {"session_index": 0, "sscan_cursor": "1250", "processed": 4350})

    def test_large_hash_write_is_split_into_bounded_requests(self):
        redis = BatchRedis()
        engine = BacktestCollector(redis, object())
        pairs = [(f"field-{i}", "x"*1000) for i in range(25)]
        engine.hset_bounded("results", pairs, max_fields=10, max_bytes=200000)
        hsets = [x for x in redis.calls if x[0] == "HSET"]
        self.assertEqual([len(x[2:])//2 for x in hsets], [10, 10, 5])

    def test_report_reads_legacy_and_session_shards(self):
        engine = BacktestCollector(ReportRedis(), object())
        self.assertEqual([x["symbol"] for x in engine.iter_results()], ["OLD", "NEW"])

    def test_frozen_threshold_analysis_separates_holdout_and_signal_types(self):
        engine = BacktestCollector(FakeRedis(), object())
        engine.iter_results = lambda: iter([
            {"mode":"approx","partition":"development","session":"2026-08-01","breakout_ready":{"opportunity":94,"failure_pressure":30,"mfe_pct":8,"mae_pct":-2,"phase":"regular"},"confirmed_entry":{"opportunity":94,"failure_pressure":30,"mfe_pct":6,"mae_pct":-1,"phase":"regular"}},
            {"mode":"approx","partition":"development","session":"2026-08-02","breakout_ready":{"opportunity":92,"failure_pressure":20,"mfe_pct":20,"mae_pct":-5,"phase":"premarket"},"confirmed_entry":None},
            {"mode":"approx","partition":"holdout","session":"2026-08-20","breakout_ready":{"opportunity":95,"failure_pressure":35,"mfe_pct":10,"mae_pct":-4,"phase":"overnight"},"confirmed_entry":{"opportunity":95,"failure_pressure":36,"mfe_pct":12,"mae_pct":-3,"phase":"overnight"}},
            {"mode":"strict","partition":"holdout","session":"2026-08-21","breakout_ready":{"opportunity":99,"failure_pressure":1,"mfe_pct":99,"mae_pct":0,"phase":"regular"},"confirmed_entry":None},
        ])
        result=engine.threshold_analysis(93,35)
        self.assertEqual(result["source_rows_scanned"],4)
        self.assertEqual(result["partitions"]["development"]["breakout_ready"]["count"],1)
        self.assertEqual(result["partitions"]["holdout"]["breakout_ready"]["mfe_ge_5_rate"],100.0)
        self.assertEqual(result["partitions"]["holdout"]["confirmed_entry"]["count"],0)
        self.assertEqual(result["partitions"]["development"]["breakout_ready"]["sessions"][0]["session"],"2026-08-01")
        self.assertIsNotNone(engine.threshold_analysis_result())

    def test_thresholds_below_stored_ready_floor_are_rejected(self):
        with self.assertRaises(ValueError):self.engine.threshold_analysis(87,35)

    def test_raw_case_prefers_session_shard(self):
        result=BacktestCollector(RawRedis(),object()).raw_case("2026-08-28","AAA","approx")
        self.assertEqual(result["symbol"],"AAA")

    def test_raw_symbol_is_session_paginated_and_reads_both_modes(self):
        result=BacktestCollector(RawRedis(),object()).raw_symbol("AAA",0,1)
        self.assertEqual(result["sessions_scanned"],1);self.assertEqual(len(result["results"]),1);self.assertIsNone(result["next_offset"])

    def test_raw_session_moves_from_legacy_to_shard(self):
        engine=BacktestCollector(RawRedis(),object());legacy=engine.raw_session("2026-08-28","legacy","0",100);shard=engine.raw_session("2026-08-28",legacy["next_source"],legacy["next_cursor"],100)
        self.assertEqual(legacy["next_source"],"shard");self.assertEqual(shard["results"][0]["symbol"],"AAA");self.assertIsNone(shard["next_source"])

    def test_trade_simulation_uses_next_bar_open_and_stop_first(self):
        candidate={"session":"2026-08-28","partition":"holdout","symbol":"AAA","signal":{"ts":"2026-08-28T14:00:00Z","price":10,"stop":9,"opportunity":94,"failure_pressure":10}}
        rows=[{"t":"2026-08-28T14:01:00Z","o":10,"h":11.6,"l":8.9,"c":10.5}]
        conservative=BacktestCollector.simulate_trade(candidate,rows,"conservative","full_t1");optimistic=BacktestCollector.simulate_trade(candidate,rows,"optimistic","full_t1")
        self.assertEqual(conservative["status"],"stop_after_entry");self.assertEqual(conservative["return_pct"],-10.0);self.assertEqual(optimistic["status"],"t1_exit");self.assertEqual(optimistic["return_pct"],15.0);self.assertEqual(conservative["ambiguous_bars"],1)

    def test_scaled_policy_moves_stop_to_breakeven_after_t1(self):
        candidate={"session":"2026-08-28","partition":"holdout","symbol":"AAA","signal":{"ts":"2026-08-28T14:00:00Z","price":10,"stop":9,"opportunity":94,"failure_pressure":10}}
        rows=[{"t":"2026-08-28T14:01:00Z","o":10,"h":11.6,"l":9.5,"c":11.5},{"t":"2026-08-28T14:02:00Z","o":11.4,"h":11.5,"l":9.9,"c":10}]
        result=BacktestCollector.simulate_trade(candidate,rows,"conservative","scaled")
        self.assertEqual(result["targets_hit"],["T1"]);self.assertEqual(result["status"],"stop_after_t1");self.assertEqual(result["return_pct"],7.5)

    def test_diagnostic_detects_stop_then_recovery(self):
        candidate={"session":"2026-08-28","partition":"holdout","symbol":"AAA","signal":{"ts":"2026-08-28T14:00:00Z","price":10,"stop":9,"stop_pct":10,"resistance":10,"opportunity":94,"failure_pressure":10}}
        rows=[{"t":"2026-08-28T14:00:00Z","o":9.8,"h":10.1,"l":9.7,"c":10,"v":100},{"t":"2026-08-28T14:01:00Z","o":10,"h":10.2,"l":8.9,"c":9.1,"v":200},{"t":"2026-08-28T14:02:00Z","o":9.1,"h":11.6,"l":9,"c":11.5,"v":300}]
        result=BacktestCollector.diagnose_trade(candidate,rows)
        self.assertEqual(result["classification"],"stop_then_recovered_t1");self.assertEqual(result["minutes_to_stop"],0.0);self.assertEqual(result["minutes_to_t1"],1.0);self.assertGreaterEqual(result["post_stop_mfe_pct"],15)

    def test_diagnostic_separates_true_failed_entry(self):
        candidate={"session":"2026-08-28","partition":"holdout","symbol":"AAA","signal":{"ts":"2026-08-28T14:00:00Z","price":10,"stop":9,"stop_pct":10,"resistance":10,"opportunity":94,"failure_pressure":10}}
        rows=[{"t":"2026-08-28T14:01:00Z","o":10,"h":10.1,"l":8.9,"c":9,"v":200},{"t":"2026-08-28T14:02:00Z","o":9,"h":9.5,"l":8.8,"c":9.2,"v":100}]
        self.assertEqual(BacktestCollector.diagnose_trade(candidate,rows)["classification"],"stop_never_recovered_t1")


if __name__ == "__main__": unittest.main()
