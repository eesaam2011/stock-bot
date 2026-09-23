"""Step3L: actual Redis 7 fenced E/B batch Lua tests in dedicated DB15."""
import copy,unittest
from redis_lua_production import ProductionRedisLua,LeaseLost,AtomicConflict
from test_step2n_real_redis import local_test_redis
from test_step3h_canonical_audit import observed,record
from state_store import record_key,canonical_json
from test_step3t_recovery_commit_permit import permit

class TestRealRedisEBBatch(unittest.TestCase):
    def setUp(self):
        self.r=local_test_redis();self.lua=ProductionRedisLua(self.r)
        self.signals,_=observed();self.records=[record(s) for s in self.signals]
        self.keys=[record_key(x) for x in self.records]
        self.r.delete(*self.keys,self.lua.leader_key,self.lua.generation_key)
        ok,g=self.lua.acquire('W',30)
        self.assertTrue(ok);self.assertEqual(g,1)
    def tearDown(self):
        self.r.delete(*self.keys,self.lua.leader_key,self.lua.generation_key)
    def commit(self,records=None):
        return self.lua._atomic_eb_batch_testonly('W',self.records if records is None else records,1)
    def test_atomic_two_record_commit_and_idempotent_retry(self):
        self.assertEqual(self.commit(),{'inserted':2,'already_identical':0})
        before=self.r.mget(self.keys)
        self.assertEqual(self.commit(),{'inserted':0,'already_identical':2})
        self.assertEqual(self.r.mget(self.keys),before)
    def test_conflict_on_second_key_prevents_first_key_write(self):
        self.r.set(self.keys[1],'OTHER')
        with self.assertRaisesRegex(AtomicConflict,'-20'):self.commit()
        self.assertIsNone(self.r.get(self.keys[0]))
        self.assertEqual(self.r.get(self.keys[1]),'OTHER')
    def test_owner_takeover_blocks_all_writes(self):
        self.r.set(self.lua.leader_key,'SUCCESSOR')
        with self.assertRaises(LeaseLost):self.commit()
        self.assertEqual(self.r.mget(self.keys),[None,None])
    def test_generation_change_blocks_all_writes(self):
        self.r.set(self.lua.generation_key,'2')
        with self.assertRaises(LeaseLost):self.commit()
        self.assertEqual(self.r.mget(self.keys),[None,None])
    def test_existing_identical_plus_missing_is_single_atomic_insert(self):
        self.r.set(self.keys[0],canonical_json(self.records[0]))
        self.assertEqual(self.commit(),{'inserted':1,'already_identical':1})
        self.assertEqual(self.r.get(self.keys[1]),canonical_json(self.records[1]))
    def test_wrong_generation_in_payload_fails_before_redis(self):
        rows=copy.deepcopy(self.records);rows[0]['leader_generation']=2
        with self.assertRaisesRegex(AtomicConflict,'LEADERSHIP_MISMATCH'):
            self.commit(rows)
        self.assertEqual(self.r.mget(self.keys),[None,None])
    def test_public_recovery_commit_requires_coverage_permit(self):
        with self.assertRaisesRegex(AtomicConflict,'COVERAGE_PERMIT_REJECTED'):
            self.lua.atomic_recovery_eb('W',self.records,1)
        self.assertEqual(self.r.mget(self.keys),[None,None])
    def test_public_recovery_commit_with_exact_permit_is_atomic_and_idempotent(self):
        proof=permit()
        self.assertEqual(
            self.lua.atomic_recovery_eb('W',self.records,1,
                coverage_permit=proof),
            {'inserted':2,'already_identical':0})
        before=self.r.mget(self.keys)
        self.assertEqual(
            self.lua.atomic_recovery_eb('W',self.records,1,
                coverage_permit=proof),
            {'inserted':0,'already_identical':2})
        self.assertEqual(self.r.mget(self.keys),before)
    def test_tampered_permit_rejected_before_redis(self):
        proof=permit();proof['handled']-=1
        with self.assertRaisesRegex(AtomicConflict,'COVERAGE_PERMIT_REJECTED'):
            self.lua.atomic_recovery_eb('W',self.records,1,
                coverage_permit=proof)
        self.assertEqual(self.r.mget(self.keys),[None,None])

if __name__=='__main__':unittest.main()
