import unittest
from datetime import datetime,timezone,timedelta
from operational_confluence import *
from confluence_state import ConfluenceStateWriter
from state_store import CanonicalStateStore,InMemoryRedis,CanonicalConflict
from leadership import InMemoryLeaseBackend,LeadershipManager,NotLeader

T=datetime(2026,9,16,15,0,tzinfo=timezone.utc)
def rec(kind,ts):
    return {"event_id":f"{kind}:1","decision_available_ts":ts.isoformat()}

class TestStep6ConfluencePolicy(unittest.TestCase):
    def test_e_then_b_inside_window(self):
        d=decide_confluence(T,T+timedelta(minutes=10),T+timedelta(minutes=10))
        self.assertEqual((d.status,d.first_event,d.delta_seconds,d.entry_trigger_ts),(ConfluenceStatus.VALID,"E",600,T+timedelta(minutes=10)))
    def test_b_then_e_inside_window(self):
        d=decide_confluence(T+timedelta(minutes=8),T,T+timedelta(minutes=8))
        self.assertEqual((d.status,d.first_event,d.delta_seconds),(ConfluenceStatus.VALID,"B",480))
    def test_delta_zero_valid(self):
        d=decide_confluence(T,T,T);self.assertEqual((d.status,d.first_event,d.delta_seconds,d.entry_trigger_ts),(ConfluenceStatus.VALID,"SIMULTANEOUS",0,T))
    def test_exact_15_minutes_inclusive(self):
        d=decide_confluence(T,T+timedelta(minutes=15),T+timedelta(minutes=15));self.assertEqual(d.status,ConfluenceStatus.VALID);self.assertEqual(d.delta_seconds,900)
    def test_over_15_minutes_expired(self):
        d=decide_confluence(T,T+timedelta(minutes=15,seconds=.001),T+timedelta(minutes=16));self.assertEqual(d.status,ConfluenceStatus.EXPIRED)
    def test_single_event_waits_at_exact_expiry(self):
        d=decide_confluence(T,None,T+timedelta(minutes=15));self.assertEqual(d.status,ConfluenceStatus.WAITING)
    def test_single_event_expires_after_window(self):
        d=decide_confluence(T,None,T+timedelta(minutes=15,microseconds=1));self.assertEqual(d.status,ConfluenceStatus.EXPIRED)
    def test_trigger_is_later_decision_availability(self):
        d=decide_confluence(T+timedelta(seconds=20),T,T+timedelta(seconds=20));self.assertEqual(d.entry_trigger_ts,T+timedelta(seconds=20))

class TestStep6CanonicalOpportunity(unittest.TestCase):
    def setUp(self):
        self.store=CanonicalStateStore(InMemoryRedis());self.lease=InMemoryLeaseBackend()
        self.leader=LeadershipManager(self.lease,"worker-A");self.token=self.leader.acquire()
        self.w=ConfluenceStateWriter(self.store,self.leader)
    def test_valid_confluence_persisted_once(self):
        d,k=self.w.evaluate_and_persist("2026-09-16","ABC",rec("E",T),rec("B",T+timedelta(minutes=5)),T+timedelta(minutes=5))
        self.assertEqual(d.status,ConfluenceStatus.VALID);self.assertEqual(k,"operational_priority_radar:v1:opportunity:2026-09-16:ABC")
    def test_exact_replay_idempotent(self):
        args=("2026-09-16","ABC",rec("E",T),rec("B",T+timedelta(minutes=5)),T+timedelta(minutes=5))
        self.assertEqual(self.w.evaluate_and_persist(*args)[1],self.w.evaluate_and_persist(*args)[1])
    def test_expired_cannot_resurrect(self):
        self.w.evaluate_and_persist("2026-09-16","ABC",rec("E",T),None,T+timedelta(minutes=16))
        with self.assertRaises(CanonicalConflict):
            self.w.evaluate_and_persist("2026-09-16","ABC",rec("E",T),rec("B",T+timedelta(minutes=10)),T+timedelta(minutes=16))
    def test_valid_cannot_be_retimed_by_correction(self):
        self.w.evaluate_and_persist("2026-09-16","ABC",rec("E",T),rec("B",T+timedelta(minutes=5)),T+timedelta(minutes=5))
        with self.assertRaises(CanonicalConflict):
            self.w.evaluate_and_persist("2026-09-16","ABC",rec("E",T),rec("B",T+timedelta(minutes=6)),T+timedelta(minutes=6))
    def test_stale_leader_cannot_persist_terminal_decision(self):
        self.lease.expire_for_test();LeadershipManager(self.lease,"worker-B").acquire()
        with self.assertRaises(NotLeader):
            self.w.evaluate_and_persist("2026-09-16","ABC",rec("E",T),rec("B",T),T)
    def test_waiting_state_not_canonicalized(self):
        d,k=self.w.evaluate_and_persist("2026-09-16","ABC",rec("E",T),None,T+timedelta(minutes=5))
        self.assertEqual(d.status,ConfluenceStatus.WAITING);self.assertIsNone(k)
    def test_fencing_identity_on_valid_record(self):
        _,k=self.w.evaluate_and_persist("2026-09-16","ABC",rec("E",T),rec("B",T),T)
        r=self.store.read(k,"opportunity");self.assertEqual((r["leader_generation"],r["worker_instance_id"]),(self.token.leader_generation,"worker-A"))
    def test_persisted_trigger_uses_decision_time_not_bar_time(self):
        late=T+timedelta(seconds=11)
        _,k=self.w.evaluate_and_persist("2026-09-16","ABC",rec("E",T),rec("B",late),late)
        r=self.store.read(k,"opportunity");self.assertEqual(r["entry_trigger_ts"],late.isoformat())

if __name__=="__main__":unittest.main()
