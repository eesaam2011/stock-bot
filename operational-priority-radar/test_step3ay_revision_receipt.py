import unittest
from datetime import timedelta
from sip_epoch_capture import BoundedEpochCapture,EpochCaptureError
from sip_semantic_digest import canonical_sha256
from revision_native_rebuild import rebuild_revision_candidate,RevisionRebuildUnsafe
from test_step3ax_revision_rebuild import diagnostic
from test_step3aq_trade_revision_audit import cancel
from test_step3t_recovery_commit_permit import Leadership
from test_step3z_native_session_recovery_batches import REST,START,END,AS_OF


class TestRevisionReceipt(unittest.TestCase):
    def call(self,value,rest=None):
        return rebuild_revision_candidate(value,rest or REST(),Leadership(),session='2026-09-24',
             session_start=START,session_end=END,as_of=AS_OF)

    def test_receipt_is_bound_to_candidate(self):
        value=diagnostic();result=self.call(value)
        self.assertEqual(result['audit']['revision_received_at_utc'],END.isoformat())
        self.assertEqual(result['audit']['revision_diagnostic_sha256'],value['diagnostic_sha256'])
        self.assertFalse(result['audit']['rest_revision_applied_proven'])

    def test_late_receipt_old_event_time_blocks_fetch(self):
        value=diagnostic();value['received_at_utc']=(AS_OF+timedelta(seconds=1)).isoformat()
        value.pop('diagnostic_sha256');value['diagnostic_sha256']=canonical_sha256(value)
        class NoREST:
            def native_recovery_batch_audited(self,*a,**k):raise AssertionError('REST called')
        with self.assertRaisesRegex(RevisionRebuildUnsafe,'RECEIVED_AFTER'):
            self.call(value,NoREST())

    def test_receipt_or_epoch_tampering_rejected(self):
        for key,new in [('received_at_utc',START.isoformat()),('epoch',999)]:
            with self.subTest(key=key):
                value=diagnostic();value[key]=new
                with self.assertRaisesRegex(RevisionRebuildUnsafe,'DIAGNOSTIC_DIGEST'):self.call(value)

    def test_legacy_diagnostic_without_receipt_binding_is_rejected(self):
        value=diagnostic();value.pop('diagnostic_sha256');value.pop('received_at_utc')
        with self.assertRaisesRegex(RevisionRebuildUnsafe,'DIAGNOSTIC_DIGEST'):self.call(value)

    def test_naive_receipt_invalidates_capture(self):
        capture=BoundedEpochCapture();capture.start(1)
        with self.assertRaisesRegex(EpochCaptureError,'RECEIVED_AT_INVALID'):
            capture.ingest(1,cancel(),received_at=END.replace(tzinfo=None))
        self.assertEqual(capture.phase,capture.INVALID)
