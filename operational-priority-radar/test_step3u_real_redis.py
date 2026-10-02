"""Step3U real Redis tests for the fenced SIP receipt-chain Lua primitive."""
import hashlib
import unittest

from redis_lua_production import AtomicConflict, LeaseLost, ProductionRedisLua
from test_step2n_real_redis import local_test_redis


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class TestRealRedisSIPTransport(unittest.TestCase):
    def setUp(self):
        self.r = local_test_redis()
        self.lua = ProductionRedisLua(self.r)
        self.key = f"{self.lua.prefix}:test:sip_transport"
        self.r.delete(self.key, self.lua.leader_key, self.lua.generation_key)
        ok, generation = self.lua.acquire("W", 30)
        self.assertTrue(ok); self.assertEqual(generation, 1)

    def tearDown(self):
        self.r.delete(self.key, self.lua.leader_key, self.lua.generation_key)

    def commit(self, first=1, last=4, previous=None, batch=None, nxt=None):
        previous = previous or ("0" * 64)
        batch = batch or digest(f"batch:{first}:{last}")
        nxt = nxt or digest(f"chain:{previous}:{batch}")
        return self.lua.atomic_sip_transport_batch(
            "W", 1, self.key, schema="OPR_SIP_TRANSPORT_JOURNAL_V1",
            epoch=7, first_sequence=first, last_sequence=last,
            item_count=last-first+1, batch_sha256=batch,
            previous_chain_sha256=previous, next_chain_sha256=nxt,
            ttl_seconds=3600)

    def test_contiguous_commit_and_exact_retry(self):
        one_batch=digest("batch:1:4"); one_chain=digest(f"chain:{'0'*64}:{one_batch}")
        self.assertEqual(self.commit(batch=one_batch,nxt=one_chain),
                         {"inserted":True,"idempotent":False})
        before=self.r.hgetall(self.key)
        self.assertEqual(self.commit(batch=one_batch,previous=one_chain,nxt=one_chain),
                         {"inserted":False,"idempotent":True})
        self.assertEqual(self.r.hgetall(self.key),before)
        self.assertGreater(self.r.ttl(self.key),0)

    def test_gap_replay_and_conflicting_retry_are_rejected(self):
        first_batch=digest("batch:1:4"); chain=digest(f"chain:{'0'*64}:{first_batch}")
        self.commit(batch=first_batch,nxt=chain)
        for args in (
            dict(first=6,last=7,previous=chain),
            dict(first=1,last=4,previous=chain,batch=digest("conflict"),nxt=chain),
            dict(first=1,last=2,previous=chain),
        ):
            with self.subTest(args=args):
                with self.assertRaises(AtomicConflict): self.commit(**args)
        self.assertEqual(self.r.hget(self.key,"last_sequence"),"4")

    def test_owner_and_generation_fences_prevent_write(self):
        self.r.set(self.lua.leader_key,"SUCCESSOR")
        with self.assertRaises(LeaseLost): self.commit()
        self.assertFalse(self.r.exists(self.key))
        self.r.set(self.lua.leader_key,"W"); self.r.set(self.lua.generation_key,"2")
        with self.assertRaises(LeaseLost): self.commit()
        self.assertFalse(self.r.exists(self.key))

    def test_hash_contains_no_payload_fields(self):
        self.commit()
        state=self.r.hgetall(self.key)
        self.assertEqual(set(state),{
            "schema","last_sequence","item_count","chain_sha256",
            "last_batch_first","last_batch_sha256",
            "last_batch_chain_sha256","epoch"})


if __name__ == "__main__":
    unittest.main()
