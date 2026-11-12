"""Redis 7: restart-safe revisions and atomic E/B inhibition, synthetic inputs."""
import unittest
from durable_revision_journal import DurableRevisionJournal
from revision_recovery_inbox import RevisionInboxUnsafe
from redis_lua_production import ProductionRedisLua,LeaseLost,AtomicConflict
from production_recovery import ProductionStartupRecovery
from test_step2n_real_redis import local_test_redis
from test_step3t_recovery_commit_permit import Leadership
from test_step3az_revision_handoff import terminal,next_revision
from test_step3z_native_session_recovery_batches import REST,START,END,AS_OF
from test_step3ah_recovery_session_committer import finalized,SYMBOLS
from recovery_eb_committer import FencedEBRecoveryCommitter
from recovery_session_committer import commit_finalized_eb_session
from state_store import record_key


class TestDurableRevisionRecovery(unittest.TestCase):
    def setUp(self):
        self.r=local_test_redis();self.lua=ProductionRedisLua(self.r)
        self.leader=Leadership();self.journal=DurableRevisionJournal(self.lua,self.leader)
        self.value=finalized();self.keys=[record_key(row) for b in self.value['record_batches'] for row in b]
        self.r.delete(self.lua.leader_key,self.lua.generation_key,self.journal.key,*self.keys)
        self.assertEqual(self.lua.acquire('W',30),(True,1))
    def tearDown(self):
        self.r.delete(self.lua.leader_key,self.lua.generation_key,self.journal.key,*self.keys)
    def rec(self):
        value=ProductionStartupRecovery(None,REST(),None,'2026-09-24',['AAPL'],
            session_start=START,now_fn=lambda:AS_OF)
        value.durable_revision_journal=DurableRevisionJournal(self.lua,self.leader)
        return value
    def commit(self):
        return commit_finalized_eb_session(self.value,FencedEBRecoveryCommitter(self.lua,self.leader),
                                          self.leader,SYMBOLS)
    def test_restart_retains_multiple_revisions_and_exact_retry(self):
        self.journal.record_terminal(terminal());self.journal.record_terminal(next_revision())
        self.assertFalse(self.journal.record_terminal(terminal())['inserted'])
        other=DurableRevisionJournal(self.lua,self.leader)
        self.assertEqual(len(other.snapshot()),2)
        self.assertEqual(self.r.ttl(other.key),-1)
        self.assertEqual(self.rec().run()['reason'],'UNRESOLVED_DURABLE_REVISIONS')
    def test_generation_loss_rejects_evidence_write(self):
        self.r.set(self.lua.generation_key,2)
        with self.assertRaises(LeaseLost):self.journal.record_terminal(terminal())
        self.assertFalse(self.r.exists(self.journal.key))
    def test_overflow_is_persistent_blocker(self):
        small=DurableRevisionJournal(self.lua,self.leader,max_records=1)
        small.record_terminal(terminal())
        with self.assertRaises(RevisionInboxUnsafe):small.record_terminal(next_revision())
        with self.assertRaisesRegex(RevisionInboxUnsafe,'OVERFLOW'):self.journal.snapshot()
        self.assertEqual(self.r.hget(self.journal.key,'count'),'1')
    def test_replay_after_restart_stays_candidate(self):
        self.journal.record_terminal(terminal())
        result=self.rec().preview_pending_revision(self.leader,session_end=END)
        self.assertTrue(result['audit']['native_session_replayed'])
        self.assertFalse(result['audit']['eb_persistence_authorized'])
        self.assertEqual(len(self.journal.snapshot()),1)
    def test_pending_revision_blocks_eb_at_atomic_write(self):
        self.journal.record_terminal(terminal())
        with self.assertRaisesRegex(AtomicConflict,'-60'):self.commit()
        self.assertTrue(all(x is None for x in self.r.mget(self.keys)))
    def test_clean_proven_session_still_commits_idempotently(self):
        self.assertEqual(self.commit()['inserted'],len(self.keys))
        self.assertEqual(self.commit()['already_identical'],len(self.keys))
    def test_persistence_failure_cancels_recovery(self):
        rec=self.rec();self.r.set(self.lua.generation_key,2)
        with self.assertRaises(LeaseLost):rec.persist_revision_terminal(terminal())
        self.assertTrue(rec.cancel_event.is_set());self.assertTrue(rec.revision_persistence_failed)
    def test_corrupted_journal_cannot_be_read_as_clean(self):
        self.r.hset(self.journal.key,mapping={'count':'1','d:bad':'{}'})
        with self.assertRaises(RevisionInboxUnsafe):self.journal.snapshot()
    def test_revision_arrives_between_preflight_and_lua(self):
        journal=self.journal
        class RacingLua(ProductionRedisLua):
            def _atomic_eb_batch_testonly(inner,*args,**kwargs):
                journal.record_terminal(terminal())
                return super()._atomic_eb_batch_testonly(*args,**kwargs)
        with self.assertRaisesRegex(AtomicConflict,'-60'):
            commit_finalized_eb_session(self.value,
                FencedEBRecoveryCommitter(RacingLua(self.r),self.leader),self.leader,SYMBOLS)
        self.assertTrue(all(x is None for x in self.r.mget(self.keys)))
    def test_composed_disconnect_persists_then_restart_blocks(self):
        import asyncio
        from production_composition import compose_shadow_runtime
        from operational_priority_radar import WorkerConfig
        from test_step2p_untrusted_quarantine import FakeEarly,FakeBase
        rec=self.rec()
        supervisor=compose_shadow_runtime(WorkerConfig(True,'redis://unused','k','s'),
            redis_client=self.r,websocket_connector=object(),recovery=rec,
            early_core=FakeEarly(),base_ready=FakeBase(),symbols=['AAPL'])
        supervisor.websocket_runtime.last_epoch_diagnostic=terminal()
        asyncio.run(supervisor.websocket_runtime.on_disconnect())
        self.assertEqual(len(self.journal.snapshot()),1)
        self.assertEqual(self.rec().run()['reason'],'UNRESOLVED_DURABLE_REVISIONS')
    def test_new_durable_revision_during_replay_rejects_result(self):
        from production_recovery import RecoveryFailure
        self.journal.record_terminal(terminal());rec=self.rec();journal=self.journal
        class RacingREST(REST):
            def native_recovery_batch_audited(inner,*args,**kwargs):
                value=super().native_recovery_batch_audited(*args,**kwargs)
                journal.record_terminal(next_revision());return value
        rec.rest=RacingREST()
        with self.assertRaisesRegex(RecoveryFailure,'CHANGED_DURING_REPLAY'):
            rec.preview_pending_revision(self.leader,session_end=END)
    def test_full_native_replay_to_redis_is_blocked_by_revision(self):
        from test_step3av_verified_recovery import synthetic_evidence
        from test_step3ag_recovery_session_finalizer import scope_snapshot
        from recovery_session_finalizer import finalize_empty_scope_session_recovery
        from verified_session_recovery import recover_and_commit_verified_empty_session
        scope=scope_snapshot(self.leader)
        value=finalize_empty_scope_session_recovery(synthetic_evidence(),REST(),['A'],scope,self.leader,as_of=AS_OF)
        keys=[record_key(row) for batch in value['record_batches'] for row in batch]
        self.keys.extend(k for k in keys if k not in self.keys);self.r.delete(*keys)
        self.journal.record_terminal(terminal())
        with self.assertRaisesRegex(AtomicConflict,'-60'):
            recover_and_commit_verified_empty_session(synthetic_evidence(),REST(),['A'],scope,self.leader,self.lua,as_of=AS_OF)
        self.assertTrue(all(x is None for x in self.r.mget(keys)))
