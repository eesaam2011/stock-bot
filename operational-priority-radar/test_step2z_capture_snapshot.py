"""Step 2Z: concurrent capture snapshot remains immutable and fail-closed."""
import threading
import unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from sip_epoch_capture import BoundedEpochCapture,EpochCaptureError
from recovery_sip_overlap import audit_draining_capture,SIPOverlapUnsafe
from test_step2y_sip_overlap import capture,rest,NOW,FETCH,bar

class TestCaptureSnapshot(unittest.TestCase):
    def test_snapshot_is_deep_copy_and_never_acks(self):
        c=capture()
        copied,meta=c.snapshot_prefix(7,2)
        self.assertEqual((meta["first_sequence"],meta["last_sequence"]),(1,2))
        copied[0].payload["c"]=500
        self.assertEqual(c.peek_batch(7,1)[0].payload["c"],10)
        self.assertEqual(c.snapshot()["buffered"],4)
        self.assertTrue(c.prefix_still_valid(7,1,2))
    def test_snapshot_rejects_stale_epoch_and_non_draining(self):
        c=capture()
        with self.assertRaises(EpochCaptureError):c.snapshot_prefix(8)
        c.invalidate("407")
        with self.assertRaises(EpochCaptureError):c.snapshot_prefix(7)
        self.assertFalse(c.prefix_still_valid(7,1,2))
    def test_live_append_during_audit_is_not_silently_claimed_complete(self):
        c=capture()
        from recovery_sip_overlap import merge_native_and_captured as real
        def append_during_audit(native,items,**kw):
            c.ingest(7,{"T":"t","S":"A","t":"2026-09-22T16:01:00Z","p":10.3},
                     received_at=NOW-timedelta(seconds=1))
            return real(native,items,**kw)
        with patch("recovery_sip_overlap.merge_native_and_captured",
                   side_effect=append_during_audit):
            events,audit=audit_draining_capture(rest(),c,epoch=7,as_of=NOW)
        self.assertEqual(audit["audited_prefix"]["captured_prefix_count"],4)
        self.assertEqual(audit["audited_prefix"]["queue_size_at_snapshot"],4)
        self.assertEqual(c.snapshot()["buffered"],5)
        self.assertTrue(audit["live_capture_may_have_appended"])
        self.assertFalse(audit["sip_continuity_proven"])
    def test_disconnect_during_audit_fails_closed(self):
        c=capture()
        from recovery_sip_overlap import merge_native_and_captured as real
        def disconnect_during_audit(native,items,**kw):
            out=real(native,items,**kw)
            c.invalidate("407")
            return out
        with patch("recovery_sip_overlap.merge_native_and_captured",
                   side_effect=disconnect_during_audit):
            with self.assertRaisesRegex(SIPOverlapUnsafe,"CAPTURE_INVALIDATED_DURING_AUDIT"):
                audit_draining_capture(rest(),c,epoch=7,as_of=NOW)
    def test_ack_during_audit_fails_closed(self):
        c=capture()
        from recovery_sip_overlap import merge_native_and_captured as real
        def ack_during_audit(native,items,**kw):
            out=real(native,items,**kw)
            c.ack_batch(7,1)
            return out
        with patch("recovery_sip_overlap.merge_native_and_captured",
                   side_effect=ack_during_audit):
            with self.assertRaisesRegex(SIPOverlapUnsafe,"CAPTURE_INVALIDATED_DURING_AUDIT"):
                audit_draining_capture(rest(),c,epoch=7,as_of=NOW)
    def test_concurrent_writer_and_snapshot_have_contiguous_prefix(self):
        c=BoundedEpochCapture(max_messages=300,max_bytes=200000)
        c.start(7)
        go=threading.Event();finished=threading.Event();failures=[]
        def producer():
            go.wait()
            try:
                for n in range(200):
                    c.ingest(7,{"T":"t","S":"A","t":"2026-09-22T15:00:00Z","p":n},
                             received_at=FETCH)
            except Exception as exc:failures.append(exc)
            finally:finished.set()
        t=threading.Thread(target=producer,daemon=True)
        t.start();go.set()
        # Transition is protected by the same lock as the producer.
        c.begin_drain(7)
        while not finished.is_set():
            items,meta=c.snapshot_prefix(7,300)
            self.assertEqual([x.sequence for x in items],
                             list(range(1,len(items)+1)))
            self.assertEqual(meta["captured_prefix_count"],len(items))
        t.join(1)
        self.assertFalse(failures)
        self.assertEqual(c.snapshot()["buffered"],200)
        self.assertEqual(len(c.snapshot_prefix(7,300)[0]),200)
    def test_overflow_invalidates_snapshot_across_threads(self):
        c=BoundedEpochCapture(max_messages=1,max_bytes=1024)
        c.start(7)
        c.ingest(7,bar("2026-09-22T15:59:00Z"),received_at=FETCH)
        c.begin_drain(7)
        copied,_=c.snapshot_prefix(7,1)
        errors=[]
        def overflow():
            try:c.ingest(7,bar("2026-09-22T16:00:00Z"),received_at=FETCH)
            except Exception as exc:errors.append(exc)
        t=threading.Thread(target=overflow);t.start();t.join(1)
        self.assertEqual(len(errors),1)
        self.assertEqual(c.phase,c.INVALID)
        self.assertEqual(len(copied),1)
        self.assertFalse(c.prefix_still_valid(7,1,1))

if __name__=="__main__":unittest.main()
