"""Synthetic bar replay to actual Redis; never a live market-session claim."""
import unittest
from datetime import timedelta
from empty_trade_status_disposition import snapshot_empty_active_trade_scope
from production_recovery import RedisCanonicalReader
from redis_lua_production import ProductionRedisLua, LeaseLost
from state_store import record_key
from test_step2n_real_redis import local_test_redis
from test_step3ag_recovery_session_finalizer import Pipeline
from test_step3av_verified_recovery import synthetic_evidence
from test_step3t_recovery_commit_permit import Leadership
from test_step3z_native_session_recovery_batches import REST, START, AS_OF
from verified_session_recovery import recover_and_commit_verified_empty_session


class TestVerifiedRecoveryRealRedis(unittest.TestCase):
    def setUp(self):
        self.r = local_test_redis()
        self.lua = ProductionRedisLua(self.r)
        self.keys = []
        self.r.delete(self.lua.leader_key, self.lua.generation_key)
        self.assertEqual(self.lua.acquire('W', 30), (True, 1))
        self.leader = Leadership()
        self.scope = snapshot_empty_active_trade_scope(
            RedisCanonicalReader(self.r), self.leader, '2026-09-24',
            pipeline=Pipeline(self.r), captured_at=START-timedelta(minutes=10))
        # Derive exact cleanup keys through the same write-free finalizer.
        from recovery_session_finalizer import finalize_empty_scope_session_recovery
        value = finalize_empty_scope_session_recovery(
            synthetic_evidence(), REST(), ['A'], self.scope, self.leader, as_of=AS_OF)
        self.keys = [record_key(row) for batch in value['record_batches'] for row in batch]
        self.assertTrue(self.keys)
        self.r.delete(*self.keys)

    def tearDown(self):
        self.r.delete(*self.keys, self.lua.leader_key, self.lua.generation_key)

    def apply(self, lua=None):
        return recover_and_commit_verified_empty_session(
            synthetic_evidence(), REST(), ['A'], self.scope, self.leader,
            lua or self.lua, as_of=AS_OF)

    def test_actual_replay_persists_nonempty_records(self):
        result = self.apply()
        self.assertEqual(result['commit']['inserted'], len(self.keys))
        self.assertTrue(all(self.r.mget(self.keys)))
        self.assertFalse(result['direct_handoff_authorized'])

    def test_exact_full_path_retry_is_idempotent(self):
        self.apply()
        result = self.apply()
        self.assertEqual(result['commit']['inserted'], 0)
        self.assertEqual(result['commit']['already_identical'], len(self.keys))

    def test_generation_change_at_write_blocks_all_records(self):
        class ChangedGenerationLua(ProductionRedisLua):
            def atomic_recovery_eb(inner, *args, **kwargs):
                inner.r.set(inner.generation_key, 2)
                return super().atomic_recovery_eb(*args, **kwargs)
        with self.assertRaises(LeaseLost):
            self.apply(ChangedGenerationLua(self.r))
        self.assertTrue(all(value is None for value in self.r.mget(self.keys)))
