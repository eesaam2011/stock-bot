import unittest,random
from datetime import datetime,timezone,timedelta
from base_ready import phase2_features as new_phase2
from historical_base_ready_reference import phase2_features as old_phase2
from base_ready_state import BaseReadyStateWriter
from state_store import CanonicalStateStore,InMemoryRedis,CanonicalConflict
from leadership import InMemoryLeaseBackend,LeadershipManager,NotLeader

def fixture(n=60,seed=7):
    rng=random.Random(seed);s=datetime(2026,9,16,13,30,tzinfo=timezone.utc);out=[];px=4.0
    for i in range(n):
        px=max(.5,px+rng.uniform(-.015,.035));o=px+rng.uniform(-.01,.01);c=px+rng.uniform(-.01,.01)
        h=max(o,c)+rng.uniform(.005,.03);l=min(o,c)-rng.uniform(.005,.025);v=max(0,10000+i*100+rng.randint(-1500,2500))
        out.append({"t":(s+timedelta(minutes=i)).isoformat().replace("+00:00","Z"),"o":o,"h":h,"l":l,"c":c,"v":v,"vw":(o+h+l+c)/4,"n":rng.randint(1,60)})
    return out

class TestStep5BHistoricalEquivalence(unittest.TestCase):
    def test_twenty_deterministic_fixtures_exact(self):
        m=datetime(2026,9,16,16,tzinfo=timezone.utc)
        for seed in range(20):self.assertEqual(new_phase2(fixture(seed=seed),m),old_phase2(fixture(seed=seed),m))
    def test_completion_cutoffs_exact(self):
        r=fixture();s=datetime(2026,9,16,13,30,tzinfo=timezone.utc)
        for minute in (23,24,25,30,45,59):
            for sec in (0,30,59):
                m=s+timedelta(minutes=minute,seconds=sec);self.assertEqual(new_phase2(r,m),old_phase2(r,m))
    def test_zero_volume_path_exact(self):
        r=fixture()
        for b in r:b["v"]=0
        m=datetime(2026,9,16,16,tzinfo=timezone.utc);self.assertEqual(new_phase2(r,m),old_phase2(r,m))

class TestStep5BFirstBCanonicalState(unittest.TestCase):
    def setUp(self):
        self.store=CanonicalStateStore(InMemoryRedis());self.lease=InMemoryLeaseBackend()
        self.leader=LeadershipManager(self.lease,"worker-A");self.token=self.leader.acquire();self.w=BaseReadyStateWriter(self.store,self.leader)
        r=fixture();self.bs=datetime.fromisoformat(r[-1]["t"].replace("Z","+00:00"));self.be=self.bs+timedelta(minutes=1)
        self.rec=self.be+timedelta(seconds=2);self.av=self.rec
        self.f,self.d=new_phase2(r,datetime(2026,9,16,16,tzinfo=timezone.utc))
    def persist(self,feature_delta=0.0,received=None,available=None):
        f=dict(self.f);f["opportunity"]+=feature_delta
        return self.w.persist_first_b("2026-09-16","ABC",self.bs,self.be,received or self.rec,available or self.av,f,self.d)
    def test_first_b_create_once(self):
        k,r=self.persist();self.assertEqual(k,"operational_priority_radar:v1:base_ready:2026-09-16:ABC");self.assertEqual(self.w.read_first_b("2026-09-16","ABC"),r)
    def test_exact_replay_idempotent(self):self.assertEqual(self.persist(),self.persist())
    def test_correction_cannot_rewrite(self):
        self.persist()
        with self.assertRaises(CanonicalConflict):self.persist(.01)
    def test_restart_reads_same_b(self):
        self.persist();self.assertEqual(BaseReadyStateWriter(self.store,self.leader).read_first_b("2026-09-16","ABC")["decision_available_ts"],self.av.isoformat())
    def test_stale_leader_rejected(self):
        self.lease.expire_for_test();LeadershipManager(self.lease,"worker-B").acquire()
        with self.assertRaises(NotLeader):self.persist()
    def test_fencing_identity(self):
        _,r=self.persist();self.assertEqual((r["leader_generation"],r["worker_instance_id"]),(self.token.leader_generation,"worker-A"))
    def test_completed_1m_source(self):self.assertEqual(self.persist()[1]["source_timeframe"],"completed_1Min")
    def test_late_decision_time_preserved(self):
        late=self.be+timedelta(seconds=19);r=self.persist(received=late,available=late)[1]
        self.assertEqual((r["received_at"],r["decision_available_ts"]),(late.isoformat(),late.isoformat()))

if __name__=="__main__":unittest.main()
