import unittest
from datetime import datetime, timedelta, timezone
from sip_original_trade_window import OriginalTradeWindow, OriginalTradeIndexOverflow
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

    def test_recent_capacity_overflow_is_explicit_and_fail_closed(self):
        window=OriginalTradeWindow(max_items=1)
        window.observe(trade())
        with self.assertRaises(OriginalTradeIndexOverflow):window.observe(trade(2))
        result=window.inspect(cancel())
        self.assertEqual(result['reason'],'ORIGINAL_INDEX_OVERFLOW')
        self.assertEqual(result['retained'],1)
        self.assertTrue(result['overflowed'])
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
        evidence=capture.snapshot()['revision_diagnostic']['original_trade_evidence']
        self.assertEqual(evidence['reason'],'PAIR_MATCHED_CROSS_EPOCH_NOT_BAR_RECONCILED')
        self.assertFalse(evidence['same_epoch'])
        self.assertFalse(evidence['capture_ack_authorized'])

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

    def test_undocumented_live_action_precedes_window_miss(self):
        window=OriginalTradeWindow(max_items=1)
        window.observe(trade(2))
        result=window.inspect(cancel(i=1,a='1'))
        self.assertEqual(result['reason'],'CANCEL_ACTION_UNDOCUMENTED')
        self.assertEqual(result['cancel_error_action'],'1')
        self.assertFalse(result['cancel_error_action_documented'])
        self.assertEqual(result['expired'],0)
        self.assertFalse(result['capture_ack_authorized'])

    def test_byte_budget_evicts_without_unbounded_growth(self):
        window=OriginalTradeWindow(max_items=100,max_bytes=1024)
        with self.assertRaises(OriginalTradeIndexOverflow):
            for i in range(1,101):window.observe(trade(i))
        result=window.inspect(cancel(1))
        self.assertLessEqual(result['bytes'],1024)
        self.assertTrue(result['overflowed'])
        self.assertEqual(result['reason'],'ORIGINAL_INDEX_OVERFLOW')

    def test_age_expiry_is_causal_and_explicit(self):
        window=OriginalTradeWindow(max_age_seconds=60)
        start=datetime(2026,9,29,13,30,tzinfo=timezone.utc)
        window.observe(trade(),epoch=1,received_at=start)
        window.observe(trade(2),epoch=1,received_at=start+timedelta(seconds=61))
        result=window.inspect(cancel(),epoch=1,received_at=start+timedelta(seconds=61))
        self.assertEqual(result['reason'],'ORIGINAL_NOT_RETAINED')
        self.assertEqual(result['expired'],1)
        self.assertEqual(result['retention_seconds'],60)

    def test_observed_market_rate_does_not_evict_first_trade(self):
        window=OriginalTradeWindow()
        observed=datetime(2026,9,29,13,30,tzinfo=timezone.utc)
        for trade_id in range(1,428_754):
            window.observe(trade(trade_id),epoch=1,received_at=observed)
        result=window.inspect(cancel(i=1),epoch=1,received_at=observed)
        self.assertEqual(result['retained'],428_753)
        self.assertEqual(result['expired'],0)
        self.assertFalse(result['overflowed'])
        self.assertTrue(result['matched_within_retained_window'])
        self.assertEqual(result['reason'],'PAIR_MATCHED_NOT_BAR_RECONCILED')
