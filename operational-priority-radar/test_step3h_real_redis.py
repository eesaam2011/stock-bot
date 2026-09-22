"""Step3H: actual Redis 7 read-only canonical E/B audit, dedicated DB15."""
import copy,json,unittest
from recovery_canonical_audit import audit_session_canonical
from production_recovery import RedisCanonicalReader
from state_store import key_early_core,key_base_ready,canonical_json
from test_step2n_real_redis import local_test_redis
from test_step3h_canonical_audit import observed,record,SESSION,NOW

class TestRealRedisCanonicalAudit(unittest.TestCase):
    def setUp(self):
        self.r=local_test_redis()
        self.keys=[key_early_core(SESSION,"A"),key_base_ready(SESSION,"A")]
        self.r.delete(*self.keys)
        self.signals,_=observed()
        self.reader=RedisCanonicalReader(self.r)
    def tearDown(self):self.r.delete(*self.keys)
    def audit(self):
        return audit_session_canonical(self.signals,self.reader,
                                       session=SESSION,symbols=["A"],as_of=NOW)
    def test_matching_real_redis_records_remain_unchanged(self):
        for s in self.signals:
            key=self.keys[0 if s.kind=="E" else 1]
            self.r.set(key,canonical_json(record(s)))
        before=self.r.mget(self.keys)
        a=self.audit()
        self.assertEqual(a["matched"],["A:E","A:B"])
        self.assertEqual(a["divergent"],[])
        self.assertEqual(self.r.mget(self.keys),before)
        self.assertEqual(a["canonical_writes"],0)
        self.assertFalse(a["multi_key_snapshot_atomic"])
    def test_real_redis_divergence_reported_without_overwrite(self):
        for s in self.signals:
            rec=record(s)
            if s.kind=="E":rec["score"]+=.01
            self.r.set(self.keys[0 if s.kind=="E" else 1],
                       canonical_json(rec))
        before=self.r.mget(self.keys)
        a=self.audit()
        self.assertEqual(a["divergent"][0]["reason"],
                         "E_FROZEN_METRICS_DIVERGENCE")
        self.assertEqual(self.r.mget(self.keys),before)
        self.assertFalse(a["canonical_backfill_authorized"])
    def test_missing_real_redis_keys_are_not_backfilled(self):
        a=self.audit()
        self.assertEqual(a["observed_without_canonical"],["A:E","A:B"])
        self.assertEqual(self.r.mget(self.keys),[None,None])
        self.assertEqual(a["canonical_writes"],0)

if __name__=="__main__":unittest.main()
