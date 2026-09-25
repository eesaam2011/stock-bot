import unittest
from sip_epoch_capture import BoundedEpochCapture, EpochCaptureError
from revision_native_rebuild import rebuild_revision_candidate, RevisionRebuildUnsafe
from production_recovery import ProductionStartupRecovery, RecoveryFailure
from test_step3aq_trade_revision_audit import trade,cancel
from test_step3t_recovery_commit_permit import Leadership
from test_step3z_native_session_recovery_batches import REST,START,END,AS_OF


def diagnostic():
    capture=BoundedEpochCapture();capture.start(1)
    capture.ingest(1,trade(t=START.isoformat()))
    try:capture.ingest(1,cancel(t=END.isoformat()))
    except EpochCaptureError:pass
    return capture.snapshot()['revision_diagnostic']


class TestRevisionNativeRebuild(unittest.TestCase):
    def call(self, value=None,rest=None,leader=None,**kw):
        return rebuild_revision_candidate(value or diagnostic(),rest or REST(),leader or Leadership(),
            session='2026-09-24',session_start=START,session_end=END,as_of=AS_OF,**kw)

    def test_actual_native_replay_without_authority(self):
        result=self.call()
        self.assertGreater(result['audit']['signal_count'],0)
        self.assertFalse(result['audit']['rest_revision_applied_proven'])
        self.assertFalse(result['audit']['eb_persistence_authorized'])
        for signal in result['signals']:self.assertGreaterEqual(signal.decision_available_ts,AS_OF)

    def test_tampered_pair_cannot_fetch(self):
        class NoREST:
            def native_recovery_batch_audited(self,*a,**k):raise AssertionError('REST called')
        value=diagnostic();value['original_trade_evidence']['pair_sha256']='bad'
        with self.assertRaisesRegex(RevisionRebuildUnsafe,'DIGEST'):self.call(value,NoREST())

    def test_missing_original_cannot_fetch(self):
        value=diagnostic();value['original_trade_evidence']['matched_within_retained_window']=False
        with self.assertRaisesRegex(RevisionRebuildUnsafe,'ORIGINAL'):self.call(value)

    def test_leadership_change_during_fetch(self):
        leader=Leadership()
        class ChangedREST(REST):
            def native_recovery_batch_audited(self,*a,**k):
                result=super().native_recovery_batch_audited(*a,**k)
                from types import SimpleNamespace
                leader.require_current=lambda:SimpleNamespace(worker_instance_id='successor',leader_generation=2)
                return result
        with self.assertRaisesRegex(RevisionRebuildUnsafe,'LEADERSHIP_CHANGED'):
            self.call(rest=ChangedREST(),leader=leader)

    def test_cancel_during_rest_and_production_adapter(self):
        recovery=ProductionStartupRecovery(None,REST(),None,'2026-09-24',['AAPL'],
            session_start=START,now_fn=lambda:AS_OF)
        result=recovery.preview_revision_rebuild(diagnostic(),Leadership(),session_end=END)
        self.assertTrue(result['audit']['native_session_replayed'])
        self.assertFalse(recovery._base_reconciled)
        class CancelREST(REST):
            def native_recovery_batch_audited(self,*a,**k):
                result=super().native_recovery_batch_audited(*a,**k);recovery.cancel();return result
        recovery.rest=CancelREST()
        with self.assertRaisesRegex(RecoveryFailure,'CANCELLED'):
            recovery.preview_revision_rebuild(diagnostic(),Leadership(),session_end=END)

    def test_production_scope_rejects_other_symbol_before_rest(self):
        recovery=ProductionStartupRecovery(None,None,None,'2026-09-24',['MSFT'],
            session_start=START,now_fn=lambda:AS_OF)
        with self.assertRaisesRegex(RecoveryFailure,'OUTSIDE_SCOPE'):
            recovery.preview_revision_rebuild(diagnostic(),Leadership(),session_end=END)
