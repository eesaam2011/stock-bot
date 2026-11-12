"""Step 2Y: causal REST/SIP overlap audit, capture retained until true replay."""
import asyncio
import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from recovery_chronology import plan_native_batch
from recovery_sip_overlap import (merge_native_and_captured,audit_draining_capture,
                                  SIPOverlapUnsafe)
from sip_epoch_capture import BoundedEpochCapture,EpochCaptureError
from websocket_runtime import WebSocketRuntime
from alpaca_production_market import AlpacaSIPProtocol
from test_step2r_sip_dispatch import SlowWS

UTC=timezone.utc
NOW=datetime(2026,9,22,16,1,20,tzinfo=UTC)
FETCH=datetime(2026,9,22,16,0,5,tzinfo=UTC)
def bar(ts,kind="b",**overrides):
    d={"T":kind,"S":"A","t":ts,"o":10,"h":11,"l":9,"c":10,"v":100}
    d.update(overrides)
    return d
def rest():
    one={"A":[{k:v for k,v in bar("2026-09-22T15:59:00Z").items() if k!="T"}]}
    five={"A":[{**{k:v for k,v in bar("2026-09-22T15:55:00Z").items() if k!="T"},
                 "_timeframe":"native_5Min"}]}
    return plan_native_batch(one,five,["A"],
        window_start=datetime(2026,9,22,15,55,tzinfo=UTC),
        window_end=FETCH,recovered_at=FETCH)[0]
def capture():
    c=BoundedEpochCapture(max_messages=20,max_bytes=4096)
    c.start(7)
    c.ingest(7,bar("2026-09-22T15:59:00Z"),received_at=FETCH)
    c.ingest(7,{"T":"t","S":"A","t":"2026-09-22T16:00:10Z","p":10.2},
             received_at=FETCH+timedelta(seconds=7))
    c.ingest(7,{"T":"s","S":"A","t":"2026-09-22T16:00:12Z","sc":"2"},
             received_at=FETCH+timedelta(seconds=8))
    c.ingest(7,bar("2026-09-22T16:00:00Z"),
             received_at=FETCH+timedelta(seconds=60))
    c.begin_drain(7)
    return c

class TestSIPOverlap(unittest.TestCase):
    def test_equal_overlap_dedup_new_bar_trade_status_order(self):
        c=capture()
        events,audit=audit_draining_capture(rest(),c,epoch=7,as_of=NOW)
        self.assertEqual(audit["overlap_equal_1m_bars"],1)
        self.assertEqual(audit["unmatched_sip_1m_bars"],1)
        self.assertEqual((audit["sip_trades"],audit["sip_statuses"]),(1,1))
        self.assertEqual(len(events),5)
        self.assertEqual([e.kind for e in events],
            ["BAR_1M","BAR_NATIVE_5M","TRADE","STATUS","BAR_1M"])
        self.assertEqual(events[-1].available_at,FETCH+timedelta(seconds=60))
        self.assertFalse(audit["sip_continuity_proven"])
        self.assertFalse(audit["direct_handoff_authorized"])
        self.assertEqual(c.snapshot()["buffered"],4)
        self.assertEqual(c.phase,c.DRAINING)
    def test_rest_sip_conflict_fails_without_ack(self):
        c=capture();c._items[0].payload["c"]=10.5
        with self.assertRaisesRegex(SIPOverlapUnsafe,"REST_SIP_BAR_CONFLICT"):
            audit_draining_capture(rest(),c,epoch=7,as_of=NOW)
        self.assertEqual(c.snapshot()["buffered"],4)
    def test_unproven_epoch_fails(self):
        c=capture()
        with self.assertRaisesRegex(SIPOverlapUnsafe,"CAPTURE_NOT_DRAINING_IN_EPOCH"):
            audit_draining_capture(rest(),c,epoch=8,as_of=NOW)
    def test_disconnect_invalidates_audit(self):
        c=capture();c.invalidate("407")
        with self.assertRaisesRegex(SIPOverlapUnsafe,"CAPTURE_NOT_DRAINING_IN_EPOCH"):
            audit_draining_capture(rest(),c,epoch=7,as_of=NOW)
    def test_sequence_gap_fails(self):
        c=capture();xs=list(c.peek_batch(7,20))
        xs[1]=replace(xs[1],sequence=9)
        with self.assertRaisesRegex(SIPOverlapUnsafe,"CAPTURE_SEQUENCE_GAP"):
            merge_native_and_captured(rest(),xs,epoch=7,as_of=NOW)
    def test_receive_time_after_audit_fails(self):
        c=capture();xs=list(c.peek_batch(7,20))
        xs[0]=replace(xs[0],received_at=(NOW+timedelta(seconds=1)).isoformat())
        with self.assertRaisesRegex(SIPOverlapUnsafe,"CAPTURE_FUTURE_EVENT_OR_RECEIVE"):
            merge_native_and_captured(rest(),xs,epoch=7,as_of=NOW)
    def test_partial_captured_bar_fails(self):
        c=capture();xs=list(c.peek_batch(7,20))
        xs[-1]=replace(xs[-1],event_ts="2026-09-22T16:01:00+00:00",
                       payload=bar("2026-09-22T16:01:00Z"))
        with self.assertRaisesRegex(SIPOverlapUnsafe,"INCOMPLETE_CAPTURED_BAR"):
            merge_native_and_captured(rest(),xs,epoch=7,as_of=NOW)
    def test_missing_receive_time_fails(self):
        c=capture();xs=list(c.peek_batch(7,20))
        xs[0]=replace(xs[0],received_at="")
        with self.assertRaisesRegex(SIPOverlapUnsafe,"CAPTURE_RECEIVE_TIME_MISSING"):
            merge_native_and_captured(rest(),xs,epoch=7,as_of=NOW)
    def test_conflicting_sip_duplicate_fails(self):
        c=capture();xs=list(c.peek_batch(7,20))
        new=replace(xs[-1],sequence=5,payload=bar("2026-09-22T16:00:00Z",c=10.7))
        with self.assertRaisesRegex(SIPOverlapUnsafe,"CONFLICTING_SIP_DUPLICATE"):
            merge_native_and_captured(rest(),xs+[new],epoch=7,as_of=NOW)
    def test_identical_sip_duplicate_dedup(self):
        c=capture();xs=list(c.peek_batch(7,20))
        same=replace(xs[-1],sequence=5)
        _,a=merge_native_and_captured(rest(),xs+[same],epoch=7,as_of=NOW)
        self.assertEqual(a["unmatched_sip_1m_bars"],1)
    def test_invalid_capture_receive_time_rejected(self):
        c=BoundedEpochCapture();c.start(7)
        with self.assertRaisesRegex(EpochCaptureError,"SIP_RECEIVED_AT_INVALID"):
            c.ingest(7,bar("2026-09-22T15:59:00Z"),
                     received_at=datetime(2026,9,22,16))
    def test_overflow_is_not_partial_overlap_proof(self):
        c=BoundedEpochCapture(max_messages=1,max_bytes=1024);c.start(7)
        c.ingest(7,bar("2026-09-22T15:59:00Z"),received_at=FETCH)
        with self.assertRaisesRegex(RuntimeError,"OVERFLOW"):
            c.ingest(7,bar("2026-09-22T16:00:00Z"),received_at=NOW)
        with self.assertRaises(SIPOverlapUnsafe):
            audit_draining_capture(rest(),c,epoch=7,as_of=NOW)

class TestQueuedCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_pending_socket_read_joins_reader(self):
        ws=SlowWS([])
        entered=asyncio.Event()
        class WatchedWS(SlowWS):
            async def _events(self):
                entered.set()
                await self.keep_open.wait()
                if False:yield None
        ws=WatchedWS([])
        disconnected=[]
        rt=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,
            lambda m:asyncio.sleep(0),
            lambda:asyncio.sleep(0, result=disconnected.append(True)),
            dispatch_queue_max=4)
        task=asyncio.create_task(rt.run_once("u","k","s",["A"]))
        try:
            await asyncio.wait_for(entered.wait(),1)
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            self.assertFalse(rt.connected_event.is_set())
            self.assertEqual(disconnected,[True])
            self.assertEqual(rt.performance_snapshot()["dispatch_queue"]["depth"],0)
            self.assertFalse(any(t.get_coro().__name__=="__anext__"
                for t in asyncio.all_tasks() if t is not asyncio.current_task()))
        finally:
            rt.stop();task.cancel();await asyncio.gather(task,return_exceptions=True)

if __name__=="__main__":unittest.main()
