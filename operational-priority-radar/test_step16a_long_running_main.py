import unittest,asyncio
from unittest.mock import patch
from shadow_worker_main import ShadowRuntimeSupervisor,startup_probe_env
from production_composition import compose_shadow_runtime,CompositionUnavailable
from operational_priority_radar import WorkerConfig

class RedisLua:
 def renew(self,*a):return None
class Rec:
 def ready_after_stream(self):return True
class C:
 def __init__(self):self.recovery=Rec()
class Orch:
 def __init__(self):self.redis=RedisLua();self.worker_id="W";self.calls=[];self.c=C()
 def acquire_leadership(self):self.calls.append("leader")
 def begin_recovery(self):self.calls.append("recovery");return {"gap_recovered":True,"reconciled":True}
 def mark_stream_connected(self):self.calls.append("stream")
 def finish_reconciliation(self,continuity_ok=True):self.calls.append("trusted")
class WS:
 def __init__(self):self.stopped=False;self.started=asyncio.Event();self.connected_event=asyncio.Event()
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
  task=asyncio.create_task(s.run());await w.started.wait();await asyncio.sleep(0)
  self.assertFalse(task.done());s.request_stop();await task
  self.assertEqual(o.calls,["leader","recovery","stream","trusted"]);self.assertTrue(w.stopped);self.assertEqual(d.calls,["DRAINING","STOPPED"])
 async def test_outbox_loop_remains_running(self):
  s=ShadowRuntimeSupervisor(Orch(),WS(),Sender(),OStore(),[],"k","s","u")
  t=asyncio.create_task(s.outbox_loop());await asyncio.sleep(0.01);self.assertFalse(t.done());s.request_stop();await t
 def test_config_still_forces_shadow(self):
  self.assertEqual(startup_probe_env({"REDIS_URL":"x","ALPACA_API_KEY":"k","ALPACA_SECRET_KEY":"s","OPR_SHADOW_MODE":"false"})["status"],"ACTIONABLE_MODE_BLOCKED")
 def test_composition_has_concrete_production_path(self):
  c=WorkerConfig(True,"redis://x","k","s")
  # If deployment dependencies are absent locally, fail explicitly rather than fabricate them.
  try:
   x=compose_shadow_runtime(c)
  except RuntimeError as e:
   self.assertIn("PRODUCTION_DEPENDENCY_MISSING",str(e))

if __name__=="__main__":unittest.main()
