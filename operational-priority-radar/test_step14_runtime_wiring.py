import unittest
from runtime_wiring import *
from resource_guard import ResourceGuard
from durable_outbox_sender import *
from market_trust import TrustState
class Redis:
 def __init__(self):self.d={};self.g=0
 def eval(self,script,n,*a):
  from redis_lua_production import ACQUIRE_LEASE_LUA
  if script!=ACQUIRE_LEASE_LUA:raise AssertionError
  k=a[:n];v=a[n:];leader,gen=k;owner,ttl=v
  if leader in self.d:return [0,self.d[leader],str(self.g)]
  self.g+=1;self.d[leader]=owner;self.d[gen]=str(self.g);return [1,owner,str(self.g)]
class SIP:
 def __init__(self,ok=True):self.ok=ok
 def connect_and_subscribe(self):pass
 def verify_continuity(self):return self.ok
class Recovery:
 def __init__(self,g=True,r=True):self.g=g;self.r=r
 def run(self):return {"gap_recovered":self.g,"reconciled":self.r}
class BR:
 def on_completed_native_1m(self,*a):return {"accepted":True,"engine":"B"}
class EC:
 def evaluate(self,*a):return {"accepted":True,"engine":"E"}
class D:pass
def C(rec=None,sip=None,guard=None):return RuntimeComponents(Redis(),sip or SIP(),D(),EC(),BR(),D(),guard or ResourceGuard(),rec or Recovery())
class TestStep14(unittest.TestCase):
 def test_full_trust(self):
  x=RuntimeOrchestrator(C(),"W");x.acquire_leadership();self.assertEqual(x.startup_recovery(),TrustState.LIVE_TRUSTED)
 def test_gap_fail(self):
  x=RuntimeOrchestrator(C(Recovery(False,True)),"W");x.acquire_leadership()
  with self.assertRaises(RuntimeFailClosed):x.startup_recovery()
 def test_reconcile_fail(self):
  x=RuntimeOrchestrator(C(Recovery(True,False)),"W");x.acquire_leadership()
  with self.assertRaises(RuntimeFailClosed):x.startup_recovery()
 def test_continuity_fail(self):
  x=RuntimeOrchestrator(C(sip=SIP(False)),"W");x.acquire_leadership()
  with self.assertRaises(RuntimeFailClosed):x.startup_recovery()
 def test_disconnect_blocks(self):
  x=RuntimeOrchestrator(C(),"W");x.acquire_leadership();x.startup_recovery();x.on_disconnect();self.assertFalse(x.new_decisions_allowed())
 def test_critical_blocks(self):
  x=RuntimeOrchestrator(C(guard=ResourceGuard(80)),"W");x.acquire_leadership();x.startup_recovery();self.assertFalse(x.new_decisions_allowed())
 def test_pressure_allows(self):
  x=RuntimeOrchestrator(C(guard=ResourceGuard(75)),"W");x.acquire_leadership();x.startup_recovery();self.assertTrue(x.new_decisions_allowed())
 def test_active_priority(self):self.assertTrue(ResourceGuard(95,False).active_trade_allowed())
 def test_pipeline_blocks(self):self.assertFalse(ResourceGuard(20,False).new_entries_allowed())
 def test_route_b(self):
  x=RuntimeOrchestrator(C(),"W");x.acquire_leadership();self.assertFalse(x.route_completed_1m("A",{})["accepted"]);x.startup_recovery();self.assertEqual(x.route_completed_1m("A",{})["engine"],"B")
 def test_route_e(self):
  x=RuntimeOrchestrator(C(),"W");x.acquire_leadership();x.startup_recovery();self.assertEqual(x.route_native_5m("A",[],None)["engine"],"E")
 def test_queue_bound(self):
  q=BoundedPriorityQueue(2);q.put("p0",0);q.put("p4",4);q.put("p3",3);self.assertEqual(len(q),2)
 def test_p0_preserved(self):
  q=BoundedPriorityQueue(1);q.put("risk",0);self.assertFalse(q.put("telemetry",4));self.assertEqual(q.get(),"risk")
 def test_higher_priority_evicts_lower(self):
  q=BoundedPriorityQueue(1);q.put("telemetry",4);self.assertTrue(q.put("risk",0));self.assertEqual(q.get(),"risk")
 def test_shadow_suppress(self):
  class S:pass
  class T:pass
  self.assertEqual(DurableOutboxSender(S(),T(),True).deliver_one({"state":"PENDING"}),"SHADOW_SUPPRESSED")
 def test_sender_success_order(self):
  calls=[]
  class S:
   def mark_attempt(self,*a,**k):calls.append("mark")
  class T:
   def send(self,p):calls.append("send")
  self.assertEqual(DurableOutboxSender(S(),T(),False).deliver_one({"state":"PENDING","event_id":"e","payload":{}}),"SENT");self.assertEqual(calls,["send","mark"])
if __name__=="__main__":unittest.main()
