"""Step 2L: first-write Redis fencing contract (fake Redis; not live Redis)."""
import unittest
from state_store import CanonicalStateStore, base_record, CanonicalConflict
from production_runtime_state import RedisCanonicalBackend
from redis_lua_production import LeaseLost

LEADER="operational_priority_radar:v1:runtime:leader"
GEN="operational_priority_radar:v1:runtime:leader_generation"

class FakeRedis:
    def __init__(self):
        self.d={LEADER:"W", GEN:"7"}
    def get(self,k):
        return self.d.get(k)
    def eval(self,script,n,*args):
        leader,gen,key=args[:n]
        owner,expected,raw=args[n:]
        if self.d.get(leader)!=owner:return -10
        if self.d.get(gen)!=expected:return -11
        if key in self.d:return 0
        self.d[key]=raw
        return 1

def record():
    x=base_record("base_ready","S","A","FIRST_TRUE","2026-09-22T00:00:00+00:00")
    x.update(features={},bar_start_ts="t",bar_end_ts="t",received_at="t",
             decision_available_ts="t",source_timeframe="completed_1Min",
             leader_generation=7,worker_instance_id="W")
    return x

class TestAtomicCanonical(unittest.TestCase):
    def test_idempotent_first_write(self):
        r=FakeRedis();s=CanonicalStateStore(RedisCanonicalBackend(r));x=record()
        self.assertEqual(s.create(x),s.create(x))
    def test_owner_loss_before_commit(self):
        r=FakeRedis();s=CanonicalStateStore(RedisCanonicalBackend(r));r.d[LEADER]="OTHER"
        with self.assertRaises(LeaseLost):s.create(record())
        self.assertFalse(any(":base_ready:" in k for k in r.d))
    def test_generation_change(self):
        r=FakeRedis();s=CanonicalStateStore(RedisCanonicalBackend(r));r.d[GEN]="8"
        with self.assertRaises(LeaseLost):s.create(record())
    def test_unfenced_cas_forbidden(self):
        with self.assertRaisesRegex(RuntimeError,"UNFENCED_CANONICAL_COMPARE_AND_SET_FORBIDDEN"):
            RedisCanonicalBackend(FakeRedis()).compare_and_set("k",None,"v")
    def test_duplicate_conflict(self):
        r=FakeRedis();s=CanonicalStateStore(RedisCanonicalBackend(r));x=record();s.create(x)
        y=record();y["state"]="DIFFERENT"
        with self.assertRaises(CanonicalConflict):s.create(y)

if __name__=="__main__":
    unittest.main()
