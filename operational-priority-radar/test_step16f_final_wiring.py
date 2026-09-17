import unittest
from datetime import datetime,timezone,timedelta
from production_adapters import OperationalBaseReady
from redis_outbox_store import RedisOutboxStore
from production_halt_status import ProductionStatusTracker
UTC=timezone.utc
class R:
 def __init__(self):self.d={}
 def scan(self,cursor=0,match=None,count=100):
  import fnmatch
  return 0,[k for k in self.d if fnmatch.fnmatch(k,match)]
 def get(self,k):return self.d.get(k)
 def set(self,k,v,*a,**kw):self.d[k]=v;return True
class TestStep16F(unittest.TestCase):
 def test_outbox_mark_attempt_same_namespace(self):
  import json
  r=R();k="operational_priority_radar:v1:outbox:E";r.d[k]=json.dumps({"event_id":"E","state":"PENDING","attempt_count":0})
  s=RedisOutboxStore(r);self.assertEqual(len(s.pending()),1);self.assertTrue(s.mark_attempt("E",True))
  self.assertEqual(json.loads(r.d[k])["state"],"SENT")
 def test_base_ready_decision_availability_uses_received_at(self):
  b=OperationalBaseReady();start=datetime(2026,1,1,tzinfo=UTC)
  out=None
  for i in range(30):
   bar={"t":(start+timedelta(minutes=i)).isoformat(),"o":1+i*.01,"h":1.02+i*.01,"l":.99+i*.01,"c":1.01+i*.01,"v":1000+i*10,"vw":1.0+i*.01,"n":10}
   recv=start+timedelta(minutes=i+1,seconds=7)
   out=b.on_completed_native_1m("A",bar,recv)
  self.assertEqual(out["decision_available_ts"],recv)
 def test_status_unknown_fail_closed(self):
  self.assertEqual(ProductionStatusTracker().ingest({"S":"A","sc":"5"})["state"],"OTHER")
 def test_composition_has_trade_and_native5_pipeline(self):
  s=open("production_composition.py").read();m=open("shadow_worker_main.py").read()
  self.assertIn('kind=="TRADE"',s);self.assertIn("pipeline.on_trade",s);self.assertIn("pipeline.on_bar",s)
  self.assertIn("decision_pipeline.poll_native5",m)
 def test_composition_passes_resolved_symbols(self):
  s=open("production_composition.py").read();self.assertIn("outbox_store,syms",s)
 def test_session_has_utc_default(self):
  s=open("production_composition.py").read();self.assertIn("datetime.now(timezone.utc).date().isoformat()",s)
if __name__=="__main__":unittest.main()
