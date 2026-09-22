"""Step 2P: no active-trade or entry processing from untrusted SIP epochs."""
import unittest,threading
from datetime import datetime,timezone
from types import SimpleNamespace
from production_pipeline import ProductionDecisionPipeline
from production_halt_status import ProductionStatusTracker
from production_composition import compose_shadow_runtime
from operational_priority_radar import WorkerConfig

class Redis:
 def get(self,*args):return None
 def scan(self,**kwargs):return 0,[]
class FakeRecovery:
 def __init__(self):self.status_tracker=ProductionStatusTracker();self.messages=[]
 def run(self):return {"gap_recovered":False,"reconciled":False}
 def on_status(self,m):self.status_tracker.ingest(m)
 def on_stream_message(self,k,m):self.messages.append(k)
 def on_disconnect(self):self.status_tracker.reset()
class FakeEarly:
 def evaluate(self,*a):return None
class FakeBase:
 def on_completed_native_1m(self,*a):return {"accepted":False}

class TestUntrustedQuarantine(unittest.IsolatedAsyncioTestCase):
 async def test_untrusted_bars_and_trades_not_forwarded_to_pipeline(self):
  rec=FakeRecovery()
  supervisor=compose_shadow_runtime(
      WorkerConfig(True,"redis://unused","k","s"),redis_client=Redis(),
      websocket_connector=object(),recovery=rec,early_core=FakeEarly(),
      base_ready=FakeBase(),symbols=["A"])
  p=supervisor.decision_pipeline
  calls=[]
  p.on_trade=lambda *args:calls.append("TRADE")
  p.on_bar=lambda *args:calls.append("BAR")
  ws=supervisor.websocket_runtime
  await ws.on_message({"T":"t","S":"A","p":1,"t":"2026-09-22T15:00:00Z"})
  await ws.on_message({"T":"b","S":"A","c":1,"t":"2026-09-22T15:00:00Z"})
  self.assertEqual(calls,[])
  self.assertEqual(rec.messages,["TRADE","BAR"])
  await ws.on_message({"T":"s","S":"A","sc":"3","t":"2026-09-22T15:00:01Z"})
  self.assertEqual(rec.status_tracker.current("A"),"TRADING")
  p._entry_opportunities["A"]={"state":"CONFLUENCE_VALID"}
  p.trades["A"]=["stale"]
  await ws.on_disconnect()
  self.assertEqual(rec.status_tracker.current("A"),"UNKNOWN")
  self.assertEqual(p._entry_opportunities,{})
  self.assertEqual(p.trades,{})
 def test_native5_rechecks_gate_after_rest_and_before_canonical_write(self):
  p=ProductionDecisionPipeline.__new__(ProductionDecisionPipeline)
  p.symbols=["A"];p.session="S"
  p.r=SimpleNamespace(mget=lambda keys:[None])
  p._memory_probe=lambda *args,**kw:None
  p.decision_lock=threading.RLock()
  trust=[True]
  p.decision_gate=lambda:trust[0]
  p.rest=SimpleNamespace(bars_multi=lambda *args,**kw:{"A":[{"t":"2026-09-22T15:00:00Z"}]})
  def evaluate(*args):
   trust[0]=False  # disconnect while native5 thread is evaluating
   return SimpleNamespace()
  p.ec=SimpleNamespace(required_history_minutes=lambda:60,
                       telemetry=lambda rows:{},evaluate=evaluate)
  writes=[]
  p.ew=SimpleNamespace(persist_first_e=lambda crossing:writes.append(crossing))
  p._confluence=lambda *args:self.fail("untrusted confluence")
  stats=p.poll_native5(datetime(2026,9,22,16,tzinfo=timezone.utc),
                       True,batch_size=1,max_workers=1)
  self.assertEqual(writes,[])
  self.assertTrue(stats["aborted_untrusted"])
  self.assertEqual(stats["early_core_persisted"],0)
 def test_unknown_and_halted_status_block_entry(self):
  p=ProductionDecisionPipeline.__new__(ProductionDecisionPipeline)
  p._entry_opportunities={"A":{"state":"CONFLUENCE_VALID",
                               "entry_trigger_ts":"2026-09-22T15:00:00+00:00"}}
  p.status_tracker=ProductionStatusTracker()
  p._get=lambda *args: self.fail("unknown halt must return before canonical lookup")
  p._try_entry("A",None)
  p.status_tracker.ingest({"S":"A","sc":"2"})
  p._try_entry("A",None)
  p.status_tracker.reset()
  self.assertEqual(p.status_tracker.current("A"),"UNKNOWN")

if __name__=="__main__":unittest.main()
