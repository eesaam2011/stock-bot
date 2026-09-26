import json
import unittest
import asyncio
from websocket_runtime import WebSocketRuntime, WebSocketProtocolError
from alpaca_production_market import AlpacaSIPProtocol
from test_step2o_websocket_epoch import FakeWS
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


class TestRevisionRuntimeEvidence(unittest.IsolatedAsyncioTestCase):
    async def test_queued_and_inline_failure_count_and_preserve_revision(self):
        for queue_size in (0, 8):
            for message in (cancel(S='A'), correction(S='A')):
                with self.subTest(queue=queue_size, kind=message['T']):
                    capture=BoundedEpochCapture()
                    sockets=[FakeWS([message]),FakeWS([])]
                    retained=[]
                    async def disconnected():
                        retained.append(runtime.last_epoch_diagnostic)
                    runtime=WebSocketRuntime(lambda url:sockets.pop(0),
                        AlpacaSIPProtocol,lambda msg:asyncio.sleep(0),disconnected,
                        epoch_capture=capture,dispatch_queue_max=queue_size)
                    with self.assertRaises(EpochCaptureError):
                        await runtime.run_once('url','key','secret',['A'])
                    first=retained[0]
                    self.assertEqual((first['received'],first['handled']), (1,0))
                    self.assertEqual(first['received_not_confirmed_handled'],1)
                    self.assertEqual(first['unknown_nonmarket_types'],{message['T']:1})
                    self.assertEqual(first['capture_before_teardown']['revision_diagnostic']['frame'],message)
                    self.assertNotIn('revision_diagnostic',capture.snapshot())
                    with self.assertRaises(WebSocketProtocolError):
                        await runtime.run_once('url','key','secret',['A'])
                    self.assertEqual(retained[1]['epoch'],2)
                    self.assertNotIn('revision_diagnostic',retained[1]['capture_before_teardown'])
                    self.assertEqual(first['capture_before_teardown']['revision_diagnostic']['frame'],message)
