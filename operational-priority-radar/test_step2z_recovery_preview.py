"""Step 2Z: integrated bounded REST+SIP frozen E/B preview stays read-only."""
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from recovery_chronology import plan_native_batch
from recovery_preview import preview_recovery_batch
from recovery_sip_overlap import SIPOverlapUnsafe
from sip_epoch_capture import BoundedEpochCapture
from test_step2x_signal_replay import RecordingBase,RecordingEarly
from test_step2y_sip_overlap import NOW,FETCH,bar

START=datetime(2026,9,22,15,45,tzinfo=timezone.utc)
def inputs():
    one={"A":[{"t":(START+timedelta(minutes=i)).isoformat(),
               "o":10,"h":11,"l":9,"c":10,"v":100} for i in range(2)]}
    five={"A":[{"t":(START+timedelta(minutes=i*5)).isoformat(),
                "o":10,"h":11,"l":9,"c":10,"v":100,
                "_timeframe":"native_5Min"} for i in range(3)]}
    events,_=plan_native_batch(one,five,["A"],window_start=START,
                               window_end=FETCH,recovered_at=FETCH)
    c=BoundedEpochCapture(max_messages=20,max_bytes=4096)
    c.start(7)
    c.ingest(7,bar((START+timedelta(minutes=2)).isoformat()),
             received_at=FETCH)
    c.ingest(7,{"T":"t","S":"A","t":"2026-09-22T16:00:10Z","p":10.2},
             received_at=FETCH+timedelta(seconds=10))
    c.ingest(7,{"T":"s","S":"A","t":"2026-09-22T16:00:12Z","sc":"2"},
             received_at=FETCH+timedelta(seconds=12))
    c.begin_drain(7)
    return events,c

class TestIntegratedPreview(unittest.TestCase):
    def setUp(self):
        RecordingBase.seen=[];RecordingEarly.seen=[]
    def preview(self,events,c,**kw):
        return preview_recovery_batch(events,c,session="2026-09-22",
            epoch=7,as_of=NOW,base_factory=RecordingBase,
            early_engine_factory=RecordingEarly,**kw)
    def test_sip_new_bar_completes_base_ready_after_rest_history(self):
        events,c=inputs()
        signals,audit=self.preview(events,c)
        self.assertEqual([(s.kind,s.bar_end_ts) for s in signals],
            [("B",START+timedelta(minutes=3)),
             ("E",START+timedelta(minutes=5))])
        self.assertEqual([s.decision_available_ts for s in signals],[NOW,NOW])
        self.assertEqual(audit["overlap"]["unmatched_sip_1m_bars"],1)
        self.assertEqual(audit["overlap"]["sip_trades"],1)
        self.assertEqual(audit["overlap"]["sip_statuses"],1)
        self.assertEqual(audit["canonical_writes"],0)
        self.assertFalse(audit["sip_continuity_proven"])
        self.assertFalse(audit["direct_handoff_authorized"])
        self.assertFalse(audit["retroactive_entries_allowed"])
        self.assertEqual(c.snapshot()["buffered"],3)
        self.assertEqual(c.phase,c.DRAINING)
    def test_actual_frozen_engines_in_integrated_preview(self):
        events,c=inputs()
        signals,audit=preview_recovery_batch(
            events,c,session="2026-09-22",epoch=7,as_of=NOW)
        self.assertTrue(all(s.decision_available_ts==NOW for s in signals))
        self.assertFalse(audit["first_of_session_proven"])
        self.assertFalse(audit["active_trades_reconciled"])
    def test_wrong_epoch_rejected(self):
        events,c=inputs()
        with self.assertRaises(SIPOverlapUnsafe):
            preview_recovery_batch(events,c,session="2026-09-22",
                                   epoch=8,as_of=NOW)
    def test_bounded_total_events(self):
        events,c=inputs()
        with self.assertRaisesRegex(SIPOverlapUnsafe,"MERGE_BOUNDED_LIMIT"):
            self.preview(events,c,max_events=len(events)+1)
    def test_overflow_invalidates_preview(self):
        events,c=inputs()
        c.invalidate("407")
        with self.assertRaises(SIPOverlapUnsafe):
            self.preview(events,c)
    def test_new_event_during_replay_invalidates_stable_preview(self):
        events,c=inputs()
        from recovery_preview import reconstruct_window_signals as real
        def append_during_replay(*args,**kwargs):
            result=real(*args,**kwargs)
            c.ingest(7,{"T":"t","S":"A","t":"2026-09-22T16:00:15Z","p":10.4},
                     received_at=FETCH+timedelta(seconds=15))
            return result
        with patch("recovery_preview.reconstruct_window_signals",
                   side_effect=append_during_replay):
            with self.assertRaisesRegex(SIPOverlapUnsafe,"CAPTURE_CHANGED_DURING_PREVIEW"):
                self.preview(events,c)
        self.assertEqual(c.snapshot()["buffered"],4)
    def test_no_native_history_rejected(self):
        _,c=inputs()
        with self.assertRaisesRegex(SIPOverlapUnsafe,"PREVIEW_REQUIRES_NATIVE"):
            self.preview((),c)

if __name__=="__main__":unittest.main()
