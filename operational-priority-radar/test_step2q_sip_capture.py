"""Step 2Q: bounded per-ACK-epoch capture; no fabricated continuity."""
import asyncio
import unittest
from types import SimpleNamespace
from sip_epoch_capture import BoundedEpochCapture,EpochCaptureError,EpochCaptureOverflow
from test_step2o_websocket_epoch import FakeWS
from websocket_runtime import WebSocketRuntime,WebSocketProtocolError
from alpaca_production_market import AlpacaSIPProtocol
from trust_gate import require_live_trust_proof,ContinuityUnproven

def bar(t="2026-09-22T15:00:00Z"):
    return {"T":"b","S":"A","t":t,"c":10}

class TestCaptureContract(unittest.TestCase):
 def test_exact_boundary_and_overflow_discards_all(self):
  c=BoundedEpochCapture(max_messages=2,max_bytes=1024)
  c.start(1)
  a=c.ingest(1,bar());b=c.ingest(1,bar("2026-09-22T15:01:00Z"))
  self.assertEqual((a.sequence,b.sequence),(1,2))
  self.assertEqual(c.snapshot()["buffered"],2)
  with self.assertRaisesRegex(EpochCaptureOverflow,"OVERFLOW"):
   c.ingest(1,bar("2026-09-22T15:02:00Z"))
  self.assertEqual(c.snapshot()["phase"],c.INVALID)
  self.assertEqual(c.snapshot()["buffered"],0)
  self.assertEqual(c.snapshot()["bytes"],0)
 def test_byte_limit_rejects_oversize_without_partial_record(self):
  c=BoundedEpochCapture(max_messages=2,max_bytes=1024);c.start(1)
  msg=bar();msg["payload"]="x"*1100
  with self.assertRaises(EpochCaptureOverflow):c.ingest(1,msg)
  self.assertEqual(c.snapshot()["buffered"],0)
 def test_non_destructive_replay_and_contiguous_ack(self):
  c=BoundedEpochCapture(max_messages=3,max_bytes=2048);c.start(3)
  c.ingest(3,bar());c.ingest(3,bar("2026-09-22T15:01:00Z"))
  c.begin_drain(3)
  a=c.peek_batch(3,2)
  self.assertEqual([x.sequence for x in a],[1,2])
  self.assertEqual(c.peek_batch(3,2),a)
  with self.assertRaises(EpochCaptureError):c.ack_batch(3,3)
  self.assertEqual(c.ack_batch(3,1),1)
  self.assertEqual(c.peek_batch(3,2)[0].sequence,2)
  with self.assertRaisesRegex(EpochCaptureError,"CAPTURE_DIRECT_HANDOFF_INVALID"):
   c.finish_direct(3)
  self.assertEqual(c.ack_batch(3,2),1)
  with self.assertRaisesRegex(EpochCaptureError,"EXTERNAL_RECONCILIATION"):
   c.finish_direct(3)
  c.finish_direct(3,reconciliation_proven=True)
  self.assertEqual(c.phase,c.DIRECT)
 def test_reconnect_invalidates_old_epoch(self):
  c=BoundedEpochCapture();c.start(1);c.ingest(1,bar())
  c.start(2)
  with self.assertRaises(EpochCaptureError):c.ingest(1,bar())
  self.assertEqual(c.snapshot()["buffered"],0)
  c.ingest(2,bar())
  c.invalidate()
  with self.assertRaises(EpochCaptureError):c.begin_drain(2)
 def test_missing_or_naive_timestamp_fails_closed(self):
  c=BoundedEpochCapture();c.start(1)
  with self.assertRaisesRegex(EpochCaptureError,"TIME_MISSING"):
   c.ingest(1,{"T":"s","S":"A"})
  with self.assertRaisesRegex(EpochCaptureError,"TIME_INVALID"):
   c.ingest(1,bar("2026-09-22T15:00:00"))
 def test_capture_direct_is_not_sufficient_for_live_trust(self):
  c=BoundedEpochCapture();c.start(4);c.begin_drain(4)
  c.finish_direct(4,reconciliation_proven=True)
  event=asyncio.Event();event.set()
  ws=SimpleNamespace(connected_event=event,connection_epoch=4,epoch_capture=c)
  recovery=SimpleNamespace(ready_after_stream=lambda:True,
                           continuity_verified=lambda epoch:False)
  leader=SimpleNamespace(require_current=lambda:True)
  with self.assertRaisesRegex(ContinuityUnproven,"CONTINUITY_PROOF_MISSING"):
   require_live_trust_proof(ws,recovery,leader,4)

class TestWebSocketCapture(unittest.IsolatedAsyncioTestCase):
 async def test_overflow_causes_disconnect_and_invalidates_epoch(self):
  ws=FakeWS([bar(),bar("2026-09-22T15:01:00Z")])
  c=BoundedEpochCapture(max_messages=1,max_bytes=1024)
  seen=[];disconnected=[]
  runtime=None
  async def on_message(msg):seen.append(msg)
  async def on_disconnect():disconnected.append(runtime.connected_event.is_set())
  runtime=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,on_message,on_disconnect,
                           epoch_capture=c)
  with self.assertRaises(EpochCaptureOverflow):
   await runtime.run_once("u","k","s",["A"])
  self.assertEqual(len(seen),1)
  self.assertEqual(disconnected,[False])
  self.assertEqual(c.phase,c.INVALID)
  self.assertEqual(c.snapshot()["buffered"],0)
 async def test_complete_ack_starts_capture_and_eof_clears_it(self):
  ws=FakeWS([bar()])
  c=BoundedEpochCapture()
  snapshots=[]
  runtime=None
  async def on_message(msg):
   snapshots.append(c.snapshot())
  runtime=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,on_message,
                           lambda:asyncio.sleep(0),epoch_capture=c)
  with self.assertRaisesRegex(WebSocketProtocolError,"SIP_STREAM_EOF"):
   await runtime.run_once("u","k","s",["A"])
  self.assertEqual((snapshots[0]["phase"],snapshots[0]["epoch"],snapshots[0]["buffered"]),
                   ("CAPTURING",1,1))
  self.assertEqual(c.phase,c.INVALID)

if __name__=="__main__":unittest.main()
