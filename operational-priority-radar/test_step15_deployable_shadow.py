import unittest,asyncio,json
from restart_recovery import *
from halt_reconciliation import *
from shutdown import GracefulDrain
from redis_outbox_store import RedisOutboxStore

class Store:
 def __init__(self,e=None,b=None,t=None):self.e=e;self.b=b;self.t=t
 def get_e(self,*a):return self.e
 def get_b(self,*a):return self.b
 def get_trade(self,*a):return self.t
class R5:
 def __init__(self):self.calls=0
 def reconstruct_first_e_chronologically(self,*a):self.calls+=1;return "E-REC"
class R1:
 def __init__(self):self.calls=0
 def reconstruct_first_b_chronologically(self,*a):self.calls+=1;return "B-REC"
class TR:
 def __init__(self):self.calls=[]
 def reconcile(self,t):self.calls.append(t)
class FakeRedis:
 def __init__(self,d):self.d=d
 def scan(self,cursor=0,match=None,count=100):return 0,list(self.d)
 def get(self,k):return self.d.get(k)
 def set(self,k,v):self.d[k]=v

class TestStep15(unittest.TestCase):
 def test_existing_e_b_authoritative_not_recomputed(self):
  a,b=R5(),R1();x=CanonicalRestartRecovery(Store("E","B"),a,b,TR()).recover_symbol("S","A")
  self.assertEqual((x["e"],x["b"],a.calls,b.calls),("E","B",0,0))
 def test_missing_e_b_reconstructed_chronologically(self):
  a,b=R5(),R1();x=CanonicalRestartRecovery(Store(),a,b,TR()).recover_symbol("S","A")
  self.assertEqual((x["e"],x["b"]),("E-REC","B-REC"))
 def test_active_trade_reconciled_before_reconstruction(self):
  order=[]
  class T:
   def reconcile(self,t):order.append("TRADE")
  class A:
   def reconstruct_first_e_chronologically(self,*x):order.append("E");return "E"
  class B:
   def reconstruct_first_b_chronologically(self,*x):order.append("B");return "B"
  CanonicalRestartRecovery(Store(t={"id":1}),A(),B(),T()).recover_symbol("S","A")
  self.assertEqual(order[0],"TRADE")
 def test_halt_preserves_state(self):
  h=HaltReconciler(None,None).on_halt({"symbol":"A","state":"ACTIVE_POST_T1"});self.assertEqual((h["state"],h["pre_halt_state"]),("HALTED_ACTIVE","ACTIVE_POST_T1"))
 def test_resume_requires_proven_trading(self):
  class S:
   def current(self,s):return "UNKNOWN"
  with self.assertRaises(HaltStateUnknown):HaltReconciler(S(),TR()).on_resume({"symbol":"A","state":"HALTED_ACTIVE","pre_halt_state":"ACTIVE_PRE_T1"})
 def test_resume_reconciles_then_restores(self):
  class S:
   def current(self,s):return "TRADING"
  tr=TR();x=HaltReconciler(S(),tr).on_resume({"symbol":"A","state":"HALTED_ACTIVE","pre_halt_state":"ACTIVE_PRE_T1"})
  self.assertEqual(x["state"],"ACTIVE_PRE_T1");self.assertEqual(len(tr.calls),1)
 def test_drain_blocks_new_entries(self):
  d=GracefulDrain();self.assertTrue(d.new_entries_allowed());d.begin();self.assertFalse(d.new_entries_allowed());self.assertEqual(d.complete(),"STOPPED")
 def test_outbox_pending_scan(self):
  p="operational_priority_radar:v1:outbox:x";r=FakeRedis({p:json.dumps({"state":"PENDING","event_id":"e","updated_at":"1"})})
  self.assertEqual(RedisOutboxStore(r).pending()[0]["event_id"],"e")
 def test_outbox_ignores_sent(self):
  p="operational_priority_radar:v1:outbox:x";r=FakeRedis({p:json.dumps({"state":"SENT","event_id":"e"})});self.assertEqual(RedisOutboxStore(r).pending(),[])
if __name__=="__main__":unittest.main()
