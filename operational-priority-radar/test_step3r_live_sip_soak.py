"""Step 3R: the live runner stays read-only and its evidence stays honest."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from live_sip_soak import (
    LiveSIPSoakBlocked, PayloadFreeLedger, _require_live_gate, build_evidence,
)


class TestLiveSIPSoak(unittest.TestCase):
    def test_explicit_gate_and_credentials_are_mandatory(self):
        with self.assertRaisesRegex(LiveSIPSoakBlocked, "EXPLICIT_GATE"):
            _require_live_gate({})
        with self.assertRaisesRegex(LiveSIPSoakBlocked, "CREDENTIALS"):
            _require_live_gate({"OPR_LIVE_SIP_SOAK":
                                "I_UNDERSTAND_READ_ONLY_SIP"})

    def test_payload_free_reconciler_returns_narrow_exact_proof(self):
        ledger = PayloadFreeLedger()
        items = [SimpleNamespace(kind="TRADE", sequence=8),
                 SimpleNamespace(kind="STATUS", sequence=9)]
        context = {"epoch": 2, "batch_sha256": "abc"}
        proof = ledger.reconcile(items, context)
        self.assertTrue(proof["committed"])
        self.assertFalse(proof["direct_handoff_authorized"])
        self.assertFalse(proof["full_session_coverage_proven"])
        self.assertEqual(ledger.snapshot()["retained_payloads"], 0)
        self.assertEqual(ledger.snapshot()["retained_symbols"], 0)
        self.assertEqual(ledger.snapshot()["counts"]["TRADE"], 1)

    def test_evidence_never_escalates_transport_measurement(self):
        terminal = {
            "schema": "OPR_SIP_EPOCH_TERMINAL_V1", "epoch": 3,
            "subscription_ack_verified": True, "failure_class": "STREAM_EOF",
            "received": 9, "handled": 9,
            "received_not_confirmed_handled": 0,
            "continuity_proven": False, "direct_handoff_authorized": False,
        }
        runtime = SimpleNamespace(last_epoch_diagnostic=terminal)
        drain = SimpleNamespace(snapshot=lambda: {
            "schema": "OPR_SIP_BOUNDED_DRAIN_V1", "acked_messages": 9,
            "direct_handoff_authorized": False,
            "full_session_coverage_proven": False})
        ledger = PayloadFreeLedger()
        evidence = build_evidence(
            started_at="2026-09-23T13:00:00+00:00",
            ended_at="2026-09-23T13:30:00+00:00",
            duration_requested=1800, symbols_count=5000,
            runtime=runtime, drain=drain, ledger=ledger,
            terminal_error=None)
        claims = evidence["claims"]
        self.assertFalse(claims["full_session_coverage_proven"])
        self.assertFalse(claims["direct_handoff_authorized"])
        self.assertFalse(claims["shadow_deploy_authorized"])
        self.assertTrue(evidence["observations"]["received_equals_handled"])
        raw = json.dumps(evidence)
        self.assertNotIn("APCA_API_SECRET", raw)
        self.assertNotIn("AAPL", raw)

    def test_real_407_is_recognized_only_from_terminal_classification(self):
        def evidence_for(failure):
            runtime = SimpleNamespace(last_epoch_diagnostic={
                "subscription_ack_verified": True, "failure_class": failure,
                "received": 0, "handled": 0})
            drain = SimpleNamespace(snapshot=lambda: {
                "acked_messages": 0, "direct_handoff_authorized": False,
                "full_session_coverage_proven": False})
            return build_evidence(
                started_at="x", ended_at="y", duration_requested=60,
                symbols_count=1, runtime=runtime, drain=drain,
                ledger=PayloadFreeLedger(), terminal_error=None)
        self.assertTrue(evidence_for("SIP_ERROR_FRAME_407")["observations"]
                        ["alpaca_error_frame_407_observed"])
        self.assertFalse(evidence_for("OTHER_407_EXCEPTION_NOT_ALPACA_PROOF")
                         ["observations"]["alpaca_error_frame_407_observed"])


if __name__ == "__main__":
    unittest.main()
