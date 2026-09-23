"""Step3K: moving REST→SIP cutoff and bounded append-only read-only retry."""
import unittest
from datetime import timedelta
from unittest.mock import patch
from recovery_stable_preview import stable_session_preview,StablePreviewUnsafe
from recovery_session_preview import preview_session_overlap as real_preview
from test_step3j_auto_capture_drain import capturing,HookedREST
from test_step2z_recovery_preview import START
from test_step2y_sip_overlap import NOW
import test_step3g_session_sip_preview as bridge_tests
from production_recovery import ProductionStartupRecovery

def trade(t,received):
    return {"T":"t","S":"A","t":t.isoformat(),"p":10.4},received

class Clock:
    def __init__(self,*times):self.times=iter(times);self.last=None
    def __call__(self):
        try:self.last=next(self.times)
        except StopIteration:pass
        return self.last

class TestStablePreview(unittest.TestCase):
    def test_one_append_after_preview_retries_and_retains_all(self):
        events,c=capturing();c.begin_drain(7)
        appended=[False]
        def first_append(*args,**kwargs):
            out=real_preview(*args,**kwargs)
            if not appended[0]:
                appended[0]=True
                msg,received=trade(NOW+timedelta(seconds=1),
                                   NOW+timedelta(seconds=2))
                c.ingest(7,msg,received_at=received)
            return out
        _,audit=stable_session_preview(
            events,c,session="2026-09-22",epoch=7,symbols=["A"],
            session_start=START,requested_start=START,rest_cutoff=NOW,
            now_fn=Clock(NOW,NOW+timedelta(seconds=10)),
            preview_fn=first_append,max_attempts=3)
        self.assertEqual(audit["stable_preview_attempts"],2)
        self.assertEqual(audit["stable_preview_append_retries"],1)
        self.assertEqual(audit["overlap"]["sip_trades"],2)
        self.assertEqual(c.snapshot()["buffered"],4)
        self.assertEqual(c.snapshot()["acked_upto"],0)
        self.assertFalse(audit["direct_handoff_authorized"])
        self.assertFalse(audit["post_rest_sip_coverage_proven"])
    def test_persistent_append_exhausts_bounded_budget(self):
        events,c=capturing();c.begin_drain(7)
        count=[0]
        def always_append(*args,**kwargs):
            out=real_preview(*args,**kwargs)
            count[0]+=1
            msg,received=trade(NOW+timedelta(seconds=count[0]),
                                  NOW+timedelta(seconds=count[0]+1))
            c.ingest(7,msg,received_at=received)
            return out
        with self.assertRaisesRegex(StablePreviewUnsafe,
                                    "RETRY_BUDGET_EXHAUSTED"):
            stable_session_preview(
                events,c,session="2026-09-22",epoch=7,symbols=["A"],
                session_start=START,requested_start=START,rest_cutoff=NOW,
                now_fn=Clock(NOW+timedelta(seconds=10)),
                preview_fn=always_append,max_attempts=3)
        self.assertEqual(count[0],3)
        self.assertEqual(c.snapshot()["buffered"],6)
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_407_after_preview_is_fatal_not_retried(self):
        events,c=capturing();c.begin_drain(7)
        calls=[0]
        def disconnect(*args,**kwargs):
            calls[0]+=1
            out=real_preview(*args,**kwargs)
            c.invalidate("407")
            return out
        with self.assertRaisesRegex(StablePreviewUnsafe,"EPOCH_CHANGED"):
            stable_session_preview(
                events,c,session="2026-09-22",epoch=7,symbols=["A"],
                session_start=START,requested_start=START,rest_cutoff=NOW,
                now_fn=lambda:NOW,preview_fn=disconnect,max_attempts=3)
        self.assertEqual(calls[0],1)
        self.assertEqual(c.phase,c.INVALID)
    def test_ack_during_preview_is_fatal_not_retried(self):
        events,c=capturing();c.begin_drain(7)
        def ack(*args,**kwargs):
            out=real_preview(*args,**kwargs)
            c.ack_batch(7,1)
            return out
        with self.assertRaisesRegex(StablePreviewUnsafe,"INVALID_EPOCH|EPOCH_CHANGED"):
            stable_session_preview(
                events,c,session="2026-09-22",epoch=7,symbols=["A"],
                session_start=START,requested_start=START,rest_cutoff=NOW,
                now_fn=lambda:NOW,preview_fn=ack,max_attempts=3)
        self.assertEqual(c.snapshot()["acked_upto"],1)
    def test_clock_regression_fails_before_preview(self):
        events,c=capturing();c.begin_drain(7)
        with self.assertRaisesRegex(StablePreviewUnsafe,"CLOCK_REGRESSION"):
            stable_session_preview(
                events,c,session="2026-09-22",epoch=7,symbols=["A"],
                session_start=START,requested_start=START,rest_cutoff=NOW,
                now_fn=lambda:NOW-timedelta(seconds=1))
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_invalid_retry_count_refused(self):
        events,c=capturing();c.begin_drain(7)
        for n in (0,4,True):
            with self.assertRaisesRegex(StablePreviewUnsafe,"INVALID_ATTEMPTS"):
                stable_session_preview(
                    events,c,session="2026-09-22",epoch=7,symbols=["A"],
                    session_start=START,requested_start=START,
                    rest_cutoff=NOW,now_fn=lambda:NOW,max_attempts=n)
    def test_cancel_between_attempts_fails_without_ack(self):
        events,c=capturing();c.begin_drain(7)
        calls=[0]
        def check():
            calls[0]+=1
            if calls[0]>=2:raise RuntimeError("RECOVERY_CANCELLED")
        def append(*args,**kwargs):
            out=real_preview(*args,**kwargs)
            msg,received=trade(NOW+timedelta(seconds=1),
                                  NOW+timedelta(seconds=2))
            c.ingest(7,msg,received_at=received)
            return out
        with self.assertRaisesRegex(RuntimeError,"RECOVERY_CANCELLED"):
            stable_session_preview(
                events,c,session="2026-09-22",epoch=7,symbols=["A"],
                session_start=START,requested_start=START,
                rest_cutoff=NOW,now_fn=lambda:NOW+timedelta(seconds=10),
                preview_fn=append,cancel_check=check)
        self.assertEqual(c.snapshot()["acked_upto"],0)

class TestStartupStablePreview(unittest.TestCase):
    def test_post_rest_new_receive_timestamp_uses_fresh_clock(self):
        events,c=capturing()
        later=NOW+timedelta(seconds=10)
        def append():
            msg,received=trade(NOW+timedelta(seconds=1),
                                  NOW+timedelta(seconds=2))
            c.ingest(7,msg,received_at=received)
        rec=ProductionStartupRecovery(
            bridge_tests.TestStartupSessionSIPBridge.Reader(),HookedREST(events,append),
            None,"2026-09-22",["A"],now_fn=Clock(NOW,later),
            audit_session_signals=True,session_start=START,
            audit_session_overlap=True,sip_capture=c,sip_epoch=7,
            auto_begin_capture_drain=True)
        result=rec.run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        a=result["fetch_audit"]
        self.assertEqual(a["session_sip_trades_not_reconciled"],2)
        self.assertEqual(a["session_stable_preview_attempts"],1)
        self.assertFalse(a["post_rest_sip_coverage_proven"])
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_startup_opt_in_retry_recovers_post_preview_append(self):
        events,c=capturing()
        clock=Clock(NOW,NOW,NOW+timedelta(seconds=10))
        calls=[0]
        def append_once(*args,**kwargs):
            out=real_preview(*args,**kwargs)
            if calls[0]==0:
                msg,received=trade(NOW+timedelta(seconds=1),
                                   NOW+timedelta(seconds=2))
                c.ingest(7,msg,received_at=received)
            calls[0]+=1
            return out
        rec=ProductionStartupRecovery(
            bridge_tests.TestStartupSessionSIPBridge.Reader(),HookedREST(events),None,
            "2026-09-22",["A"],now_fn=clock,
            audit_session_signals=True,session_start=START,
            audit_session_overlap=True,sip_capture=c,sip_epoch=7,
            auto_begin_capture_drain=True,stable_preview_max_attempts=3)
        with patch("production_recovery.preview_session_overlap",
                   side_effect=append_once):
            result=rec.run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        a=result["fetch_audit"]
        self.assertEqual(a["session_stable_preview_attempts"],2)
        self.assertEqual(a["session_stable_preview_append_retries"],1)
        self.assertEqual(a["session_sip_trades_not_reconciled"],2)
        self.assertEqual(c.snapshot()["acked_upto"],0)
        self.assertFalse(rec.continuity_verified(7))
    def test_retry_policy_without_overlap_fails_closed(self):
        events,c=capturing()
        rec=ProductionStartupRecovery(
            bridge_tests.TestStartupSessionSIPBridge.Reader(),HookedREST(events),None,
            "2026-09-22",["A"],now_fn=lambda:NOW,
            stable_preview_max_attempts=2)
        result=rec.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(c.phase,c.CAPTURING)

if __name__=="__main__":unittest.main()
