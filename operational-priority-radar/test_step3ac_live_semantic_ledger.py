"""Step3AC: the full-session runner validates SIP semantics without writes."""
import unittest
from datetime import datetime, timedelta, timezone

from live_sip_soak import LiveSemanticLedger, build_evidence
from sip_drain_coordinator import BoundedSIPDrainCoordinator
from sip_epoch_capture import BoundedEpochCapture
from sip_semantic_digest import canonical_sha256
from test_step3aa_sip_semantic_journal import Leadership, bar, status, trade


START = datetime(2026, 9, 24, 13, 30, tzinfo=timezone.utc)
END = START + timedelta(hours=6, minutes=30)


def run_messages(messages, batch_size=2):
    capture = BoundedEpochCapture(128, 1024 * 1024)
    capture.start(7)
    capture.begin_drain(7)
    ledger = LiveSemanticLedger(
        ["A", "B"], "2026-09-24", START.isoformat(), END.isoformat())
    for message in messages:
        capture.ingest(7, message, received_at=START)
    drain = BoundedSIPDrainCoordinator(
        capture, Leadership(), ledger.reconcile, batch_size=batch_size)
    drain.drain_available(7, max_batches=128)
    return ledger, drain


class TestLiveSemanticLedger(unittest.TestCase):
    def test_validates_every_kind_and_retains_only_signed_summaries(self):
        messages = [bar(0), trade(1), status(2)]
        ledger, _ = run_messages(messages)
        snap = ledger.semantic_snapshot()
        self.assertEqual(snap["schema"], "OPR_LIVE_SIP_SEMANTIC_LEDGER_V1")
        self.assertEqual((snap["bar_count"], snap["trade_count"],
                          snap["status_count"]), (1, 1, 1))
        self.assertEqual(snap["bar_window_count"], 1)
        self.assertEqual(snap["payloads_retained"], 0)
        self.assertFalse(snap["entry_processing_enabled"])
        self.assertEqual(snap["pipeline_writes"], 0)
        self.assertFalse(snap["sip_semantics_reconciled"])
        digest = snap.pop("snapshot_sha256")
        self.assertEqual(digest, canonical_sha256(snap))
        self.assertNotIn("symbols", snap)
        self.assertNotIn("payload", snap)

    def test_bar_multiset_is_batch_independent_and_window_bounded(self):
        before = {**bar(0), "t": (START - timedelta(minutes=1)).isoformat()}
        inside = [bar(i) for i in range(4)]
        after = {**bar(0), "t": END.isoformat()}
        outputs = [run_messages([before, *inside, after], size)[0]
                   .semantic_snapshot() for size in (1, 4)]
        self.assertEqual(outputs[0]["bar_window_count"], 4)
        self.assertEqual(outputs[0]["bar_multiset_sha256"],
                         outputs[1]["bar_multiset_sha256"])
        self.assertNotEqual(outputs[0]["semantic_chain_sha256"],
                            outputs[1]["semantic_chain_sha256"])

    def test_invalid_semantics_fail_before_metrics_ack(self):
        invalid = {**trade(1), "s": 0}
        capture = BoundedEpochCapture(128, 1024 * 1024)
        capture.start(7); capture.begin_drain(7)
        capture.ingest(7, invalid, received_at=START)
        ledger = LiveSemanticLedger(
            ["A"], "2026-09-24", START.isoformat(), END.isoformat())
        drain = BoundedSIPDrainCoordinator(
            capture, Leadership(), ledger.reconcile, batch_size=1)
        with self.assertRaises(Exception):
            drain.drain_one(7)
        self.assertEqual(drain.snapshot()["acked_messages"], 0)
        self.assertEqual(ledger.snapshot()["counts"]["TRADE"], 0)

    def test_evidence_embeds_narrow_semantic_snapshot_without_escalation(self):
        ledger, drain = run_messages([bar(0)])
        terminal = {
            "subscription_ack_verified": True, "failure_class": "STREAM_EOF",
            "received": 1, "handled": 1, "received_not_confirmed_handled": 0,
            "continuity_proven": False, "direct_handoff_authorized": False,
        }
        runtime = type("Runtime", (), {"last_epoch_diagnostic": terminal})()
        evidence = build_evidence(
            started_at=(START - timedelta(minutes=5)).isoformat(),
            ended_at=END.isoformat(), duration_requested=24000,
            symbols_count=2, runtime=runtime, drain=drain, ledger=ledger,
            terminal_error=None, subscription_ack_at=(START - timedelta(
                minutes=1)).isoformat(), expected_session_start=START.isoformat(),
            expected_session_end=END.isoformat())
        self.assertTrue(evidence["claims"]["semantic_validation_only"])
        self.assertFalse(evidence["claims"]["full_session_coverage_proven"])
        self.assertEqual(evidence["semantic_ledger"]["pipeline_writes"], 0)
        self.assertTrue(evidence["session_window"]["observed_through_session_end"])


if __name__ == "__main__":
    unittest.main()
