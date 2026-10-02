"""Step 2S: cancellation and lease release after untrusted recovery."""
import asyncio
import threading
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace
from production_recovery import ProductionStartupRecovery
from active_trade_recovery import ActiveTradeChronologicalReconciler,ActiveTradeRecoveryError
from test_step16a_long_running_main import Orch,WS,Drain,Sender,OStore
from shadow_worker_main import ShadowRuntimeSupervisor

NOW=datetime(2026,9,22,16,tzinfo=timezone.utc)
class Reader:
 def active_trades(self):return []
 def earliest_decision_anchor(self,session):return None
class BlockingRest:
 def __init__(self):
  self.entered=threading.Event();self.resume=threading.Event()
 def native_recovery_batch(self,*args,**kwargs):
  self.entered.set()
  if not self.resume.wait(timeout=3):
   raise RuntimeError("TEST_REST_WAIT_TIMEOUT")
  return ({"A":[{"t":"2026-09-22T15:00:00Z"}]},
          {"A":[{"t":"2026-09-22T15:00:00Z"}]})

class TestCancel(unittest.IsolatedAsyncioTestCase):
 async def test_rest_thread_exits_fail_closed_after_cancel(self):
  rest=BlockingRest()
  rec=ProductionStartupRecovery(Reader(),rest,None,"2026-09-22",["A"],
                                now_fn=lambda:NOW)
  task=asyncio.create_task(asyncio.to_thread(rec.run))
  try:
   self.assertTrue(await asyncio.wait_for(asyncio.to_thread(rest.entered.wait,1),2))
   rec.cancel()
   rest.resume.set()
   result=await asyncio.wait_for(task,timeout=2)
   self.assertFalse(result["gap_recovered"])
   self.assertFalse(result["reconciled"])
   self.assertEqual(result["reason"],"RecoveryFailure")
   self.assertFalse(rec.ready_after_stream())
  finally:
   rec.cancel();rest.resume.set()
   if not task.done():await asyncio.wait_for(task,2)
 async def test_supervisor_cancels_recovery_and_releases_owner_lease(self):
  o,w,d=Orch(),WS(),Drain()
  rec=o.c.recovery
  canceled=[]
  rec.cancel=lambda:canceled.append(True)
  released=[]
  o.redis.release=lambda owner:released.append(owner) or True
  o.begin_recovery=lambda: (_ for _ in ()).throw(RuntimeError("REPLAY_UNPROVEN"))
  s=ShadowRuntimeSupervisor(o,w,Sender(),OStore(),[],"k","s","u",d)
  s.decision_pipeline=SimpleNamespace(
      leadership=SimpleNamespace(require_current=lambda:True,
                                 sync_generation=lambda:1))
  with self.assertRaisesRegex(RuntimeError,"REPLAY_UNPROVEN"):
   await asyncio.wait_for(s.run(),2)
  self.assertEqual(canceled,[True])
  self.assertEqual(released,["W"])
  self.assertTrue(w.stopped)
  self.assertEqual(d.calls,["DRAINING","STOPPED"])
 def test_active_trade_reconciler_rejects_commit_after_cancel(self):
  rec=ActiveTradeChronologicalReconciler(
      SimpleNamespace(),SimpleNamespace(),"W")
  rec.cancel_event=threading.Event();rec.cancel_event.set()
  with self.assertRaisesRegex(ActiveTradeRecoveryError,"RECOVERY_CANCELLED"):
   rec.reconcile({"state":"ACTIVE_PRE_T1"})

if __name__=="__main__":unittest.main()
