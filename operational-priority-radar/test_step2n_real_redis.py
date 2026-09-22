"""Step 2N: actual Redis Lua tests, only on a dedicated local Redis DB 15.

Never point OPR_REDIS_TEST_URL at production; nonlocal hosts are rejected.
"""
import json
import os
import unittest
from urllib.parse import urlparse
from redis_lua_production import ProductionRedisLua,LeaseLost,AtomicConflict
from production_runtime_state import RedisCanonicalBackend

URL=os.getenv("OPR_REDIS_TEST_URL")
def local_test_redis():
    if not URL:
        raise unittest.SkipTest("OPR_REDIS_TEST_URL not set; real Redis tests not run")
    parsed=urlparse(URL)
    if parsed.hostname not in {"127.0.0.1","localhost"} or parsed.path!="/15":
        raise unittest.SkipTest("real Redis tests require local dedicated database /15")
    import redis
    r=redis.Redis.from_url(URL,decode_responses=True,socket_timeout=2)
    r.ping()
    return r

def raw(state,g=1):
    return json.dumps({"state":state,"leader_generation":g},sort_keys=True)

class TestActualRedisFencing(unittest.TestCase):
    def setUp(self):
        self.r=local_test_redis()
        self.lua=ProductionRedisLua(self.r)
        self.r.delete(self.lua.leader_key,self.lua.generation_key)
        ok,g=self.lua.acquire("W",30)
        self.assertTrue(ok)
        self.assertEqual(g,1)
        self.keys=["opr:step2n:test:opp","opr:step2n:test:trade",
                   "opr:step2n:test:outbox","opr:step2n:test:first"]
        self.r.delete(*self.keys)
    def tearDown(self):
        self.r.delete(*self.keys,self.lua.leader_key,self.lua.generation_key)
    def test_first_write_checks_owner_and_generation_atomically(self):
        backend=RedisCanonicalBackend(self.r)
        self.assertTrue(backend.set_if_absent_fenced(self.keys[3],"first","W",1))
        self.assertFalse(backend.set_if_absent_fenced(self.keys[3],"second","W",1))
        self.r.set(self.lua.generation_key,"2")
        with self.assertRaises(LeaseLost):
            backend.set_if_absent_fenced(self.keys[3],"third","W",1)
        self.assertEqual(self.r.get(self.keys[3]),"first")
    def test_entry_writes_three_records_in_one_commit(self):
        opp,trade,out=self.keys[:3]
        self.r.set(opp,raw("CONFLUENCE_VALID"))
        self.assertTrue(self.lua.atomic_entry("W",opp,raw("CONFLUENCE_VALID"),
            raw("ENTRY_COMMITTED"),trade,raw("ACTIVE_PRE_T1"),out,
            raw("PENDING"),1))
        self.assertEqual([json.loads(self.r.get(k))["state"] for k in (opp,trade,out)],
                         ["ENTRY_COMMITTED","ACTIVE_PRE_T1","PENDING"])
    def test_stale_generation_cannot_commit_entry_even_same_owner(self):
        opp,trade,out=self.keys[:3]
        self.r.set(opp,raw("CONFLUENCE_VALID"))
        self.r.set(self.lua.generation_key,"2")
        with self.assertRaises(LeaseLost):
            self.lua.atomic_entry("W",opp,raw("CONFLUENCE_VALID"),
                raw("ENTRY_COMMITTED"),trade,raw("ACTIVE_PRE_T1"),out,
                raw("PENDING"),1)
        self.assertEqual(self.r.get(opp),raw("CONFLUENCE_VALID"))
        self.assertIsNone(self.r.get(trade))
        self.assertIsNone(self.r.get(out))
    def test_stale_generation_cannot_commit_trade_event(self):
        _,trade,out=self.keys[:3]
        self.r.set(trade,raw("ACTIVE_PRE_T1"))
        self.r.set(self.lua.generation_key,"2")
        with self.assertRaises(LeaseLost):
            self.lua.atomic_trade_event("W",trade,raw("ACTIVE_PRE_T1"),
                raw("CLOSED_STOP"),out,raw("PENDING"),1)
        self.assertEqual(self.r.get(trade),raw("ACTIVE_PRE_T1"))
        self.assertIsNone(self.r.get(out))
    def test_payload_generation_mismatch_rejected_before_redis(self):
        opp,trade,out=self.keys[:3]
        self.r.set(opp,raw("CONFLUENCE_VALID"))
        with self.assertRaisesRegex(AtomicConflict,"PAYLOAD_GENERATION_MISMATCH"):
            self.lua.atomic_entry("W",opp,raw("CONFLUENCE_VALID"),
                raw("ENTRY_COMMITTED",2),trade,raw("ACTIVE_PRE_T1"),out,
                raw("PENDING"),1)
        self.assertIsNone(self.r.get(trade))
    def test_owner_takeover_blocks_trade_event(self):
        _,trade,out=self.keys[:3]
        self.r.set(trade,raw("ACTIVE_PRE_T1"))
        self.r.set(self.lua.leader_key,"NEW_OWNER")
        with self.assertRaises(LeaseLost):
            self.lua.atomic_trade_event("W",trade,raw("ACTIVE_PRE_T1"),
                raw("CLOSED_STOP"),out,raw("PENDING"),1)
        self.assertEqual(self.r.get(trade),raw("ACTIVE_PRE_T1"))
        self.assertIsNone(self.r.get(out))

if __name__=="__main__":unittest.main()
