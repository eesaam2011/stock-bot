"""Step3L: low-level E/B fenced Lua primitive is NOT a recovery gate."""
import copy,unittest
from redis_lua_production import ProductionRedisLua,AtomicConflict
from test_step3h_canonical_audit import observed,record

class FakeRedis:
    def __init__(self):self.calls=[]
    def eval(self,*args):self.calls.append(args);return 2

class TestEBBatchPreflight(unittest.TestCase):
    def setUp(self):
        self.signals,_=observed()
        self.records=[record(s) for s in self.signals]
        self.r=FakeRedis();self.lua=ProductionRedisLua(self.r)
    def test_primitive_validates_then_one_atomic_call(self):
        result=self.lua._atomic_eb_batch_testonly('W',self.records,1)
        self.assertEqual(result,{'inserted':2,'already_identical':0})
        self.assertEqual(len(self.r.calls),1)
        args=self.r.calls[0]
        self.assertEqual(args[1],4)
        self.assertEqual(args[2:4],(self.lua.leader_key,self.lua.generation_key))
    def test_production_entrypoint_requires_coverage_permit(self):
        with self.assertRaisesRegex(AtomicConflict,'COVERAGE_PERMIT_REJECTED'):
            self.lua.atomic_recovery_eb('W',self.records,1)
        self.assertEqual(self.r.calls,[])
    def test_duplicate_key_rejected_before_redis(self):
        with self.assertRaisesRegex(AtomicConflict,'DUPLICATE'):
            self.lua._atomic_eb_batch_testonly('W',self.records+self.records,1)
        self.assertEqual(self.r.calls,[])
    def test_wrong_generation_and_owner_rejected_before_redis(self):
        for change in ({'leader_generation':2},{'worker_instance_id':'other'}):
            rows=copy.deepcopy(self.records);rows[0].update(change)
            with self.assertRaisesRegex(AtomicConflict,'LEADERSHIP_MISMATCH'):
                self.lua._atomic_eb_batch_testonly('W',rows,1)
        self.assertEqual(self.r.calls,[])
    def test_non_eb_and_invalid_schema_rejected_before_redis(self):
        rows=copy.deepcopy(self.records);rows[0]['record_type']='trade'
        with self.assertRaisesRegex(AtomicConflict,'RECORD_TYPE'):
            self.lua._atomic_eb_batch_testonly('W',rows,1)
        rows=copy.deepcopy(self.records);del rows[0]['bar_end_ts']
        with self.assertRaisesRegex(AtomicConflict,'SCHEMA'):
            self.lua._atomic_eb_batch_testonly('W',rows,1)
        self.assertEqual(self.r.calls,[])
    def test_invalid_batch_size_and_bool_generation(self):
        for rows,g in (([],1),(self.records*41,1),(self.records,True)):
            with self.assertRaisesRegex(AtomicConflict,'INVALID_EB_BATCH'):
                self.lua._atomic_eb_batch_testonly('W',rows,g)
        self.assertEqual(self.r.calls,[])

if __name__=='__main__':unittest.main()
