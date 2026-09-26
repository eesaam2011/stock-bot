import unittest
from sip_original_trade_window import OriginalTradeWindow
from sip_epoch_capture import BoundedEpochCapture, EpochCaptureError
from test_step3aq_trade_revision_audit import trade, cancel, correction


class TestOriginalWindow(unittest.TestCase):
    def test_cancel_and_correction_link_after_ack(self):
        for revision in (cancel(), correction()):
            with self.subTest(kind=revision['T']):
                capture=BoundedEpochCapture(); capture.start(1); capture.begin_drain(1)
                capture.ingest(1, trade()); capture.ack_batch(1, 1)
                with self.assertRaises(EpochCaptureError): capture.ingest(1, revision)
                diagnostic=capture.snapshot()['revision_diagnostic']
                evidence=diagnostic['original_trade_evidence']
                self.assertTrue(evidence['matched_within_retained_window'])
                self.assertEqual(evidence['original_frame'],trade())
                self.assertFalse(diagnostic['revision_reconciled'])
                self.assertFalse(evidence['capture_ack_authorized'])
                self.assertEqual(capture.phase, capture.INVALID)

    def test_eviction_is_explicit_and_bounded(self):
        window=OriginalTradeWindow(max_items=1)
        window.observe(trade());window.observe(trade(2))
        result=window.inspect(cancel())
        self.assertEqual(result['reason'],'ORIGINAL_NOT_RETAINED')
        self.assertEqual((result['retained'],result['evicted']),(1,1))
        self.assertFalse(result['upstream_completeness_proven'])

    def test_conflict_and_duplicate_are_not_matched(self):
        window=OriginalTradeWindow();window.observe(trade())
        self.assertEqual(window.inspect(cancel(p=99))['reason'],'ORIGINAL_CONFLICT')
        window.observe(trade())
        self.assertEqual(window.inspect(cancel())['reason'],'ORIGINAL_ID_REPEATED')

    def test_new_epoch_cannot_reuse_original(self):
        capture=BoundedEpochCapture();capture.start(1);capture.ingest(1,trade())
        capture.start(2)
        with self.assertRaises(EpochCaptureError):capture.ingest(2,cancel())
        self.assertEqual(capture.snapshot()['revision_diagnostic']['original_trade_evidence']['reason'],
                         'ORIGINAL_NOT_RETAINED')

    def test_allowlist_and_copy(self):
        window=OriginalTradeWindow();row=trade(secret='not retained');window.observe(row)
        row['p']=999
        result=window.inspect(cancel())
        self.assertTrue(result['matched_within_retained_window'])
        self.assertNotIn('secret',result['original_frame'])
        result['original_frame']['p']=1
        self.assertTrue(window.inspect(cancel())['matched_within_retained_window'])

    def test_oversized_fields_and_malformed_ids(self):
        window=OriginalTradeWindow();window.observe(trade(c=['x'*10000]))
        self.assertEqual(window.inspect(cancel())['rejected'],1)
        self.assertEqual(window.inspect(cancel(i=[]))['reason'],'REVISION_ID_INVALID')

    def test_same_id_other_symbol_never_matches(self):
        window=OriginalTradeWindow();window.observe(trade(S='MSFT'))
        self.assertEqual(window.inspect(cancel())['reason'],'ORIGINAL_NOT_RETAINED')

    def test_byte_budget_evicts_without_unbounded_growth(self):
        window=OriginalTradeWindow(max_items=100,max_bytes=1024)
        for i in range(1,101):window.observe(trade(i))
        result=window.inspect(cancel(100))
        self.assertLessEqual(result['bytes'],1024)
        self.assertGreater(result['evicted'],0)
        self.assertTrue(result['matched_within_retained_window'])
