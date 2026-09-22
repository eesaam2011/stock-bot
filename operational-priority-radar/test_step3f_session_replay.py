"""Step 3F: session-scoped E/B audit, no retroactive entry or coverage claims."""
import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from recovery_chronology import plan_native_batch,ChronologyUnsafe
from recovery_session_replay import reconstruct_session_signals,EARLY_WARMUP_MINUTES
from test_step2x_signal_replay import RecordingBase
from early_core_score import ARTIFACT

UTC=timezone.utc
SESSION=datetime(2026,9,22,13,30,tzinfo=UTC)
FETCH=SESSION+timedelta(minutes=15)
REQUEST=SESSION-timedelta(minutes=EARLY_WARMUP_MINUTES)
def bar(ts,tf="1m"):
    d={"t":ts.isoformat(),"o":10,"h":11,"l":9,"c":10,"v":100,"n":10}
    if tf=="5m":d["_timeframe"]="native_5Min"
    return d
def history():
    one=[bar(SESSION-timedelta(minutes=10)+timedelta(minutes=i))
         for i in range(25)]
    five=[bar(SESSION-timedelta(minutes=15)+timedelta(minutes=i*5),"5m")
          for i in range(6)]
    return plan_native_batch({"A":one},{"A":five},["A"],
        window_start=REQUEST,window_end=FETCH,recovered_at=FETCH)[0]
def replay(events=None,**kw):
    return reconstruct_session_signals(
        history() if events is None else events,"2026-09-22",
        session_start=SESSION,session_end=FETCH,
        requested_start=REQUEST,recovered_at=FETCH,
        base_factory=RecordingBase,early_score=lambda rows,end:(.9,.9),**kw)

class TestSessionScopedReplay(unittest.TestCase):
    def setUp(self):RecordingBase.seen=[]
    def test_pre_session_crossing_not_mistaken_for_session_e(self):
        signals,a=replay()
        e=next(s for s in signals if s.kind=="E")
        self.assertEqual(e.bar_start_ts,SESSION)
        self.assertEqual(e.bar_end_ts,SESSION+timedelta(minutes=5))
        self.assertEqual(e.decision_available_ts,FETCH)
        self.assertEqual(a["pre_session_native5_observations"],3)
        self.assertTrue(a["warmup_window_requested"])
        self.assertFalse(a["first_of_session_proven"])
        self.assertFalse(a["full_session_coverage_proven"])
        self.assertEqual(a["canonical_writes"],0)
        self.assertFalse(a["direct_handoff_authorized"])
    def test_b_history_resets_at_session_start(self):
        signals,a=replay()
        b=next(s for s in signals if s.kind=="B")
        self.assertEqual(b.bar_start_ts,SESSION+timedelta(minutes=2))
        self.assertEqual(len(RecordingBase.seen),3)
        self.assertTrue(all(datetime.fromisoformat(x)>=SESSION for x in RecordingBase.seen))
        self.assertEqual(a["session_observed_B"],1)
    def test_order_independent_and_frozen_threshold_unchanged(self):
        p=history()
        first,a=replay(p)
        second,b=replay(tuple(reversed(p)))
        self.assertEqual(first,second)
        self.assertEqual(a,b)
        self.assertEqual(a["frozen_early_core_threshold"],ARTIFACT["threshold"])
    def test_no_5m_synthesis_from_1m(self):
        p=tuple(x for x in history() if x.timeframe=="1m")
        signals,a=replay(p)
        self.assertEqual([s.kind for s in signals],["B"])
        self.assertEqual(a["session_observed_E"],0)
    def test_synthetic_5m_fails_closed(self):
        p=list(history())
        e=next(x for x in p if x.timeframe=="5m")
        p[p.index(e)]=replace(e,bar={k:v for k,v in e.bar.items()
                                      if k!="_timeframe"})
        with self.assertRaisesRegex(ChronologyUnsafe,"SESSION_REPLAY_SYNTHETIC_5M"):
            replay(tuple(p))
    def test_conflicting_duplicate_rejected(self):
        p=history()
        with self.assertRaisesRegex(ChronologyUnsafe,"SESSION_REPLAY_DUPLICATE_EVENT"):
            replay(p+(p[0],))
    def test_wrong_epoch_recovery_time_rejected(self):
        p=list(history());p[0]=replace(p[0],recovered_at=FETCH-timedelta(seconds=1))
        with self.assertRaisesRegex(ChronologyUnsafe,"SESSION_REPLAY_EVENT_OUTSIDE_FETCH"):
            replay(tuple(p))
    def test_malformed_bar_rejected(self):
        p=list(history());p[0]=replace(p[0],bar={**p[0].bar,"c":float("nan")})
        with self.assertRaisesRegex(ChronologyUnsafe,"INCONSISTENT_OHLCV"):
            replay(tuple(p))
    def test_window_rejects_future_end_and_unbounded_history(self):
        with self.assertRaisesRegex(ChronologyUnsafe,"SESSION_REPLAY_INVALID_WINDOW_OR_LIMIT"):
            replay(max_events=2)
        p=list(history());p[0]=replace(p[0],end=FETCH+timedelta(minutes=1))
        with self.assertRaisesRegex(ChronologyUnsafe,"SESSION_REPLAY_EVENT_OUTSIDE_FETCH"):
            replay(tuple(p))
    def test_partial_warmup_is_reported_not_silently_proven(self):
        p=tuple(x for x in history() if x.start>=SESSION)
        _,a=reconstruct_session_signals(
            p,"2026-09-22",session_start=SESSION,session_end=FETCH,
            requested_start=SESSION,recovered_at=FETCH,
            base_factory=RecordingBase,early_score=lambda rows,end:(.9,.9))
        self.assertFalse(a["warmup_window_requested"])
        self.assertEqual(a["pre_session_native5_observations"],0)
        self.assertFalse(a["first_of_session_proven"])
    def test_real_frozen_scoring_runs_without_injected_threshold(self):
        signals,a=reconstruct_session_signals(
            history(),"2026-09-22",session_start=SESSION,session_end=FETCH,
            requested_start=REQUEST,recovered_at=FETCH)
        self.assertTrue(all(s.decision_available_ts==FETCH for s in signals))
        self.assertEqual(a["frozen_early_core_threshold"],ARTIFACT["threshold"])
        self.assertFalse(a["sip_continuity_proven"])

class TestStartupSessionAudit(unittest.TestCase):
    class Reader:
        def active_trades(self):return []
        def earliest_decision_anchor(self,session):return None
    class REST:
        def __init__(self):self.calls=[]
        def native_recovery_batch(self,symbols,start,end,**kw):
            self.calls.append((tuple(symbols),start,end,kw))
            return ({sym:[bar(SESSION+timedelta(minutes=i)) for i in range(25)
                          if SESSION+timedelta(minutes=i)<end]
                     for sym in symbols},
                    {sym:[bar(SESSION-timedelta(minutes=5),"5m"),
                              bar(SESSION,"5m")]
                     for sym in symbols})
    def test_explicit_session_start_fetches_warmup_but_no_trust(self):
        from production_recovery import ProductionStartupRecovery
        rest=self.REST()
        rec=ProductionStartupRecovery(self.Reader(),rest,None,"2026-09-22",
            ["A"],now_fn=lambda:SESSION+timedelta(minutes=30),
            audit_session_signals=True,session_start=SESSION)
        result=rec.run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        a=result["fetch_audit"]
        self.assertEqual(rest.calls[0][1],REQUEST)
        self.assertEqual(a["session_audited_batches"],1)
        self.assertTrue(a["session_warmup_window_requested"])
        self.assertFalse(a["full_session_coverage_proven"])
        self.assertFalse(result["gap_recovered"])
        self.assertFalse(rec.continuity_verified(7))
    def test_session_audit_without_explicit_start_fails_closed(self):
        from production_recovery import ProductionStartupRecovery
        rest=self.REST()
        rec=ProductionStartupRecovery(self.Reader(),rest,None,"2026-09-22",
            ["A"],now_fn=lambda:FETCH,audit_session_signals=True)
        result=rec.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(rest.calls,[])
        self.assertFalse(rec.ready_after_stream())
    def test_session_audit_uses_bounded_80_symbol_batches(self):
        from production_recovery import ProductionStartupRecovery
        rest=self.REST()
        syms=["A"+str(i) for i in range(161)]
        rec=ProductionStartupRecovery(self.Reader(),rest,None,"2026-09-22",
            syms,now_fn=lambda:SESSION+timedelta(minutes=30),
            audit_session_signals=True,session_start=SESSION)
        result=rec.run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        self.assertEqual([len(x[0]) for x in rest.calls],[80,80,1])
        self.assertEqual(result["fetch_audit"]["session_audited_batches"],3)
        self.assertFalse(result["fetch_audit"]["api_page_chains_exhausted"])

if __name__=="__main__":unittest.main()
