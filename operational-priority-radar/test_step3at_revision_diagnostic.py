import json
import unittest
from sip_epoch_capture import BoundedEpochCapture, EpochCaptureError
from test_step3aq_trade_revision_audit import cancel, correction


class TestRevisionDiagnostic(unittest.TestCase):
    def test_revision_identity_survives_invalidation_without_ack(self):
        for message in (cancel(), correction()):
            with self.subTest(kind=message['T']):
                capture=BoundedEpochCapture();capture.start(7)
                with self.assertRaisesRegex(EpochCaptureError,'REVISION_UNRECONCILED'):
                    capture.ingest(7,message)
                snapshot=capture.snapshot()
                self.assertEqual(snapshot['phase'],'INVALID')
                diagnostic=snapshot['revision_diagnostic']
                self.assertEqual(diagnostic['frame'],message)
                self.assertEqual(diagnostic['epoch'],7)
                self.assertFalse(diagnostic['capture_ack_authorized'])
                message['S']='CHANGED'
                self.assertEqual(capture.snapshot()['revision_diagnostic']['frame']['S'],'AAPL')
                capture.start(8)
                self.assertNotIn('revision_diagnostic',capture.snapshot())

    def test_oversized_fields_and_unknown_secret_are_not_retained(self):
        capture=BoundedEpochCapture();capture.start(1)
        message=correction(S='X'*100000,cc=['X'*100000],api_secret='do-not-retain')
        with self.assertRaises(EpochCaptureError):capture.ingest(1,message)
        diagnostic=capture.snapshot()['revision_diagnostic']
        raw=json.dumps(diagnostic)
        self.assertLess(len(raw),4096)
        self.assertNotIn('do-not-retain',raw)
        self.assertEqual(diagnostic['invalid_or_oversized_fields'],['S','cc'])
