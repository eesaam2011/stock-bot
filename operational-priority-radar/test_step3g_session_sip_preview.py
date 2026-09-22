"""Step3G: read-only session REST/SIP preview and opt-in startup bridge."""
import unittest
from datetime import timedelta
from unittest.mock import patch
from recovery_session_preview import preview_session_overlap
from recovery_sip_overlap import SIPOverlapUnsafe
from test_step2z_recovery_preview import inputs,START
from test_step2y_sip_overlap import NOW,FETCH,bar
from test_step2x_signal_replay import RecordingBase
from production_recovery import ProductionStartupRecovery

def preview(events,c,**kw):
    return preview_session_overlap(
        events,c,session="2026-09-22",epoch=7,symbols=["A"],
        session_start=START,session_end=NOW,requested_start=START,
        as_of=NOW,base_factory=RecordingBase,
        early_score=lambda rows,end:(.9,.9),**kw)

class TestSessionSIPPreview(unittest.TestCase):
    def setUp(self):RecordingBase.seen=[]
    def test_sip_bar_contributes_to_session_b_without_ack(self):
        events,c=inputs()
        signals,a=preview(events,c)
        self.assertEqual([(s.kind,s.bar_end_ts) for s in signals],
            [("B",START+timedelta(minutes=3)),
             ("E",START+timedelta(minutes=5))])
        self.assertTrue(all(s.decision_available_ts==NOW for s in signals))
        self.assertEqual(a["session_observed_B"],1)
        self.assertEqual(a["session_observed_E"],1)
        self.assertEqual(a["overlap"]["unmatched_sip_1m_bars"],1)
        self.assertEqual(a["sip_trades_observed_not_reconciled"],1)
        self.assertEqual(a["sip_statuses_observed_not_reconciled"],1)
        self.assertEqual(c.snapshot()["buffered"],3)
        self.assertEqual(c.snapshot()["acked_upto"],0)
        self.assertEqual(c.phase,c.DRAINING)
        self.assertEqual(a["canonical_writes"],0)
        self.assertFalse(a["direct_handoff_authorized"])
        self.assertFalse(a["first_of_session_proven"])
    def test_previously_acked_capture_prefix_rejected(self):
        events,c=inputs();c.ack_batch(7,1)
        with self.assertRaisesRegex(SIPOverlapUnsafe,"SESSION_PREVIEW_PRIOR_ACK"):
            preview(events,c)
    def test_all_captured_messages_acked_still_rejected(self):
        events,c=inputs();c.ack_batch(7,3)
        self.assertEqual(c.snapshot()["buffered"],0)
        with self.assertRaisesRegex(SIPOverlapUnsafe,"SESSION_PREVIEW_PRIOR_ACK"):
            preview(events,c)
    def test_concurrent_append_during_session_scoring_fails_closed(self):
        events,c=inputs()
        def scoring(rows,end):
            if c.snapshot()["buffered"]==3:
                c.ingest(7,{"T":"t","S":"A","t":"2026-09-22T16:00:20Z","p":10.4},
                         received_at=FETCH+timedelta(seconds=20))
            return .9,.9
        with self.assertRaisesRegex(SIPOverlapUnsafe,
                                    "SESSION_PREVIEW_CAPTURE_CHANGED"):
            preview_session_overlap(
                events,c,session="2026-09-22",epoch=7,symbols=["A"],
                session_start=START,session_end=NOW,requested_start=START,
                as_of=NOW,base_factory=RecordingBase,early_score=scoring)
        self.assertEqual(c.snapshot()["buffered"],4)
    def test_mid_preview_ack_fails_closed(self):
        events,c=inputs()
        def scoring(rows,end):
            if c.snapshot()["acked_upto"]==0:c.ack_batch(7,1)
            return .9,.9
        with self.assertRaisesRegex(SIPOverlapUnsafe,
                                    "SESSION_PREVIEW_CAPTURE_CHANGED"):
            preview_session_overlap(
                events,c,session="2026-09-22",epoch=7,symbols=["A"],
                session_start=START,session_end=NOW,requested_start=START,
                as_of=NOW,base_factory=RecordingBase,early_score=scoring)
    def test_out_of_batch_symbol_rejected_not_silently_ignored(self):
        events,c=inputs()
        c.ingest(7,{"T":"t","S":"B","t":"2026-09-22T16:00:20Z","p":11},
                 received_at=FETCH+timedelta(seconds=20))
        with self.assertRaisesRegex(SIPOverlapUnsafe,
                                    "SESSION_PREVIEW_OUT_OF_BATCH_SYMBOL"):
            preview(events,c)
    def test_conflicting_rest_sip_bar_fails_without_ack(self):
        events,c=inputs()
        first=events[0]
        c.ingest(7,bar(first.start.isoformat(),c=12),
                 received_at=FETCH+timedelta(seconds=30))
        with self.assertRaisesRegex(SIPOverlapUnsafe,
                                    "REST_SIP_BAR_CONFLICT"):
            preview(events,c)
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_407_invalidation_fails_closed(self):
        events,c=inputs();c.invalidate("407")
        with self.assertRaisesRegex(SIPOverlapUnsafe,"CAPTURE_NOT_DRAINING"):
            preview(events,c)
    def test_bounded_symbol_batch_and_max_events(self):
        events,c=inputs()
        with self.assertRaisesRegex(SIPOverlapUnsafe,"SESSION_PREVIEW_INVALID_BATCH"):
            preview_session_overlap(
                events,c,session="2026-09-22",epoch=7,
                symbols=["A"]*81,session_start=START,session_end=NOW,
                requested_start=START,as_of=NOW)
        with self.assertRaisesRegex(SIPOverlapUnsafe,"MERGE_BOUNDED_LIMIT"):
            preview(events,c,max_events=len(events)+1)
    def test_empty_native_history_fails_closed(self):
        _,c=inputs()
        with self.assertRaisesRegex(SIPOverlapUnsafe,"SESSION_PREVIEW_INVALID_BATCH"):
            preview((),c)
    def test_untrusted_status_is_counted_but_not_applied(self):
        events,c=inputs()
        _,a=preview(events,c)
        self.assertEqual(a["sip_statuses_observed_not_reconciled"],1)
        self.assertFalse(a["halt_coverage_proven"])
        self.assertFalse(a["active_trades_reconciled"])
        self.assertFalse(a["sip_continuity_proven"])

class TestStartupSessionSIPBridge(unittest.TestCase):
    class Reader:
        def active_trades(self):return []
        def earliest_decision_anchor(self,session):return None
    class REST:
        def __init__(self,events):self.events=events;self.calls=[]
        def native_recovery_batch_audited(self,symbols,start,end,**kw):
            self.calls.append((tuple(symbols),start,end))
            one={s:[] for s in symbols};five={s:[] for s in symbols}
            for e in self.events:
                (one if e.timeframe=="1m" else five)[e.symbol].append(e.bar)
            return one,five,{"both_api_page_chains_exhausted":True,
                             "native_1m":{"pages":1},"native_5m":{"pages":1}}
        def native_recovery_batch(self,*a,**kw):
            raise AssertionError("audited REST required")
    def test_external_drain_one_audited_batch_still_no_trust(self):
        events,c=inputs();rest=self.REST(events)
        rec=ProductionStartupRecovery(self.Reader(),rest,None,"2026-09-22",
            ["A"],now_fn=lambda:NOW,audit_session_signals=True,
            session_start=START,audit_session_overlap=True,
            sip_capture=c,sip_epoch=7)
        result=rec.run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        a=result["fetch_audit"]
        self.assertEqual(a["session_sip_overlap_audited_batches"],1)
        self.assertEqual(a["session_sip_overlap_new_bars"],1)
        self.assertEqual(a["session_sip_trades_not_reconciled"],1)
        self.assertEqual(a["session_sip_statuses_not_reconciled"],1)
        self.assertTrue(a["api_page_chains_exhausted"])
        self.assertFalse(a["full_session_coverage_proven"])
        self.assertFalse(rec.continuity_verified(7))
        self.assertEqual(c.snapshot()["acked_upto"],0)
        self.assertEqual(c.phase,c.DRAINING)
    def test_external_drain_is_required_not_auto_started(self):
        events,c=inputs();c.start(8);rest=self.REST(events)
        rec=ProductionStartupRecovery(self.Reader(),rest,None,"2026-09-22",
            ["A"],now_fn=lambda:NOW,audit_session_signals=True,
            session_start=START,audit_session_overlap=True,
            sip_capture=c,sip_epoch=8)
        result=rec.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(rest.calls,[])
        self.assertEqual(c.phase,c.CAPTURING)
    def test_large_universe_refused_before_rest_fetch(self):
        events,c=inputs();rest=self.REST(events)
        rec=ProductionStartupRecovery(self.Reader(),rest,None,"2026-09-22",
            ["A"+str(i) for i in range(81)],now_fn=lambda:NOW,
            audit_session_signals=True,session_start=START,
            audit_session_overlap=True,sip_capture=c,sip_epoch=7)
        result=rec.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(rest.calls,[])
    def test_wrong_capture_epoch_refused_before_rest(self):
        events,c=inputs();rest=self.REST(events)
        rec=ProductionStartupRecovery(self.Reader(),rest,None,"2026-09-22",
            ["A"],now_fn=lambda:NOW,audit_session_signals=True,
            session_start=START,audit_session_overlap=True,
            sip_capture=c,sip_epoch=8)
        result=rec.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(rest.calls,[])
    def test_overlap_requires_session_audit_opt_in(self):
        events,c=inputs();rest=self.REST(events)
        rec=ProductionStartupRecovery(self.Reader(),rest,None,"2026-09-22",
            ["A"],now_fn=lambda:NOW,audit_session_signals=False,
            session_start=START,audit_session_overlap=True,
            sip_capture=c,sip_epoch=7)
        result=rec.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(rest.calls,[])

if __name__=="__main__":unittest.main()
