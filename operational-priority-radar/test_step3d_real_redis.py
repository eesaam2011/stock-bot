"""Mandatory Step3D integration: actual Redis 7 Lua all-or-nothing recovery."""
import unittest,json
from redis_lua_production import ProductionRedisLua,LeaseLost,AtomicConflict
from test_step2n_real_redis import local_test_redis,raw

class TestRealRedisAtomicRecovery(unittest.TestCase):
    def setUp(self):
        self.r=local_test_redis()
        self.lua=ProductionRedisLua(self.r)
        self.keys=["opr:step3d:test:trade","opr:step3d:test:t1",
                   "opr:step3d:test:t2"]
        self.r.delete(*self.keys,self.lua.leader_key,self.lua.generation_key)
        ok,g=self.lua.acquire("W",30)
        self.assertTrue(ok);self.assertEqual(g,1)
        self.r.set(self.keys[0],raw("ACTIVE_PRE_T1"))
    def tearDown(self):
        self.r.delete(*self.keys,self.lua.leader_key,self.lua.generation_key)
    def commit(self,**kwargs):
        return self.lua.atomic_trade_recovery(
            "W",self.keys[0],raw("ACTIVE_PRE_T1"),raw("CLOSED_T2"),
            [(self.keys[1],raw("T1")),(self.keys[2],raw("T2"))],1,**kwargs)
    def test_one_transaction_final_trade_and_both_outboxes(self):
        self.assertTrue(self.commit())
        self.assertEqual([json.loads(self.r.get(k))["state"] for k in self.keys],
                         ["CLOSED_T2","T1","T2"])
    def test_second_outbox_conflict_leaves_first_absent_and_trade_unchanged(self):
        self.r.set(self.keys[2],"conflicting payload")
        with self.assertRaisesRegex(AtomicConflict,"RECOVERY_CONFLICT:-30"):
            self.commit()
        self.assertEqual(self.r.get(self.keys[0]),raw("ACTIVE_PRE_T1"))
        self.assertIsNone(self.r.get(self.keys[1]))
        self.assertEqual(self.r.get(self.keys[2]),"conflicting payload")
    def test_lease_generation_change_rejects_all_writes(self):
        self.r.set(self.lua.generation_key,"2")
        with self.assertRaises(LeaseLost):
            self.commit()
        self.assertEqual(self.r.get(self.keys[0]),raw("ACTIVE_PRE_T1"))
        self.assertIsNone(self.r.get(self.keys[1]))
        self.assertIsNone(self.r.get(self.keys[2]))
    def test_owner_takeover_rejects_all_writes(self):
        self.r.set(self.lua.leader_key,"NEW_OWNER")
        with self.assertRaises(LeaseLost):
            self.commit()
        self.assertEqual(self.r.get(self.keys[0]),raw("ACTIVE_PRE_T1"))
        self.assertIsNone(self.r.get(self.keys[1]))
        self.assertIsNone(self.r.get(self.keys[2]))
    def test_stale_expected_trade_rejects_all_writes(self):
        self.r.set(self.keys[0],raw("ACTIVE_POST_T1"))
        with self.assertRaisesRegex(AtomicConflict,"RECOVERY_CONFLICT:-20"):
            self.commit()
        self.assertEqual(self.r.get(self.keys[0]),raw("ACTIVE_POST_T1"))
        self.assertIsNone(self.r.get(self.keys[1]))
        self.assertIsNone(self.r.get(self.keys[2]))
    def test_invalid_outbox_keys_and_payload_generation_rejected_preflight(self):
        with self.assertRaisesRegex(AtomicConflict,"INVALID_RECOVERY_OUTBOX_KEYS"):
            self.lua.atomic_trade_recovery("W",self.keys[0],
                raw("ACTIVE_PRE_T1"),raw("CLOSED_T2"),
                [(self.keys[1],raw("T1")),(self.keys[1],raw("T2"))],1)
        with self.assertRaisesRegex(AtomicConflict,"PAYLOAD_GENERATION_MISMATCH"):
            self.lua.atomic_trade_recovery("W",self.keys[0],
                raw("ACTIVE_PRE_T1"),raw("CLOSED_T2"),
                [(self.keys[1],raw("T1",2))],1)
        self.assertEqual(self.r.get(self.keys[0]),raw("ACTIVE_PRE_T1"))
        self.assertIsNone(self.r.get(self.keys[1]))

if __name__=="__main__":unittest.main()
