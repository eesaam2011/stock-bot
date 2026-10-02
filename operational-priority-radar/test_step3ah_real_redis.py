"""Step3AH: actual Redis 7 finalization-to-fenced-E/B integration."""
import unittest

from recovery_eb_committer import FencedEBRecoveryCommitter
from recovery_session_committer import commit_finalized_eb_session
from redis_lua_production import ProductionRedisLua
from state_store import record_key
from test_step2n_real_redis import local_test_redis
from test_step3ah_recovery_session_committer import SYMBOLS, finalized
from test_step3t_recovery_commit_permit import Leadership


class TestRealRedisRecoverySessionCommit(unittest.TestCase):
    def setUp(self):
        self.r = local_test_redis()
        self.lua = ProductionRedisLua(self.r)
        self.value = finalized()
        self.records = list(self.value["record_batches"][0])
        self.keys = [record_key(record) for record in self.records]
        self.r.delete(*self.keys, self.lua.leader_key, self.lua.generation_key)
        ok, generation = self.lua.acquire("W", 30)
        self.assertTrue(ok); self.assertEqual(generation, 1)

    def tearDown(self):
        self.r.delete(*self.keys, self.lua.leader_key, self.lua.generation_key)

    def apply(self):
        leadership = Leadership()
        return commit_finalized_eb_session(
            self.value, FencedEBRecoveryCommitter(self.lua, leadership),
            leadership, SYMBOLS)

    def test_real_redis_commit_is_fenced_and_exact(self):
        result = self.apply()
        self.assertEqual((result["inserted"], result["already_identical"]),
                         (2, 0))
        self.assertEqual(
            sum(value is not None for value in self.r.mget(self.keys)), 2)

    def test_real_redis_exact_retry_is_idempotent(self):
        self.apply()
        result = self.apply()
        self.assertEqual((result["inserted"], result["already_identical"]),
                         (0, 2))


if __name__ == "__main__":
    unittest.main()
