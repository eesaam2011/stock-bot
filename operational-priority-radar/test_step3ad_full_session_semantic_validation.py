"""Step3AD: exact full-session semantic validation remains narrow."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from full_session_semantic_validation import (
    FullSessionSemanticUnsafe, adjudicate_full_session_semantics,
)
from full_session_transport_evidence import adjudicate_full_session_transport
from live_sip_soak import LiveSemanticLedger, build_evidence
from sip_drain_coordinator import BoundedSIPDrainCoordinator
from sip_epoch_capture import BoundedEpochCapture
from sip_semantic_digest import canonical_sha256
from test_step3aa_sip_semantic_journal import Leadership, bar, status, trade


START = datetime(2026, 9, 24, 13, 30, tzinfo=timezone.utc)
END = START + timedelta(hours=6, minutes=30)
SYMBOLS = ["A", "B"]


def resign_evidence(evidence):
    evidence = copy.deepcopy(evidence)
    evidence.pop("evidence_sha256", None)
    evidence["evidence_sha256"] = canonical_sha256(evidence)
    return evidence


def resign_ledger(evidence):
    evidence = copy.deepcopy(evidence)
    ledger = evidence["semantic_ledger"]
    ledger.pop("snapshot_sha256", None)
    ledger["snapshot_sha256"] = canonical_sha256(ledger)
    return resign_evidence(evidence)


def valid_evidence():
    messages = [bar(0), trade(1), status(2)]
    capture = BoundedEpochCapture(128, 1024 * 1024)
    capture.start(1); capture.begin_drain(1)
    ledger = LiveSemanticLedger(
        SYMBOLS, "2026-09-24", START.isoformat(), END.isoformat())
    for message in messages:
        capture.ingest(1, message, received_at=START)
    drain = BoundedSIPDrainCoordinator(
        capture, Leadership(), ledger.reconcile, batch_size=2)
    drain.drain_available(1, max_batches=8)
    terminal = {
        "schema": "OPR_SIP_EPOCH_TERMINAL_V1", "epoch": 1,
        "subscription_ack_verified": True,
        "failure_class": "OTHER_OR_CANCELLED",
        "received": 3, "handled": 3, "received_not_confirmed_handled": 0,
        "market_data_received": 3, "known_control_received": 0,
        "unknown_nonmarket_received": 0,
        "capture_before_teardown": {
            "acked_upto": 3, "last_sequence": 3, "buffered": 0,
            "bytes": 0, "epoch": 1, "invalid_reason": None,
            "phase": "DRAINING"},
        "dispatch_queue": {"limit": 1024, "depth": 0,
                           "high_water": 2, "overflows": 0},
        "continuity_proven": False, "direct_handoff_authorized": False,
    }
    return build_evidence(
        started_at=(START - timedelta(minutes=10)).isoformat(),
        ended_at=(END + timedelta(minutes=5)).isoformat(),
        duration_requested=24600, symbols_count=len(SYMBOLS),
        runtime=SimpleNamespace(last_epoch_diagnostic=terminal),
        drain=drain, ledger=ledger, terminal_error=None,
        subscription_ack_at=(START - timedelta(minutes=9)).isoformat(),
        expected_session_start=START.isoformat(),
        expected_session_end=END.isoformat())


class TestFullSessionSemanticValidation(unittest.TestCase):
    def test_exact_join_proves_validation_without_reconciliation(self):
        evidence = valid_evidence()
        transport = adjudicate_full_session_transport(evidence)
        audit = adjudicate_full_session_semantics(
            evidence, transport, SYMBOLS)
        self.assertTrue(audit["all_received_messages_semantically_validated"])
        self.assertEqual(audit["received"], 3)
        self.assertEqual((audit["bar_count"], audit["trade_count"],
                          audit["status_count"]), (1, 1, 1))
        for key in ("sip_bars_reconciled", "sip_trades_reconciled",
                    "sip_statuses_reconciled", "full_session_coverage_proven",
                    "direct_handoff_authorized", "shadow_deploy_authorized",
                    "retroactive_entries_allowed"):
            self.assertFalse(audit[key])

    def test_semantic_snapshot_tampering_is_rejected_before_join(self):
        evidence = valid_evidence()
        evidence["semantic_ledger"]["trade_count"] += 1
        evidence = resign_evidence(evidence)
        with self.assertRaisesRegex(FullSessionSemanticUnsafe, "DIGEST"):
            adjudicate_full_session_semantics(
                evidence, adjudicate_full_session_transport(evidence), SYMBOLS)

    def test_count_scope_window_and_epoch_mismatches_fail_closed(self):
        mutations = (
            ("item_count", 2), ("symbol_count", 1),
            ("bar_window_end_utc", (END + timedelta(minutes=1)).isoformat()),
            ("epoch", 2), ("first_sequence", 2),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                evidence = valid_evidence()
                evidence["semantic_ledger"][field] = value
                evidence = resign_ledger(evidence)
                with self.assertRaises(FullSessionSemanticUnsafe):
                    adjudicate_full_session_semantics(
                        evidence, adjudicate_full_session_transport(evidence),
                        SYMBOLS)

    def test_transport_audit_must_be_exact_recomputation(self):
        evidence = valid_evidence()
        transport = adjudicate_full_session_transport(evidence)
        transport["received"] -= 1
        with self.assertRaisesRegex(FullSessionSemanticUnsafe, "TRANSPORT"):
            adjudicate_full_session_semantics(evidence, transport, SYMBOLS)

    def test_any_scope_escalation_or_write_claim_fails(self):
        for field, value in (("pipeline_writes", 1),
                             ("sip_semantics_reconciled", True),
                             ("direct_handoff_authorized", True)):
            with self.subTest(field=field):
                evidence = valid_evidence()
                evidence["semantic_ledger"][field] = value
                evidence = resign_ledger(evidence)
                with self.assertRaises(FullSessionSemanticUnsafe):
                    adjudicate_full_session_semantics(
                        evidence, adjudicate_full_session_transport(evidence),
                        SYMBOLS)


if __name__ == "__main__":
    unittest.main()
