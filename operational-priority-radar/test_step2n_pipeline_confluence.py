"""Step 2N: production pipeline must cache the canonical opportunity record."""
import unittest
from types import SimpleNamespace
from production_pipeline import ProductionDecisionPipeline
from state_store import key_early_core,key_base_ready,key_opportunity

class TestProductionConfluence(unittest.TestCase):
 def test_writer_returns_key_pipeline_reads_canonical_record(self):
  p=ProductionDecisionPipeline.__new__(ProductionDecisionPipeline)
  p.session="S";p._entry_opportunities={}
  e={"decision_available_ts":"2026-09-22T15:00:00+00:00"}
  b={"decision_available_ts":"2026-09-22T15:01:00+00:00"}
  opportunity={"state":"CONFLUENCE_VALID","entry_trigger_ts":"2026-09-22T15:01:00+00:00",
               "delta_seconds":60,"symbol":"A"}
  key=key_opportunity("S","A")
  records={key_early_core("S","A"):e,key_base_ready("S","A"):b}
  calls=[]
  def get(k,typ=None):
   calls.append((k,typ))
   return records.get(k)
  p._get=get
  p.cw=SimpleNamespace(evaluate_and_persist=lambda *args:(None,key))
  def get_with_persisted(k,typ=None):
   calls.append((k,typ))
   return opportunity if k==key and typ=="opportunity" else records.get(k)
  p._get=get_with_persisted
  p._confluence("A",__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
  self.assertIs(p._entry_opportunities["A"],opportunity)
  self.assertIn((key,"opportunity"),calls)
 def test_expired_key_not_cached_as_entry(self):
  p=ProductionDecisionPipeline.__new__(ProductionDecisionPipeline)
  p.session="S";p._entry_opportunities={}
  key=key_opportunity("S","A")
  p._get=lambda k,typ=None: (
      {"state":"OPPORTUNITY_EXPIRED"} if k==key and typ=="opportunity"
      else None)
  p.cw=SimpleNamespace(evaluate_and_persist=lambda *args:(None,key))
  p._confluence("A",None)
  self.assertNotIn("A",p._entry_opportunities)

if __name__=="__main__":unittest.main()
