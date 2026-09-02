import json, unittest
from datetime import datetime, timedelta, timezone

from market_radar_backtest_engine import MarketRadarBacktest


class FakeRedis:
    def __init__(self):
        self.values={"next_day_radar_backtest_v3:manifest":json.dumps({"sessions":["2026-08-28"],"development_sessions":[],"holdout_sessions":["2026-08-28"]})}
    def get_json(self,key,default=None):return json.loads(self.values[key]) if key in self.values else default
    def set_json(self,key,value):self.values[key]=json.dumps(value);return "OK"
    def command(self,*parts):
        if parts[0]=="GET":return self.values.get(parts[1])
        if parts[0]=="SET":self.values[parts[1]]=str(parts[2]);return "OK"
        if parts[0]=="SCARD":return 1
        raise AssertionError(parts)


def sample_bars(count=90):
    start=datetime(2026,8,28,12,30,tzinfo=timezone.utc);rows=[]
    for i in range(count):
        price=5+i*.01
        rows.append({"t":(start+timedelta(minutes=i)).isoformat().replace("+00:00","Z"),"o":price,"h":price+.02,"l":price-.02,"c":price+.01,"v":100000,"n":100,"vw":price})
    return rows


class MarketRadarBacktestTests(unittest.TestCase):
    def setUp(self):self.engine=MarketRadarBacktest(FakeRedis(),object())

    def test_indicator_formulas(self):
        rows=sample_bars(40)
        self.assertGreater(self.engine.vwap(rows),5)
        self.assertGreater(self.engine.atr(rows),0)
        self.assertTrue(self.engine.obv_rising(rows))
        self.assertAlmostEqual(self.engine.volume_accel(rows),1.0)

    def test_missing_feature_score_is_bracketed(self):
        lower,_=self.engine.score_variant(None,"technical_lower",8,4,10,9,True,True,True,0,5)
        upper,_=self.engine.score_variant(None,"technical_upper",8,4,10,9,True,True,True,0,5)
        self.assertGreater(upper,lower);self.assertGreaterEqual(lower,86)

    def test_trade_simulation_is_stop_first_inside_ambiguous_minute(self):
        status,result=self.engine.simulate_full_t1(10,[{"o":10,"h":12,"l":8,"c":11}],{"stop":9,"t1":11.5})
        self.assertEqual(status,"stop");self.assertAlmostEqual(result,-10)

    def test_replay_enters_on_next_bar_and_separates_holdout(self):
        rows=sample_bars(150)
        self.engine.vwap=lambda history:float(history[-1]["c"])-.1
        self.engine.rvol=lambda history:8.0
        self.engine.atr=lambda history,period=14:float(history[-1]["c"])*.05
        self.engine.volume_accel=lambda history:4.0
        self.engine.obv_rising=lambda history:True
        self.engine.trend_15m=lambda history:True
        self.engine.resistance=lambda history:{"resistance":float(history[-1]["c"])-.01,"distance_pct":0,"breakout":True,"touches":3}
        self.engine.entry_quality=lambda history,resistance:("allow",[])
        result=self.engine.replay("2026-08-28","AAA",rows);signal=result["signals"]["technical_lower"]["86"]
        self.assertIsNotNone(signal);self.assertGreater(signal["entry_ts"],signal["ts"]);self.assertEqual(result["partition"],"holdout")

    def test_future_price_does_not_change_signal_timestamp(self):
        rows=sample_bars(150)
        for name,value in (("vwap",lambda h:float(h[-1]["c"])-.1),("rvol",lambda h:8.0),("atr",lambda h,period=14:float(h[-1]["c"])*.05),("volume_accel",lambda h:4.0),("obv_rising",lambda h:True),("trend_15m",lambda h:True),("resistance",lambda h:{"resistance":float(h[-1]["c"])-.01,"distance_pct":0,"breakout":True,"touches":3}),("entry_quality",lambda h,r:("allow",[]))):setattr(self.engine,name,value)
        first=self.engine.replay("2026-08-28","AAA",rows)["signals"]["technical_lower"]["86"]
        changed=[dict(x) for x in rows];changed[-1].update(h=99,c=99)
        second=self.engine.replay("2026-08-28","AAA",changed)["signals"]["technical_lower"]["86"]
        self.assertEqual(first["ts"],second["ts"]);self.assertEqual(first["entry_price"],second["entry_price"])

    def test_stored_diagnostic_separates_stop_recovery_without_holdout_tuning(self):
        def signal(status,mfe,trade_return,rvol=4.5):
            return {"entry_price":10,"t1":11,"trade_status":status,"trade_return_pct":trade_return,"mfe_pct":mfe,"mae_pct":-3,"rvol":rvol,"volume_acceleration":2.2,"atr_pct":2,"breakout":True,"obv_rising":True,"trend_15m_ok":True,"resistance_distance_pct":-.5}
        rows=[
            {"session":"2026-08-01","symbol":"AAA","partition":"development","signals":{"technical_lower":{},"technical_upper":{"live_policy":signal("stop",12,-2)}}},
            {"session":"2026-08-02","symbol":"BBB","partition":"development","signals":{"technical_lower":{},"technical_upper":{"live_policy":signal("t1",15,4)}}},
            {"session":"2026-08-20","symbol":"CCC","partition":"holdout","signals":{"technical_lower":{},"technical_upper":{"live_policy":signal("stop",5,-2)}}},
            {"session":"2026-08-21","symbol":"DDD","partition":"holdout","signals":{"technical_lower":{},"technical_upper":{"live_policy":signal("t1",14,4)}}},
        ]
        self.engine.iter_results=lambda:iter(rows);result=self.engine.stored_diagnostic_report();diag=result["stop_diagnostics"]["technical_upper"]["live_policy"]
        self.assertEqual(diag["development"]["stopped_but_window_reached_t1_level"],1)
        self.assertEqual(diag["holdout"]["stopped_and_never_reached_t1_level"],1)
        self.assertTrue(result["conclusion"]["holdout_was_not_used_for_rule_ranking"])
        self.assertFalse(result["methodology"]["entry_quality_ab_test_available"])
        self.assertIsNotNone(self.engine.stored_diagnostic_result())


if __name__=="__main__":unittest.main()
