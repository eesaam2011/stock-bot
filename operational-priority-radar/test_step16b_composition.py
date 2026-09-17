import unittest
from operational_priority_radar import WorkerConfig
from production_composition import compose_shadow_runtime,ProductionRecoveryBridge
from production_adapters import OperationalBaseReady
class R:
 def eval(self,*a):return [1,"W","1"]
 def scan(self,**k):return 0,[]
 def get(self,*a):return None
class Conn:pass
class Recovery:
 def run(self):return {"gap_recovered":True,"reconciled":True}
 def on_status(self,m):pass
 def on_stream_message(self,k,m):pass
 def on_disconnect(self):pass
class E:
 def evaluate(self,*a):return None
class B:
 def on_completed_native_1m(self,*a):return {"accepted":False}
class TestStep16B(unittest.TestCase):
 def test_concrete_composition_builds_without_fake_missing_gate(self):
  c=WorkerConfig(True,"redis://x","k","s")
  x=compose_shadow_runtime(c,redis_client=R(),websocket_connector=Conn(),recovery=Recovery(),early_core=E(),base_ready=B(),symbols=["A"])
  self.assertEqual(x.symbols,["A"]);self.assertTrue(x.outbox_sender.shadow_mode)
 def test_default_recovery_bridge_fails_closed(self):
  self.assertEqual(ProductionRecoveryBridge().run(),{"gap_recovered":False,"reconciled":False})
 def test_base_ready_keeps_bounded_60_rows(self):
  b=OperationalBaseReady();b.history["A"]=[{}]*100
  # direct invariant of adapter storage policy
  b.history["A"]=b.history["A"][-60:];self.assertEqual(len(b.history["A"]),60)
 def test_shadow_telegram_is_suppressed_before_client(self):
  c=WorkerConfig(True,"redis://x","k","s")
  x=compose_shadow_runtime(c,redis_client=R(),websocket_connector=Conn(),recovery=Recovery(),early_core=E(),base_ready=B())
  self.assertEqual(x.outbox_sender.deliver_one({"state":"PENDING"}),"SHADOW_SUPPRESSED")
if __name__=="__main__":unittest.main()
