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
    def bars(self, symbols, start, end, feed, adjustment="raw"):
        data = bars(40)
        return {symbol: data for symbol in symbols}


class AdjustedAlpaca:
    def bars(self,symbols,start,end,feed,adjustment="raw"):
        rows=[{"t":"2026-08-28T14:00:00Z","o":1,"h":1,"l":1,"c":1,"v":100},{"t":"2026-08-28T14:10:00Z","o":1.2,"h":1.2,"l":1.2,"c":1.2,"v":100},{"t":"2026-08-28T14:30:00Z","o":1.3,"h":1.6,"l":1.2,"c":1.5,"v":100}]
        return {symbol:(rows if feed=="sip" else []) for symbol in symbols}


class SplitAdjustedAlpaca:
    def bars(self,symbols,start,end,feed,adjustment="raw"):
        rows=[{"t":"2026-08-28T14:00:00Z","o":10,"h":10,"l":10,"c":10,"v":100},{"t":"2026-08-28T14:01:00Z","o":10,"h":11,"l":9.8,"c":10.5,"v":100}]
        return {symbol:(rows if feed=="sip" else []) for symbol in symbols}


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

    def test_weekday_report_uses_all_signal_outcomes_and_separate_partitions(self):
        redis=FakeRedis();redis.values["next_day_radar_backtest_v3:manifest"]=json.dumps({"sessions":["2026-08-27","2026-08-28"],"development_sessions":["2026-08-27"],"holdout_sessions":["2026-08-28"]})
        engine=BacktestCollector(redis,object());engine.iter_results=lambda:iter([
            {"mode":"approx","partition":"development","session":"2026-08-27","symbol":"WIN","breakout_ready":{"opportunity":94,"failure_pressure":20,"mfe_pct":8,"mae_pct":-1,"phase":"REGULAR"},"confirmed_entry":{"opportunity":94,"failure_pressure":20,"mfe_pct":6,"mae_pct":-1,"phase":"REGULAR"}},
            {"mode":"approx","partition":"development","session":"2026-08-27","symbol":"LOSS","breakout_ready":{"opportunity":89,"failure_pressure":20,"mfe_pct":1,"mae_pct":-3,"phase":"PREMARKET"},"confirmed_entry":None},
            {"mode":"approx","partition":"holdout","session":"2026-08-28","symbol":"LOSS2","breakout_ready":{"opportunity":95,"failure_pressure":20,"mfe_pct":-2,"mae_pct":-5,"phase":"REGULAR"},"confirmed_entry":None},
            {"mode":"strict","partition":"holdout","session":"2026-08-28","symbol":"IGNORE","breakout_ready":{"opportunity":99,"failure_pressure":1,"mfe_pct":99,"mae_pct":0,"phase":"REGULAR"},"confirmed_entry":None},
        ])
        result=engine.weekday_signal_report();thu=result["thresholds"]["88/35"]["development"]["Thursday"]["breakout_ready"];fri=result["thresholds"]["93/35"]["holdout"]["Friday"]["breakout_ready"]
        self.assertEqual(result["source_rows_scanned"],4);self.assertEqual(thu["signals"],2);self.assertEqual(thu["mfe_ge_5_rate"],50.0);self.assertEqual(thu["sessions"],1)
        self.assertEqual(fri["signals"],1);self.assertEqual(fri["mfe_ge_0_rate"],0.0);self.assertEqual(result["thresholds"]["93/35"]["development"]["Thursday"]["confirmed_entry"]["signals"],1)
        self.assertFalse(result["methodology"]["winner_only_explosions_file_used"]);self.assertIsNotNone(engine.weekday_signal_result())

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

    def test_explosion_catalog_lists_and_buckets_named_cases(self):
        engine=BacktestCollector(FakeRedis(),object());engine.iter_results=lambda:iter([{"mode":"approx","session":"2026-08-28","partition":"holdout","symbol":"AAA","breakout_ready":{"ts":"x","phase":"REGULAR","price":1,"opportunity":90,"failure_pressure":10,"mfe_pct":25,"mae_pct":-2,"time_to_mfe_minutes":5,"forward_bars":10},"confirmed_entry":{"ts":"y","phase":"REGULAR","price":1.1,"opportunity":94,"failure_pressure":8,"mfe_pct":9,"mae_pct":-1,"time_to_mfe_minutes":4,"forward_bars":9}},{"mode":"strict","session":"2026-08-28","partition":"holdout","symbol":"BBB","breakout_ready":{"mfe_pct":99},"confirmed_entry":None},{"mode":"approx","session":"2026-08-28","partition":"holdout","symbol":"CCC","breakout_ready":None,"confirmed_entry":None}])
        result=engine.build_explosion_catalog()
        self.assertEqual(len(result["cases"]),2);self.assertEqual(result["cases"][0]["symbol"],"AAA");self.assertEqual(result["summary"]["breakout_ready"]["mfe_ge_20"],1);self.assertEqual(result["summary"]["confirmed_entry"]["mfe_ge_10"],0);self.assertIn("Rejected cases",result["limitation"])

    def test_big_move_review_pairs_ready_with_entry_and_delay(self):
        engine=BacktestCollector(FakeRedis(),AdjustedAlpaca());engine.delay=0;engine.iter_results=lambda:iter([{"mode":"approx","session":"2026-08-28","partition":"holdout","symbol":"AAA","block_reasons":{"breakout_not_held":3},"unavailable_features":{},"breakout_ready":{"ts":"2026-08-28T14:00:00Z","phase":"PREMARKET","price":1,"opportunity":94,"failure_pressure":10,"mfe_pct":55,"mae_pct":-2,"time_to_mfe_minutes":30},"confirmed_entry":{"ts":"2026-08-28T14:10:00Z","phase":"REGULAR","price":1.2,"opportunity":90,"failure_pressure":12,"mfe_pct":12,"mae_pct":-1,"time_to_mfe_minutes":20}},{"mode":"approx","session":"2026-08-28","partition":"holdout","symbol":"BBB","block_reasons":{},"unavailable_features":{},"breakout_ready":{"ts":"2026-08-28T15:00:00Z","phase":"REGULAR","price":2,"opportunity":91,"failure_pressure":15,"mfe_pct":19,"mae_pct":-1,"time_to_mfe_minutes":10},"confirmed_entry":None}])
        result=engine.build_big_move_review();summary=result["summary"]["all"]
        self.assertEqual(len(result["cases"]),1);self.assertEqual(result["cases"][0]["entry_delay_minutes"],10.0);self.assertEqual(result["raw_candidate_count"],1);self.assertEqual(result["excluded_after_split_adjustment"],0);self.assertEqual(summary["with_confirmed_entry"],1);self.assertEqual(summary["entry_retained_mfe_ge_10"],1);self.assertEqual(summary["ready_opportunity_bands"]["93_plus"],1)

    def test_split_adjustment_removes_mechanical_raw_explosion(self):
        engine=BacktestCollector(FakeRedis(),SplitAdjustedAlpaca());engine.delay=0;engine.iter_results=lambda:iter([{"mode":"approx","session":"2026-08-28","partition":"holdout","symbol":"AAA","block_reasons":{},"unavailable_features":{},"breakout_ready":{"ts":"2026-08-28T14:00:00Z","phase":"REGULAR","price":0.1,"opportunity":94,"failure_pressure":10,"mfe_pct":10900,"mae_pct":0,"time_to_mfe_minutes":1},"confirmed_entry":None}])
        result=engine.build_big_move_review()
        self.assertEqual(result["raw_candidate_count"],1);self.assertEqual(result["clean_case_count"],0);self.assertEqual(result["excluded_after_split_adjustment"],1);self.assertTrue(result["excluded_cases"][0]["corporate_action_contaminated"])

    def test_pcprofit_case_excludes_bars_after_t_plus_60(self):
        # a bar just after T+60 has an enormous high that must NOT leak into the recomputed outcome
        c={"symbol":"AAA","session":"2026-08-28","partition":"development","t":"2026-08-28T14:00:00Z","price":10.0}
        past=[{"t":f"2026-08-28T13:{15+i:02d}:00Z","o":10,"h":10.2,"l":9.9,"c":10.05+i*0.01,"v":500} for i in range(45)]
        within_window=[{"t":f"2026-08-28T14:{5*i+5:02d}:00Z","o":10.4,"h":10.6,"l":10.3,"c":10.5,"v":500} for i in range(6)]
        after_t60=[{"t":"2026-08-28T15:05:00Z","o":10.5,"h":999.0,"l":10.4,"c":10.5,"v":500}]  # 5 min after the T+60 boundary
        row=BacktestCollector._pcprofit_compute_case(c,past+within_window+after_t60)
        self.assertIsNotNone(row)
        self.assertLess(row["recomputed_mfe_pct"],10)  # would be >9800% if the T+65 bar leaked in

    def test_pcprofit_cost_is_applied_per_trade_before_profit_factor(self):
        rows=[{"symbol":f"S{i}","session":"2026-08-28","t":"x","partition":"development","has_plan":True,
               "price_change_pct_last45m":0.0,"status":"t1_exit" if i%2==0 else "stop_after_entry",
               "return_pct":2.0 if i%2==0 else -1.0,"recomputed_mfe_pct":2.0,"recomputed_mae_pct":-1.0} for i in range(200)]
        result=BacktestCollector._pcprofit_analyze(rows)
        base=result["development"]["baseline_no_filter"]
        pf_0=base["cost_0_0pct"]["summary"]["profit_factor"];pf_50=base["cost_0_5pct"]["summary"]["profit_factor"]
        self.assertGreater(pf_0,pf_50)  # a flat 0.50pp round-trip cost per trade must strictly reduce PF
        self.assertAlmostEqual(base["cost_0_0pct"]["summary"]["avg_return_pct"]-0.5,base["cost_0_5pct"]["summary"]["avg_return_pct"],places=6)

    def test_pcprofit_selection_uses_development_only_and_respects_min_sample(self):
        rows=[]
        for i in range(300):
            partition="development" if i<250 else "holdout"
            pc=5.0 if i%3==0 else 0.0  # only ~1/3 of rows clear a threshold of e.g. 3
            rows.append({"symbol":f"S{i}","session":"2026-08-28","t":"x","partition":partition,"has_plan":True,
                "price_change_pct_last45m":pc,"status":"t1_exit","return_pct":1.0,
                "recomputed_mfe_pct":1.0,"recomputed_mae_pct":-0.5})
        result=BacktestCollector._pcprofit_analyze(rows)
        baseline_n=result["development"]["baseline_no_filter"]["cost_0_0pct"]["n_tradable"]
        self.assertEqual(baseline_n,250)
        self.assertEqual(result["frozen_threshold_selection"]["min_n_required"],max(100,int(round(0.10*baseline_n))))
        # a threshold with fewer tradable development rows than min_n must never be eligible
        for th in result["frozen_threshold_selection"]["eligible_thresholds"]:
            self.assertGreaterEqual(result["development"][f"threshold_{th}pct"]["cost_0_25pct"]["n_tradable"],result["frozen_threshold_selection"]["min_n_required"])
        # holdout rows must never influence eligibility/selection
        holdout_n_at_threshold_5=len([r for r in rows if r["partition"]=="holdout" and r["price_change_pct_last45m"]>=5])
        self.assertNotEqual(result["frozen_threshold_selection"]["min_n_required"],holdout_n_at_threshold_5)

    def test_pcprofit_rejects_threshold_that_beats_baseline_but_is_still_unprofitable(self):
        rows=[]
        for i in range(400):
            partition="development" if i<300 else "holdout"
            pc=4.0 if i%2==0 else 0.0
            # baseline: mostly losers (PF well below 1). Filtered subset (pc>=some threshold) is LESS bad but still losing.
            ret=-0.5 if i%2==0 else -3.0
            rows.append({"symbol":f"S{i}","session":"2026-08-28","t":"x","partition":partition,"has_plan":True,
                "price_change_pct_last45m":pc,"status":"stop_after_entry","return_pct":ret,
                "recomputed_mfe_pct":0.5,"recomputed_mae_pct":-2.0})
        result=BacktestCollector._pcprofit_analyze(rows)
        for th in [0,1,2,3,4]:
            stats=result["development"][f"threshold_{th}pct"]["cost_0_25pct"]["summary"]
            if stats:self.assertLess(stats["profit_factor"],1.0)
        self.assertIsNone(result["frozen_threshold_selection"]["frozen_threshold_pct"])
        self.assertNotIn("holdout_validation",result)


if __name__ == "__main__": unittest.main()
