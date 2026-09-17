import unittest
from datetime import datetime,timezone,timedelta
from early_core_engine import EarlyCoreCrossing
from early_core_state import EarlyCoreStateWriter
from state_store import CanonicalStateStore,InMemoryRedis,CanonicalConflict
from leadership import InMemoryLeaseBackend,LeadershipManager,NotLeader

def crossing(score=.8, received_offset=0):
    bs=datetime(2026,9,16,15,0,tzinfo=timezone.utc); be=bs+timedelta(minutes=5); rec=be+timedelta(seconds=received_offset)
    return EarlyCoreCrossing("ABC","2026-09-16",bs,be,rec,max(be,rec),score,1.0)

class TestStep4DFirstECanonicalState(unittest.TestCase):
    def setUp(self):
        self.redis=InMemoryRedis(); self.store=CanonicalStateStore(self.redis)
        self.lease=InMemoryLeaseBackend(); self.leader=LeadershipManager(self.lease,"worker-A"); self.token=self.leader.acquire()
        self.w=EarlyCoreStateWriter(self.store,self.leader)

    def test_first_e_create_once(self):
        key,r=self.w.persist_first_e(crossing())
        got=self.w.read_first_e("2026-09-16","ABC")
        self.assertEqual(got,r); self.assertEqual(key,"operational_priority_radar:v1:early_core:2026-09-16:ABC")

    def test_exact_replay_is_idempotent(self):
        k1,r1=self.w.persist_first_e(crossing()); k2,r2=self.w.persist_first_e(crossing())
        self.assertEqual(k1,k2); self.assertEqual(r1,r2)

    def test_later_correction_cannot_rewrite_first_e(self):
        self.w.persist_first_e(crossing())
        with self.assertRaises(CanonicalConflict):
            self.w.persist_first_e(crossing(score=.9,received_offset=20))
        got=self.w.read_first_e("2026-09-16","ABC")
        self.assertEqual(got["score"],.8)

    def test_restart_new_writer_reads_same_first_e(self):
        self.w.persist_first_e(crossing())
        restarted=EarlyCoreStateWriter(self.store,self.leader)
        self.assertEqual(restarted.read_first_e("2026-09-16","ABC")["bar_end_ts"],crossing().bar_end_ts.isoformat())

    def test_stale_leader_cannot_create_e(self):
        self.lease.expire_for_test(); other=LeadershipManager(self.lease,"worker-B"); other.acquire()
        with self.assertRaises(NotLeader): self.w.persist_first_e(crossing())
        with self.assertRaises(KeyError): self.w.read_first_e("2026-09-16","ABC")

    def test_record_contains_fencing_identity(self):
        _,r=self.w.persist_first_e(crossing())
        self.assertEqual(r["leader_generation"],self.token.leader_generation)
        self.assertEqual(r["worker_instance_id"],"worker-A")

    def test_decision_time_is_preserved_not_recomputed(self):
        c=crossing(received_offset=17); self.w.persist_first_e(c)
        got=self.w.read_first_e("2026-09-16","ABC")
        self.assertEqual(got["decision_available_ts"],c.decision_available_ts.isoformat())
        self.assertEqual(got["received_at"],c.received_at.isoformat())

    def test_native_5m_source_is_canonical(self):
        _,r=self.w.persist_first_e(crossing())
        self.assertEqual(r["source_timeframe"],"native_5Min")

if __name__=="__main__": unittest.main()
