import unittest
from datetime import datetime,timezone,timedelta
from state_store import *
from leadership import *
from trade_transition_commit import *
T=datetime(2026,9,16,15,0,tzinfo=timezone.utc)
class TestStep9B(unittest.TestCase):
 def setUp(self):
  self.mem=InMemoryRedis();self.store=CanonicalStateStore(self.mem);self.lease=InMemoryLeaseBackend();self.leader=LeadershipManager(self.lease,"W");self.tok=self.leader.acquire()
  r=base_record("trade","2026-09-16","ABC","ACTIVE_PRE_T1",T.isoformat())
  r.update(trade_id="TR1",entry_alert_price=10.,structure_low=9.8,structural_stop=9.751,risk_pct=2.49,t1=10.249,t2=10.498,monitoring_deadline=(T+timedelta(minutes=120)).isoformat(),leader_generation=self.tok.leader_generation,worker_instance_id="W")
  self.store.create(r);self.r=r;self.w=TradeTransitionWriter(InMemoryTradeTransitionBackend(self.mem),self.leader)
 def test_t1_atomic(self):
  n,o=self.w.commit(self.r,"ACTIVE_POST_T1","T1",T+timedelta(minutes=1))
  self.assertEqual(n["state"],"ACTIVE_POST_T1");self.assertEqual(o["event_id"],"T1:TR1")
 def test_stop_atomic(self):
  n,o=self.w.commit(self.r,"CLOSED_STOP","STOP",T+timedelta(minutes=1),{"stop_level":9.751,"first_observed_breach_price":9.4})
  self.assertEqual((n["state"],o["event_type"]),("CLOSED_STOP","STOP"));self.assertNotIn("fill",o["payload"])
 def test_t2_atomic(self):
  n,o=self.w.commit(self.r,"CLOSED_T2","T2",T+timedelta(minutes=1));self.assertEqual(o["event_id"],"T2:TR1")
 def test_post_t1_exit_atomic(self):
  n,o=self.w.commit(self.r,"CLOSED_POST_T1_BREAKEVEN_EXIT","POST_T1_EXIT",T+timedelta(minutes=2));self.assertEqual(o["event_id"],"POST_T1_EXIT:TR1")
 def test_final_monitoring_expired(self):
  n,o=self.w.commit(self.r,"MONITORING_EXPIRED","MONITORING_EXPIRED",T+timedelta(minutes=120));self.assertEqual(o["event_id"],"FINAL:TR1")
 def test_recovery_ambiguous_uses_final_event(self):
  n,o=self.w.commit(self.r,"RECOVERY_PATH_AMBIGUOUS","RECOVERY_PATH_AMBIGUOUS",T+timedelta(minutes=3));self.assertEqual(o["event_id"],"FINAL:TR1")
 def test_crash_before_no_partial(self):
  with self.assertRaises(AtomicTradeTransitionError):self.w.commit(self.r,"CLOSED_STOP","STOP",T,crash="before")
  self.assertEqual(self.store.read(record_key(self.r),"trade")["state"],"ACTIVE_PRE_T1");self.assertEqual(len([k for k in self.mem._data if ":outbox:" in k]),0)
 def test_crash_between_no_partial(self):
  with self.assertRaises(AtomicTradeTransitionError):self.w.commit(self.r,"CLOSED_STOP","STOP",T,crash="between")
  self.assertEqual(self.store.read(record_key(self.r),"trade")["state"],"ACTIVE_PRE_T1");self.assertEqual(len([k for k in self.mem._data if ":outbox:" in k]),0)
 def test_stale_expected_rejected(self):
  self.w.commit(self.r,"ACTIVE_POST_T1","T1",T)
  with self.assertRaises(CanonicalConflict):self.w.commit(self.r,"CLOSED_STOP","STOP",T+timedelta(seconds=1))
 def test_stale_leader_rejected(self):
  self.lease.expire_for_test();LeadershipManager(self.lease,"B").acquire()
  with self.assertRaises(NotLeader):self.w.commit(self.r,"CLOSED_STOP","STOP",T)
 def test_deterministic_ids(self):
  self.assertEqual(trade_event_id("STOP","TR1"),trade_event_id("STOP","TR1"))
 def test_no_fill_claim_for_target(self):
  n,o=self.w.commit(self.r,"CLOSED_T2","T2",T);self.assertNotIn("fill",o["payload"])
if __name__=="__main__":unittest.main()
