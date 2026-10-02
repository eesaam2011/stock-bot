"""Step3I mandatory actual Redis 7 atomic MGET snapshot checks, DB15."""
import unittest
from production_recovery import RedisCanonicalReader,RecoveryFailure
from recovery_canonical_audit import audit_session_canonical,CanonicalAuditUnsafe
from test_step2n_real_redis import local_test_redis
from test_step3h_canonical_audit import observed,record,SESSION,NOW
from state_store import key_early_core,key_base_ready,canonical_json

class TestRealRedisAtomicCanonicalSnapshot(unittest.TestCase):
    def setUp(self):
        self.r=local_test_redis()
        self.keys=[key_early_core(SESSION,"A"),key_base_ready(SESSION,"A")]
        self.r.delete(*self.keys)
        self.signals,_=observed()
        self.reader=RedisCanonicalReader(self.r)
    def tearDown(self):self.r.delete(*self.keys)
    def audit(self):
        return audit_session_canonical(self.signals,self.reader,
            session=SESSION,symbols=["A"],as_of=NOW,
            require_atomic_snapshot=True)
    def test_one_mget_returns_both_matching_keys_unchanged(self):
        for s in self.signals:
            self.r.set(self.keys[0 if s.kind=="E" else 1],
                       canonical_json(record(s)))
        before=self.r.mget(self.keys)
        a=self.audit()
        self.assertEqual(a["matched"],["A:E","A:B"])
        self.assertTrue(a["multi_key_snapshot_atomic"])
        self.assertEqual(a["snapshot_scope"],"EB_KEYS_ONLY")
        self.assertEqual(self.r.mget(self.keys),before)
        self.assertEqual(a["canonical_writes"],0)
    def test_mget_detects_missing_key_without_backfill(self):
        s=next(x for x in self.signals if x.kind=="E")
        self.r.set(self.keys[0],canonical_json(record(s)))
        a=self.audit()
        self.assertEqual(a["matched"],["A:E"])
        self.assertEqual(a["observed_without_canonical"],["A:B"])
        self.assertIsNone(self.r.get(self.keys[1]))
        self.assertFalse(a["canonical_backfill_authorized"])
    def test_mget_invalid_json_fails_closed_no_mutation(self):
        self.r.set(self.keys[0],"{bad json")
        before=self.r.mget(self.keys)
        with self.assertRaises(CanonicalAuditUnsafe):self.audit()
        self.assertEqual(self.r.mget(self.keys),before)

if __name__=="__main__":unittest.main()
