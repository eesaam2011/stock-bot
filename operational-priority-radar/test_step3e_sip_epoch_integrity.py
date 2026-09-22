"""Step 3E: fail-closed ACK-epoch prefix, timestamps and immutable ingress."""
import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from sip_epoch_capture import BoundedEpochCapture
from recovery_sip_overlap import merge_native_and_captured,SIPOverlapUnsafe
from recovery_preview import preview_recovery_batch
from test_step2y_sip_overlap import capture,rest,bar,NOW,FETCH
from test_step2z_recovery_preview import inputs
from test_step2x_signal_replay import RecordingBase,RecordingEarly

class TestEpochPrefixIntegrity(unittest.TestCase):
    def preview(self,events,c,epoch=7):
        return preview_recovery_batch(
            events,c,session="2026-09-22",epoch=epoch,as_of=NOW,
            base_factory=RecordingBase,early_engine_factory=RecordingEarly)
    def test_start_resets_sequence_and_ack_watermark_per_epoch(self):
        c=capture()
        self.assertEqual(c.snapshot()["last_sequence"],4)
        self.assertEqual(c.ack_batch(7,2),2)
        self.assertEqual(c.snapshot()["acked_upto"],2)
        c.start(8)
        self.assertEqual(c.snapshot()["last_sequence"],0)
        self.assertEqual(c.snapshot()["acked_upto"],0)
        item=c.ingest(8,bar("2026-09-22T15:59:00Z"),received_at=FETCH)
        self.assertEqual(item.sequence,1)
        self.assertEqual(item.epoch,8)
        c.begin_drain(8)
        self.assertEqual(c.snapshot_prefix(8)[1]["first_sequence"],1)
    def test_ingress_nested_payload_is_immutable_copy(self):
        c=BoundedEpochCapture(max_messages=5,max_bytes=4096)
        c.start(7)
        msg=bar("2026-09-22T15:59:00Z",conditions=[{"codes":[1,2]}])
        item=c.ingest(7,msg,received_at=FETCH)
        msg["conditions"][0]["codes"][0]=999
        self.assertEqual(item.payload["conditions"][0]["codes"],[1,2])
        c.begin_drain(7)
        copied,_=c.snapshot_prefix(7)
        copied[0].payload["conditions"][0]["codes"][1]=777
        self.assertEqual(c.peek_batch(7)[0].payload["conditions"][0]["codes"],[1,2])
    def test_previously_acked_prefix_rejected_before_frozen_replay(self):
        events,c=inputs()
        c.ack_batch(7,1)
        with patch("recovery_preview.reconstruct_window_signals",
                   side_effect=AssertionError("replay must not run")):
            with self.assertRaisesRegex(
                    SIPOverlapUnsafe,"PREVIEW_CAPTURE_PREFIX_ALREADY_ACKED"):
                self.preview(events,c)
    def test_acked_entire_epoch_rejected_even_empty_queue(self):
        events,c=inputs()
        c.ack_batch(7,3)
        self.assertEqual(c.snapshot()["buffered"],0)
        self.assertEqual(c.snapshot()["acked_upto"],3)
        with self.assertRaisesRegex(
                SIPOverlapUnsafe,"PREVIEW_CAPTURE_PREFIX_ALREADY_ACKED"):
            self.preview(events,c)
    def test_append_after_ack_does_not_restore_missing_prefix(self):
        events,c=inputs()
        c.ack_batch(7,1)
        c.ingest(7,{"T":"t","S":"A","t":"2026-09-22T16:00:15Z","p":10.4},
                 received_at=FETCH+timedelta(seconds=15))
        meta=c.snapshot_prefix(7)[1]
        self.assertEqual(meta["first_sequence"],2)
        self.assertEqual(meta["acked_upto_at_snapshot"],1)
        with self.assertRaisesRegex(
                SIPOverlapUnsafe,"PREVIEW_CAPTURE_PREFIX_ALREADY_ACKED"):
            self.preview(events,c)
    def test_mid_preview_ack_rejected(self):
        events,c=inputs()
        from recovery_preview import reconstruct_window_signals as real
        def ack_during_replay(*a,**kw):
            out=real(*a,**kw)
            c.ack_batch(7,1)
            return out
        with patch("recovery_preview.reconstruct_window_signals",
                   side_effect=ack_during_replay):
            with self.assertRaisesRegex(SIPOverlapUnsafe,
                                        "CAPTURE_CHANGED_DURING_PREVIEW"):
                self.preview(events,c)
    def test_payload_timestamp_tamper_rejected(self):
        c=capture()
        xs=list(c.peek_batch(7,20))
        xs[0].payload["t"]="2026-09-22T15:58:00Z"
        with self.assertRaisesRegex(SIPOverlapUnsafe,
                                    "CAPTURE_EVENT_TIME_MISMATCH"):
            merge_native_and_captured(rest(),xs,epoch=7,as_of=NOW)
    def test_payload_timestamp_missing_rejected(self):
        c=capture()
        xs=list(c.peek_batch(7,20))
        xs[0].payload.pop("t")
        with self.assertRaisesRegex(SIPOverlapUnsafe,
                                    "CAPTURE_PAYLOAD_TIME_INVALID"):
            merge_native_and_captured(rest(),xs,epoch=7,as_of=NOW)
    def test_equivalent_timestamp_offset_is_accepted(self):
        c=capture()
        xs=list(c.peek_batch(7,20))
        xs[0].payload["t"]="2026-09-22T18:59:00+03:00"
        _,audit=merge_native_and_captured(rest(),xs,epoch=7,as_of=NOW)
        self.assertEqual(audit["overlap_equal_1m_bars"],1)
    def test_zero_or_negative_sequence_rejected(self):
        c=capture()
        xs=list(c.peek_batch(7,20))
        for seq in (0,-1):
            bad=[replace(xs[0],sequence=seq),*xs[1:]]
            with self.assertRaisesRegex(SIPOverlapUnsafe,
                                        "CAPTURE_SEQUENCE_GAP"):
                merge_native_and_captured(rest(),bad,epoch=7,as_of=NOW)
    def test_unacked_new_epoch_preview_still_read_only(self):
        events,c=inputs()
        signals,audit=self.preview(events,c)
        self.assertEqual(c.snapshot()["acked_upto"],0)
        self.assertFalse(audit["direct_handoff_authorized"])
        self.assertFalse(audit["sip_continuity_proven"])
        self.assertEqual(audit["canonical_writes"],0)

if __name__=="__main__":unittest.main()
