import unittest,os
from unittest.mock import patch
from operational_priority_radar import *
from market_trust import Trust,TrustState
from shadow_metrics import ShadowMetrics
from render_worker import render_contract

class TestStep13(unittest.TestCase):
 def cfg(self,shadow=True):
  return WorkerConfig(shadow,"redis://x","k","s","t","c")
 def test_shadow_is_default_env(self):
  with patch.dict(os.environ,{"REDIS_URL":"redis://x","ALPACA_API_KEY":"k","ALPACA_SECRET_KEY":"s"},clear=True):
   self.assertTrue(WorkerConfig.from_env().shadow_mode)
 def test_shadow_blocks_actionable_even_live_trusted(self):
  w=OperationalPriorityRadarWorker(self.cfg(True),{})
  w.trust=Trust(TrustState.LIVE_TRUSTED,True,True,True,True);w.actionable_alerts_enabled=True
  self.assertFalse(w.can_create_new_entry())
 def test_shadow_output_never_actionable(self):
  w=OperationalPriorityRadarWorker(self.cfg(True),{})
  self.assertFalse(w.shadow_decision({"symbol":"ABC"})["actionable_sent"])
 def test_explicit_actionable_assertion_fails_shadow(self):
  with self.assertRaises(ActionableModeBlocked):OperationalPriorityRadarWorker(self.cfg(True),{}).assert_actionable_block()
 def test_live_mode_blocked_while_slow_consumer_pending(self):
  with self.assertRaisesRegex(ActionableModeBlocked,"PENDING_SHADOW_BENCHMARK"):OperationalPriorityRadarWorker(self.cfg(False),{})
 def test_missing_redis_fails(self):
  with self.assertRaises(ValueError):WorkerConfig(True,None,"k","s").validate()
 def test_missing_alpaca_fails(self):
  with self.assertRaises(ValueError):WorkerConfig(True,"redis://x",None,None).validate()
 def test_worker_instance_uuid_unique(self):
  a=OperationalPriorityRadarWorker(self.cfg(),{});b=OperationalPriorityRadarWorker(self.cfg(),{})
  self.assertNotEqual(a.worker_instance_id,b.worker_instance_id)
 def test_metrics_blocker(self):
  m=ShadowMetrics(duplicate_leaders=1);self.assertIn("duplicate_leaders",m.live_gate_blockers())
 def test_metrics_no_blocker_initially(self):
  self.assertEqual(ShadowMetrics().live_gate_blockers(),[])
 def test_render_background_worker_contract(self):
  c=render_contract();self.assertEqual(c["service_type"],"Background Worker");self.assertEqual(c["canonical_state"],"Redis")
 def test_render_shadow_initial(self):
  self.assertEqual(render_contract()["initial_mode"],"OPERATIONAL_SHADOW_MODE")
if __name__=="__main__":unittest.main()
