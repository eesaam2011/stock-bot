"""Step 2W: native REST chronology planning is read-only and fail-closed."""
import unittest
from datetime import datetime,timezone,timedelta
from recovery_chronology import plan_native_batch,ChronologyUnsafe
from production_recovery import ProductionStartupRecovery

NOW=datetime(2026,9,22,16,tzinfo=timezone.utc)
START=NOW-timedelta(minutes=10)
def bar(ts,**kw):
    return dict({"t":ts,"o":10,"h":11,"l":9,"c":10,"v":100},**kw)
def rows():
    return ({"A":[bar("2026-09-22T15:55:00Z"),bar("2026-09-22T15:56:00Z")]},
            {"A":[bar("2026-09-22T15:55:00Z",_timeframe="native_5Min")]})
def plan(one=None,five=None,**kw):
    a,b=rows()
    return plan_native_batch(one if one is not None else a,
                             five if five is not None else b,["A"],
                             window_start=START,window_end=NOW,
                             recovered_at=NOW,**kw)

class TestNativeChronology(unittest.TestCase):
    def test_chronological_order_native_provenance_and_no_trust(self):
        events,audit=plan()
        self.assertEqual([(e.timeframe,e.start.minute) for e in events],
                         [("1m",55),("1m",56),("5m",55)])
        self.assertEqual(events[0].decision_available_ts,NOW)
        self.assertEqual((audit["native_1m"],audit["native_5m"]),(2,1))
        self.assertFalse(audit["continuity_proven"])
        self.assertFalse(audit["canonical_replay_completed"])
    def test_order_independent_digest_and_identical_dedup(self):
        a,b=rows();first,au=plan(a,b)
        a["A"]=list(reversed(a["A"]))+[dict(a["A"][0])]
        second,bu=plan(a,b)
        self.assertEqual(first,second)
        self.assertEqual(au["chronological_sha256"],bu["chronological_sha256"])
    def test_conflicting_duplicate_fails(self):
        a,b=rows();a["A"].append({**a["A"][0],"c":10.5})
        with self.assertRaisesRegex(ChronologyUnsafe,"CONFLICTING_DUPLICATE_BAR"):
            plan(a,b)
    def test_synthetic_native5_fails(self):
        a,b=rows();b["A"][0].pop("_timeframe")
        with self.assertRaisesRegex(ChronologyUnsafe,"SYNTHETIC_OR_UNPROVEN_NATIVE5"):
            plan(a,b)
    def test_missing_symbol_response_fails(self):
        a,b=rows();b.clear()
        with self.assertRaisesRegex(ChronologyUnsafe,"INCOMPLETE_OR_EXTRA_SYMBOL_RESPONSE"):
            plan(a,b)
    def test_incomplete_future_bar_fails(self):
        a,b=rows();a["A"].append(bar("2026-09-22T15:59:30Z"))
        with self.assertRaisesRegex(ChronologyUnsafe,"INCOMPLETE_OR_FUTURE_BAR"):
            plan(a,b)
    def test_invalid_ohlcv_fails(self):
        a,b=rows();a["A"][0]["l"]=12
        with self.assertRaisesRegex(ChronologyUnsafe,"INCONSISTENT_OHLCV"):
            plan(a,b)
    def test_bounded_events_fails(self):
        with self.assertRaisesRegex(ChronologyUnsafe,"REPLAY_PLAN_LIMIT"):
            plan(max_events=2)
    def test_empty_lanes_not_coverage_proof(self):
        ev,a=plan({"A":[]},{"A":[]})
        self.assertEqual(ev,())
        self.assertEqual(a["empty_symbol_timeframe_lanes"],2)
        self.assertFalse(a["continuity_proven"])
    def test_observed_gap_is_diagnostic_only(self):
        a,b=rows();a["A"]=[a["A"][0],bar("2026-09-22T15:59:00Z")]
        _,audit=plan(a,b)
        self.assertEqual(audit["observed_interbar_gaps"],1)
        self.assertFalse(audit["continuity_proven"])
    def test_production_recovery_returns_audit_but_never_trust(self):
        class Reader:
            def active_trades(self):return []
            def earliest_decision_anchor(self,session):return None
        class REST:
            def native_recovery_batch(self,symbols,start,end,**kw):
                a,b=rows();return a,b
        rec=ProductionStartupRecovery(Reader(),REST(),None,"2026-09-22",["A"],now_fn=lambda:NOW)
        result=rec.run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        self.assertEqual(result["fetch_audit"]["kind"],"REST_CHRONOLOGY_AUDIT_ONLY")
        self.assertEqual(result["fetch_audit"]["native_1m"],2)
        self.assertEqual(result["fetch_audit"]["native_5m"],1)
        self.assertFalse(result["gap_recovered"])
        self.assertFalse(rec.continuity_verified(1))
    def test_production_recovery_rejects_corrupt_native5(self):
        class Reader:
            def active_trades(self):return []
            def earliest_decision_anchor(self,session):return None
        class REST:
            def native_recovery_batch(self,symbols,start,end,**kw):
                a,b=rows();b["A"][0].pop("_timeframe");return a,b
        rec=ProductionStartupRecovery(Reader(),REST(),None,"2026-09-22",["A"],now_fn=lambda:NOW)
        result=rec.run()
        self.assertFalse(result["gap_recovered"])
        self.assertEqual(result["reason"],"ChronologyUnsafe")
        self.assertFalse(rec.ready_after_stream())

if __name__=="__main__":unittest.main()
