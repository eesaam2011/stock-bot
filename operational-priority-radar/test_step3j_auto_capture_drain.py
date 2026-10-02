"""Step3J: bounded post-REST CAPTURING→DRAINING, no ACK/DIRECT/trust."""
import unittest
from datetime import datetime,timedelta
from unittest.mock import patch
from sip_epoch_capture import BoundedEpochCapture,EpochCaptureOverflow
from test_step2z_recovery_preview import inputs,START
from test_step2y_sip_overlap import NOW,FETCH
from test_step3g_session_sip_preview import TestStartupSessionSIPBridge
from production_recovery import ProductionStartupRecovery
from recovery_session_preview import preview_session_overlap as real_preview

class HookedREST(TestStartupSessionSIPBridge.REST):
    def __init__(self,events,hook=None):
        super().__init__(events);self.hook=hook
    def native_recovery_batch_audited(self,*args,**kwargs):
        if self.hook:self.hook()
        return super().native_recovery_batch_audited(*args,**kwargs)

def capturing(*,max_messages=20):
    events,original=inputs()
    copied=original.peek_batch(7)
    c=BoundedEpochCapture(max_messages=max_messages,max_bytes=4096)
    c.start(7)
    for item in copied:
        c.ingest(7,item.payload,
                 received_at=datetime.fromisoformat(item.received_at))
    return events,c

def startup(events,c,rest=None,**opts):
    return ProductionStartupRecovery(
        TestStartupSessionSIPBridge.Reader(),
        rest or HookedREST(events),None,"2026-09-22",["A"],
        now_fn=lambda:NOW,audit_session_signals=True,session_start=START,
        audit_session_overlap=True,sip_capture=c,sip_epoch=7,
        auto_begin_capture_drain=True,**opts)

class TestPostRESTAutoDrain(unittest.TestCase):
    def test_opt_in_starts_drain_after_rest_without_ack_or_direct(self):
        events,c=capturing()
        rest=HookedREST(events)
        result=startup(events,c,rest).run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        self.assertEqual(len(rest.calls),1)
        self.assertEqual(c.phase,c.DRAINING)
        self.assertEqual(c.snapshot()["buffered"],3)
        self.assertEqual(c.snapshot()["acked_upto"],0)
        a=result["fetch_audit"]
        self.assertTrue(a["session_auto_begin_capture_drain"])
        self.assertFalse(a["session_capture_acknowledged"])
        self.assertFalse(a["session_direct_handoff_authorized"])
        self.assertFalse(a["full_session_coverage_proven"])
    def test_sip_message_arriving_during_rest_is_preserved(self):
        events,c=capturing()
        def append():
            self.assertEqual(c.phase,c.CAPTURING)
            c.ingest(7,{"T":"t","S":"A",
                        "t":"2026-09-22T16:00:18Z","p":10.4},
                     received_at=FETCH+timedelta(seconds=18))
        result=startup(events,c,HookedREST(events,append)).run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        self.assertEqual(result["fetch_audit"]["session_sip_trades_not_reconciled"],2)
        self.assertEqual(c.snapshot()["buffered"],4)
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_407_during_rest_invalidates_before_drain(self):
        events,c=capturing()
        result=startup(events,c,HookedREST(events,lambda:c.invalidate("407"))).run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(c.phase,c.INVALID)
        self.assertEqual(c.snapshot()["buffered"],0)
    def test_capture_overflow_during_rest_fails_closed(self):
        events,c=capturing(max_messages=3)
        def overflow():
            c.ingest(7,{"T":"t","S":"A",
                        "t":"2026-09-22T16:00:18Z","p":10.4},
                     received_at=FETCH+timedelta(seconds=18))
        result=startup(events,c,HookedREST(events,overflow)).run()
        self.assertEqual(result["reason"],"EpochCaptureOverflow")
        self.assertEqual(c.phase,c.INVALID)
    def test_rest_exception_keeps_capture_capturing_and_unacked(self):
        events,c=capturing()
        def fail():raise RuntimeError("REST pagination failed")
        result=startup(events,c,HookedREST(events,fail)).run()
        self.assertEqual(result["reason"],"RuntimeError")
        self.assertEqual(c.phase,c.CAPTURING)
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_already_draining_refused_when_auto_mode_selected(self):
        events,c=inputs()
        rest=HookedREST(events)
        result=startup(events,c,rest).run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(rest.calls,[])
        self.assertEqual(c.phase,c.DRAINING)
    def test_auto_drain_without_overlap_refused(self):
        events,c=capturing()
        r=ProductionStartupRecovery(
            TestStartupSessionSIPBridge.Reader(),HookedREST(events),None,
            "2026-09-22",["A"],now_fn=lambda:NOW,
            auto_begin_capture_drain=True)
        result=r.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(c.phase,c.CAPTURING)
    def test_sip_append_immediately_after_preview_return_is_detected(self):
        events,c=capturing()
        def preview_then_append(*args,**kwargs):
            signals,audit=real_preview(*args,**kwargs)
            c.ingest(7,{"T":"t","S":"A",
                        "t":"2026-09-22T16:00:18Z","p":10.4},
                     received_at=FETCH+timedelta(seconds=18))
            return signals,audit
        with patch("production_recovery.preview_session_overlap",
                   side_effect=preview_then_append):
            result=startup(events,c).run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(c.phase,c.DRAINING)
        self.assertEqual(c.snapshot()["buffered"],4)
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_407_immediately_after_preview_return_is_detected(self):
        events,c=capturing()
        def preview_then_disconnect(*args,**kwargs):
            signals,audit=real_preview(*args,**kwargs)
            c.invalidate("407")
            return signals,audit
        with patch("production_recovery.preview_session_overlap",
                   side_effect=preview_then_disconnect):
            result=startup(events,c).run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertEqual(c.phase,c.INVALID)
    def test_recovery_cancellation_before_rest_does_not_begin_drain(self):
        events,c=capturing()
        r=startup(events,c)
        r.cancel()
        with self.assertRaisesRegex(Exception,"RECOVERY_CANCELLED"):
            r.run()
        self.assertEqual(c.phase,c.CAPTURING)
        self.assertEqual(c.snapshot()["acked_upto"],0)

if __name__=="__main__":unittest.main()
