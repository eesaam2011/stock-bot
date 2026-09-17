import unittest
from datetime import datetime,timezone,timedelta
from state_store import *
from leadership import *
from risk_engine import *
from entry_commit import EntryCommitBuilder
from entry_atomic_commit import *
from atomic_commit import AtomicCommitError
T=datetime(2026,9,16,15,0,tzinfo=timezone.utc)

class TestStep8BAtomicEntry(unittest.TestCase):
 def setUp(self):
  self.mem=InMemoryRedis();self.store=CanonicalStateStore(self.mem);self.lease=InMemoryLeaseBackend()
  self.leader=LeadershipManager(self.lease,"worker-A");self.tok=self.leader.acquire()
  o=base_record("opportunity","2026-09-16","ABC","CONFLUENCE_VALID",T.isoformat())
  o.update(e_id="E:1",b_id="B:1",delta_seconds=0,first_event="SIMULTANEOUS",entry_trigger_ts=T.isoformat(),
           expiry=(T+timedelta(minutes=15)).isoformat(),terminal_reason=None,leader_generation=self.tok.leader_generation,worker_instance_id="worker-A")
  self.store.create(o);self.o=o
  self.r=RiskDecision(RiskStatus.APPROVED,9.8,9.751,2.49,10.249,10.498)
  self.builder=EntryCommitBuilder(self.leader);self.atomic=AtomicEntryWriter(InMemoryEntryAtomicBackend(self.mem),self.leader)
 def build(self):
  return self.builder.build(self.o,self.r,10.0,T+timedelta(seconds=3))
 def test_deterministic_entry_event_id(self):
  a=self.build();b=self.build();self.assertEqual(a[2]["event_id"],b[2]["event_id"])
 def test_no_commit_without_approved_risk(self):
  with self.assertRaises(ValueError):self.builder.build(self.o,RiskDecision(RiskStatus.NO_STRUCTURE),10,T)
 def test_entry_payload_has_no_fill_claim(self):
  _,_,x=self.build();self.assertNotIn("fill",x["payload"]);self.assertEqual(x["event_type"],"ENTRY")
 def test_monitoring_deadline_120m(self):
  _,t,_=self.build();self.assertEqual(t["monitoring_deadline"],(T+timedelta(seconds=3+7200)).isoformat())
 def test_atomic_success_makes_all_three_visible(self):
  n,t,x=self.build();self.atomic.commit(self.o,n,t,x)
  self.assertEqual(self.store.read(record_key(n),"opportunity")["state"],"ENTRY_COMMITTED")
  self.assertEqual(self.store.read(record_key(t),"trade")["state"],"ACTIVE_PRE_T1")
  self.assertEqual(self.store.read(record_key(x),"outbox")["state"],"PENDING")
 def _assert_crash_none_visible(self,point):
  n,t,x=self.build()
  with self.assertRaises(AtomicCommitError):self.atomic.commit(self.o,n,t,x,point)
  self.assertEqual(self.store.read(record_key(self.o),"opportunity")["state"],"CONFLUENCE_VALID")
  with self.assertRaises(KeyError):self.store.read(record_key(t),"trade")
  with self.assertRaises(KeyError):self.store.read(record_key(x),"outbox")
 def test_crash_before(self):self._assert_crash_none_visible("before_commit")
 def test_crash_between_state_trade(self):self._assert_crash_none_visible("between_state_trade")
 def test_crash_between_trade_outbox(self):self._assert_crash_none_visible("between_trade_outbox")
 def test_exact_retry_is_idempotent_after_success(self):
  n,t,x=self.build();self.atomic.commit(self.o,n,t,x)
  # retry sees state changed and cannot create a second canonical decision
  with self.assertRaises(CanonicalConflict):self.atomic.commit(self.o,n,t,x)
  self.assertEqual(len([k for k in self.mem._data if ":outbox:" in k]),1)
 def test_stale_leader_rejected(self):
  n,t,x=self.build();self.lease.expire_for_test();LeadershipManager(self.lease,"worker-B").acquire()
  with self.assertRaises(NotLeader):self.atomic.commit(self.o,n,t,x)
 def test_fencing_same_generation_all_records(self):
  n,t,x=self.build();self.assertEqual({n["leader_generation"],t["leader_generation"],x["leader_generation"]},{self.tok.leader_generation})
 def test_trade_levels_equal_risk_decision(self):
  _,t,_=self.build();self.assertEqual((t["entry_alert_price"],t["structural_stop"],t["t1"],t["t2"]),(10.0,9.751,10.249,10.498))
if __name__=="__main__":unittest.main()
