import unittest
from datetime import datetime, timezone

from sip_epoch_capture import BoundedEpochCapture, EpochCaptureError
from symbol_decision_isolation import (
    SymbolDecisionIsolation, SymbolDecisionIsolationUnsafe,
)
from test_step3aq_trade_revision_audit import trade, cancel


UTC = timezone.utc


class SymbolIsolationTests(unittest.TestCase):
    def test_one_symbol_block_does_not_block_another(self):
        isolation=SymbolDecisionIsolation("2026-09-29",max_symbols=2)
        result=isolation.block("AAOI","REVISION",datetime.now(UTC))
        self.assertTrue(isolation.blocked("AAOI"))
        self.assertFalse(isolation.blocked("AAPL"))
        self.assertFalse(result["other_symbols_affected"])
        self.assertFalse(result["new_entry_allowed"])

    def test_limit_fails_closed(self):
        isolation=SymbolDecisionIsolation("2026-09-29",max_symbols=1)
        isolation.block("AAOI","REVISION",datetime.now(UTC))
        with self.assertRaisesRegex(SymbolDecisionIsolationUnsafe,"LIMIT"):
            isolation.block("AAPL","REVISION",datetime.now(UTC))

    def test_documented_revision_can_be_isolated_without_epoch_stop(self):
        observed=[]
        capture=BoundedEpochCapture(revision_isolator=lambda d:(observed.append(d) or True))
        capture.start(1);capture.ingest(1,trade(),received_at=datetime.now(UTC))
        self.assertIsNone(capture.ingest(1,cancel(a="C"),received_at=datetime.now(UTC)))
        snapshot=capture.snapshot()
        self.assertEqual(snapshot["phase"],capture.CAPTURING)
        self.assertEqual(snapshot["isolated_revisions"],1)
        self.assertEqual(observed[0]["frame"]["S"],"AAPL")

    def test_refused_or_unknown_revision_still_invalidates_epoch(self):
        capture=BoundedEpochCapture(revision_isolator=lambda _:False)
        capture.start(1);capture.ingest(1,trade(),received_at=datetime.now(UTC))
        with self.assertRaises(EpochCaptureError):
            capture.ingest(1,cancel(a="1"),received_at=datetime.now(UTC))
        self.assertEqual(capture.snapshot()["invalid_reason"],
                         "SIP_TRADE_REVISION_UNRECONCILED")


if __name__ == "__main__":unittest.main()
