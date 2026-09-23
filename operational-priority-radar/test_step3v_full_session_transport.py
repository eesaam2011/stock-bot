"""Step3V: full-session time/epoch adjudication remains narrow and exact."""
import copy
import hashlib
import json
import unittest
from types import SimpleNamespace

from full_session_transport_evidence import (
    FullSessionTransportUnsafe, adjudicate_full_session_transport,
)
from live_sip_soak import build_evidence


def resign(evidence):
    evidence = copy.deepcopy(evidence)
    evidence.pop("evidence_sha256", None)
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    evidence["evidence_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    return evidence


def valid_evidence():
    received = 900000
    terminal = {
        "schema": "OPR_SIP_EPOCH_TERMINAL_V1",
        "epoch": 1,
        "subscription_ack_verified": True,
        "failure_class": "OTHER_OR_CANCELLED",
        "received": received,
        "handled": received,
        "received_not_confirmed_handled": 0,
        "capture_before_teardown": {
            "acked_upto": received, "last_sequence": received,
            "buffered": 0, "bytes": 0, "epoch": 1,
            "invalid_reason": None, "phase": "DRAINING"},
        "dispatch_queue": {"limit": 1024, "depth": 0,
                           "high_water": 300, "overflows": 0},
        "continuity_proven": False,
        "direct_handoff_authorized": False,
    }
    runtime = SimpleNamespace(last_epoch_diagnostic=terminal)
    drain = SimpleNamespace(snapshot=lambda: {
        "schema": "OPR_SIP_BOUNDED_DRAIN_V1",
        "acked_messages": received, "batch_size": 256,
        "reconciled_batches": 4000, "last_batch_sha256": "a" * 64,
        "direct_handoff_authorized": False,
        "full_session_coverage_proven": False})
    ledger = SimpleNamespace(snapshot=lambda: {
        "retained_payloads": 0, "retained_symbols": 0,
        "counts": {"BAR": 5000, "TRADE": 894900, "STATUS": 100},
        "batches": 4000, "first_sequence": 1,
        "last_sequence": received})
    return build_evidence(
        started_at="2026-09-24T13:19:00+00:00",
        ended_at="2026-09-24T20:05:01+00:00",
        duration_requested=24300, symbols_count=12000,
        runtime=runtime, drain=drain, ledger=ledger,
        terminal_error=None,
        subscription_ack_at="2026-09-24T13:20:02+00:00",
        expected_session_start="2026-09-24T13:30:00+00:00",
        expected_session_end="2026-09-24T20:00:00+00:00")


class TestFullSessionTransport(unittest.TestCase):
    def test_exact_full_window_transport_passes_but_does_not_escalate(self):
        audit = adjudicate_full_session_transport(valid_evidence())
        self.assertTrue(audit["transport_full_window_observed"])
        self.assertTrue(audit["single_uninterrupted_epoch_observed"])
        self.assertFalse(audit["upstream_market_completeness_proven"])
        self.assertFalse(audit["semantic_eb_replay_completed"])
        self.assertFalse(audit["full_session_coverage_proven"])
        self.assertFalse(audit["direct_handoff_authorized"])
        self.assertFalse(audit["shadow_deploy_authorized"])
        self.assertFalse(audit["retroactive_entries_allowed"])

    def test_late_ack_or_early_end_rejected_even_with_valid_digest(self):
        for field, value in (
            ("subscription_ack_at_utc", "2026-09-24T13:30:01+00:00"),
            ("observed_through_session_end", False),
        ):
            with self.subTest(field=field):
                evidence = valid_evidence()
                evidence["session_window"][field] = value
                evidence = resign(evidence)
                with self.assertRaises(FullSessionTransportUnsafe):
                    adjudicate_full_session_transport(evidence)

    def test_every_count_tail_and_overflow_mismatch_is_rejected(self):
        mutations = (
            lambda e: e["terminal"].__setitem__("handled", 899999),
            lambda e: e["metrics_drain"].__setitem__("acked_messages", 899999),
            lambda e: e["terminal"]["capture_before_teardown"].__setitem__("buffered", 1),
            lambda e: e["terminal"]["dispatch_queue"].__setitem__("overflows", 1),
            lambda e: e["payload_free_ledger"].__setitem__("first_sequence", 2),
            lambda e: e["terminal"].__setitem__("epoch", 2),
        )
        for mutate in mutations:
            evidence = valid_evidence(); mutate(evidence); evidence = resign(evidence)
            with self.assertRaises(FullSessionTransportUnsafe):
                adjudicate_full_session_transport(evidence)

    def test_407_only_counts_from_exact_sip_error_frame_class(self):
        fake = valid_evidence()
        fake["terminal"]["failure_class"] = "OTHER_407_EXCEPTION_NOT_ALPACA_PROOF"
        fake = resign(fake)
        with self.assertRaisesRegex(FullSessionTransportUnsafe,
                                    "UNPLANNED_TERMINATION"):
            adjudicate_full_session_transport(fake)
        real = valid_evidence()
        real["terminal"]["failure_class"] = "SIP_ERROR_FRAME_407"
        real["observations"]["alpaca_error_frame_407_observed"] = True
        real = resign(real)
        with self.assertRaisesRegex(FullSessionTransportUnsafe,
                                    "SIP_ERROR_FRAME_407"):
            adjudicate_full_session_transport(real)

    def test_tampered_evidence_digest_is_rejected_first(self):
        evidence = valid_evidence()
        evidence["terminal"]["received"] += 1
        with self.assertRaisesRegex(FullSessionTransportUnsafe, "DIGEST"):
            adjudicate_full_session_transport(evidence)


if __name__ == "__main__":
    unittest.main()
