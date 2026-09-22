"""Step 2U: abort untrusted REST startup without waiting for 900s timeout."""
import asyncio,threading,unittest
from types import SimpleNamespace
from shadow_worker_main import ShadowRuntimeSupervisor
from test_step16a_long_running_main import Orch,WS,Drain,Sender,OStore

def supervisor(o,w,d):
 s=ShadowRuntimeSupervisor(o,w,Sender(),OStore(),[],"k","s","u",d)
 s.decision_pipeline=SimpleNamespace(
  leadership=SimpleNamespace(require_current=lambda:True,sync_generation=lambda:1),
  bars={},trades={},session="S",memory_stats=lambda:{},
  poll_native5=lambda *a:{})
 return s

class TestStartupWatchdog(unittest.IsolatedAsyncioTestCase):
 async def test_explicit_unproven_result_aborts_without_waiting_post_stream(self):
  o,w,d=Orch(),WS(),Drain()
  o.begin_recovery=lambda:{"gap_recovered":False,"reconciled":False,
                            "reason":"CANONICAL_REPLAY_NOT_IMPLEMENTED"}
  s=supervisor(o,w,d);s.post_stream_timeout=100
  with self.assertRaisesRegex(RuntimeError,"STARTUP_RECOVERY_UNPROVEN:CANONICAL_REPLAY_NOT_IMPLEMENTED"):
   await asyncio.wait_for(s.run(),2)
  self.assertNotIn("trusted",o.calls)
  self.assertTrue(w.stopped)
  self.assertEqual(d.calls,["DRAINING","STOPPED"])
 async def test_capture_overflow_cancels_blocked_rest(self):
  o,w,d=Orch(),WS(),Drain()
  entered=threading.Event();cancelled=threading.Event()
  def recovery():
   entered.set()
   if not cancelled.wait(2):raise RuntimeError("TEST_WAIT_TIMEOUT")
   return {"gap_recovered":False,"reconciled":False,"reason":"CANCELLED"}
  o.begin_recovery=recovery
  o.c.recovery.cancel=cancelled.set
  s=supervisor(o,w,d)
  task=asyncio.create_task(s.run())
  try:
   self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait,1),2))
   w.epoch_capture.phase="INVALID" # overflow invalidates capture while ACK remains up
   with self.assertRaisesRegex(RuntimeError,"SIP_CAPTURE_INVALID_DURING_RECOVERY"):
    await asyncio.wait_for(task,2)
   self.assertTrue(cancelled.is_set())
   self.assertNotIn("trusted",o.calls)
   self.assertTrue(w.stopped)
  finally:
   cancelled.set()
   if not task.done():s.request_stop();await asyncio.wait_for(task,2)
 async def test_epoch_change_cancels_blocked_rest(self):
  o,w,d=Orch(),WS(),Drain()
  entered=threading.Event();cancelled=threading.Event()
  def recovery():
   entered.set();cancelled.wait(2)
   return {"gap_recovered":False,"reconciled":False}
  o.begin_recovery=recovery;o.c.recovery.cancel=cancelled.set
  s=supervisor(o,w,d)
  task=asyncio.create_task(s.run())
  try:
   self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait,1),2))
   w.connection_epoch=2
   with self.assertRaisesRegex(RuntimeError,"SIP_CAPTURE_INVALID_DURING_RECOVERY"):
    await asyncio.wait_for(task,2)
   self.assertTrue(cancelled.is_set())
  finally:
   cancelled.set()
   if not task.done():s.request_stop();await asyncio.wait_for(task,2)

if __name__=="__main__":unittest.main()
