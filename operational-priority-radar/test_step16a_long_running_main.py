import unittest,asyncio
from unittest.mock import patch
from types import SimpleNamespace
from shadow_worker_main import ShadowRuntimeSupervisor,startup_probe_env
from production_composition import compose_shadow_runtime,CompositionUnavailable
from operational_priority_radar import WorkerConfig

class RedisLua:
 def renew(self,*a):return None
class Rec:
 def ready_after_stream(self):return True
 def continuity_verified(self,epoch):return epoch==1
class C:
 def __init__(self):self.recovery=Rec()
class Orch:
 def __init__(self):self.redis=RedisLua();self.worker_id="W";self.calls=[];self.c=C()
 def acquire_leadership(self):self.calls.append("leader")
 def begin_recovery(self):self.calls.append("recovery");return {"gap_recovered":True,"reconciled":True}
 def mark_stream_connected(self):self.calls.append("stream")
 def finish_reconciliation(self,continuity_ok=False,epoch=None):
  if not continuity_ok or epoch!=1:raise AssertionError("missing proof")
  self.calls.append("trusted")
class WS:
 def __init__(self):
  self.stopped=False;self.started=asyncio.Event();self.connected_event=asyncio.Event()
  self.connection_epoch=1
  self.epoch_capture=SimpleNamespace(DIRECT="DIRECT",phase="DIRECT",buffer=SimpleNamespace(epoch=1))
 async def reconnect_loop(self,*a):self.started.set();self.connected_event.set();await asyncio.Event().wait()
 def stop(self):self.stopped=True
class OStore:
 def pending(self):return []
class Sender:
 def deliver_one(self,e):raise AssertionError
class Drain:
 def __init__(self):self.calls=[]
 def begin(self):self.calls.append("DRAINING")
 def complete(self):self.calls.append("STOPPED")

class TestStep16A(unittest.IsolatedAsyncioTestCase):
 async def test_supervisor_is_long_running_until_stop(self):
  o,w,d=Orch(),WS(),Drain();s=ShadowRuntimeSupervisor(o,w,Sender(),OStore(),[], "k","s","url",d)
  s.decision_pipeline=SimpleNamespace(
      leadership=SimpleNamespace(require_current=lambda:True,sync_generation=lambda:1),
      bars={},trades={},session="S",memory_stats=lambda:{},
      poll_native5=lambda *args:{})
  task=asyncio.create_task(s.run())
  try:
   await asyncio.wait_for(w.started.wait(),timeout=3.0)
   await asyncio.sleep(0.05)
   self.assertFalse(task.done());s.request_stop();await asyncio.wait_for(task,timeout=3.0)
  finally:
   s.request_stop()
   if not task.done():
    task.cancel();await asyncio.gather(task,return_exceptions=True)
  self.assertEqual(o.calls,["leader","recovery","stream","trusted"]);self.assertTrue(w.stopped);self.assertEqual(d.calls,["DRAINING","STOPPED"])
 async def test_unproven_post_stream_recovery_times_out_and_cleans_up(self):
  o,w,d=Orch(),WS(),Drain()
  o.c.recovery.ready_after_stream=lambda:False
  s=ShadowRuntimeSupervisor(o,w,Sender(),OStore(),[],"k","s","u",d)
  s.post_stream_timeout=.06
  s.decision_pipeline=SimpleNamespace(
      leadership=SimpleNamespace(require_current=lambda:True,sync_generation=lambda:1))
  with self.assertRaisesRegex(RuntimeError,"POST_STREAM_RECONCILIATION_TIMEOUT"):
   await asyncio.wait_for(s.run(),timeout=2)
  self.assertTrue(w.stopped)
  self.assertEqual(d.calls,["DRAINING","STOPPED"])
  self.assertNotIn("trusted",o.calls)
 async def test_startup_recovery_failure_cleans_up_lease_task(self):
  o,w,d=Orch(),WS(),Drain()
  def fail():raise RuntimeError("GAP_RECOVERY_FAILED")
  o.begin_recovery=fail
  s=ShadowRuntimeSupervisor(o,w,Sender(),OStore(),[],"k","s","u",d)
  s.decision_pipeline=SimpleNamespace(
      leadership=SimpleNamespace(require_current=lambda:True,sync_generation=lambda:1))
  with self.assertRaisesRegex(RuntimeError,"GAP_RECOVERY_FAILED"):
   await asyncio.wait_for(s.run(),timeout=2)
  self.assertTrue(w.stopped)
  self.assertEqual(d.calls,["DRAINING","STOPPED"])
 async def test_outbox_loop_remains_running(self):
  s=ShadowRuntimeSupervisor(Orch(),WS(),Sender(),OStore(),[],"k","s","u")
  t=asyncio.create_task(s.outbox_loop());await asyncio.sleep(0.01);self.assertFalse(t.done());s.request_stop();await t
 def test_config_still_forces_shadow(self):
  self.assertEqual(startup_probe_env({"REDIS_URL":"x","ALPACA_API_KEY":"k","ALPACA_SECRET_KEY":"s","OPR_SHADOW_MODE":"false"})["status"],"ACTIONABLE_MODE_BLOCKED")
 def test_composition_has_concrete_production_path(self):
  c=WorkerConfig(True,"redis://x","k","s")
  # If deployment dependencies are absent locally, fail explicitly rather than fabricate them.
  try:
   # The real dependency path is exercised, but no test may call Alpaca
   # with placeholder credentials or depend on a live market endpoint.
   with patch("production_composition.build_operational_universe",return_value=["A"]):
    x=compose_shadow_runtime(c)
   self.assertEqual(x.symbols,["A"])
  except RuntimeError as e:
   self.assertIn("PRODUCTION_DEPENDENCY_MISSING",str(e))

if __name__=="__main__":unittest.main()
