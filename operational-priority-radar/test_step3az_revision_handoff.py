import unittest
from copy import deepcopy
from revision_recovery_inbox import RevisionRecoveryInbox,RevisionInboxUnsafe
from test_step3ax_revision_rebuild import diagnostic
from sip_semantic_digest import canonical_sha256
from production_recovery import ProductionStartupRecovery
from test_step3t_recovery_commit_permit import Leadership
from test_step3z_native_session_recovery_batches import REST,START,END,AS_OF


def terminal(value=None):
    value=value or diagnostic()
    return {'epoch':value['epoch'],'capture_before_teardown':{'revision_diagnostic':value}}


def next_revision():
    value=diagnostic();value['epoch']=2;value.pop('diagnostic_sha256')
    value['diagnostic_sha256']=canonical_sha256(value)
    return terminal(value)


class TestRevisionHandoff(unittest.TestCase):
    def test_nonrevision_disconnect_does_not_erase_and_copies_are_isolated(self):
        inbox=RevisionRecoveryInbox();value=terminal();inbox.observe_terminal(value)
        inbox.observe_terminal({'epoch':2,'capture_before_teardown':{}})
        value['capture_before_teardown'].clear()
        saved=inbox.snapshot();saved['frame']['S']='other'
        self.assertEqual(inbox.snapshot()['frame']['S'],'AAPL')

    def test_exact_repeat_idempotent_distinct_revision_blocks(self):
        inbox=RevisionRecoveryInbox();value=terminal()
        inbox.observe_terminal(value);inbox.observe_terminal(value)
        self.assertEqual(inbox.snapshot()['epoch'],1)
        inbox.observe_terminal(next_revision())
        with self.assertRaisesRegex(RevisionInboxUnsafe,'MULTIPLE_UNRESOLVED'):inbox.snapshot()

    def test_mismatched_epoch_or_digest_blocks(self):
        for mode in ('epoch','digest'):
            with self.subTest(mode=mode):
                inbox=RevisionRecoveryInbox();value=terminal()
                if mode=='epoch':value['epoch']=99
                else:value['capture_before_teardown']['revision_diagnostic']['frame']['p']=99
                inbox.observe_terminal(value)
                with self.assertRaisesRegex(RevisionInboxUnsafe,'INVALID'):inbox.snapshot()

    def recovery(self):
        return ProductionStartupRecovery(None,REST(),None,'2026-09-24',['AAPL'],
            session_start=START,now_fn=lambda:AS_OF)

    def test_pending_production_replay_preserves_evidence_and_trust(self):
        rec=self.recovery();rec.retain_revision_terminal(terminal());rec.on_disconnect()
        result=rec.preview_pending_revision(Leadership(),session_end=END)
        self.assertTrue(result['audit']['native_session_replayed'])
        self.assertFalse(rec.ready_after_stream());self.assertFalse(rec.continuity_verified(1))
        self.assertEqual(rec.revision_inbox.snapshot()['epoch'],1)

    def test_second_revision_during_rest_rejects_candidate(self):
        rec=self.recovery();rec.retain_revision_terminal(terminal())
        class ConcurrentREST(REST):
            def native_recovery_batch_audited(self,*a,**kw):
                value=super().native_recovery_batch_audited(*a,**kw)
                rec.retain_revision_terminal(next_revision());return value
        rec.rest=ConcurrentREST()
        with self.assertRaisesRegex(RevisionInboxUnsafe,'MULTIPLE_UNRESOLVED'):
            rec.preview_pending_revision(Leadership(),session_end=END)

    def test_oversized_handoff_rejected(self):
        inbox=RevisionRecoveryInbox();value=terminal()
        value['capture_before_teardown']['revision_diagnostic']['extra']='x'*17000
        inbox.observe_terminal(value)
        with self.assertRaisesRegex(RevisionInboxUnsafe,'INVALID'):inbox.snapshot()


class TestComposedRevisionHandoff(unittest.IsolatedAsyncioTestCase):
    async def test_actual_disconnect_callback_retains_terminal(self):
        from production_composition import compose_shadow_runtime
        from operational_priority_radar import WorkerConfig
        from test_step2p_untrusted_quarantine import Redis,FakeEarly,FakeBase
        rec=ProductionStartupRecovery(None,REST(),None,'2026-09-24',['AAPL'],
            session_start=START,now_fn=lambda:AS_OF)
        supervisor=compose_shadow_runtime(WorkerConfig(True,'redis://unused','k','s'),
            redis_client=Redis(),websocket_connector=object(),recovery=rec,
            early_core=FakeEarly(),base_ready=FakeBase(),symbols=['AAPL'])
        ws=supervisor.websocket_runtime
        ws.last_epoch_diagnostic=terminal()
        supervisor.decision_pipeline._entry_opportunities['AAPL']={'stale':True}
        await ws.on_disconnect()
        self.assertEqual(rec.revision_inbox.snapshot()['epoch'],1)
        self.assertEqual(supervisor.decision_pipeline._entry_opportunities,{})
        ws.last_epoch_diagnostic={'epoch':2,'capture_before_teardown':{}}
        await ws.on_disconnect()
        self.assertEqual(rec.revision_inbox.snapshot()['epoch'],1)
        self.assertFalse(rec.ready_after_stream())
