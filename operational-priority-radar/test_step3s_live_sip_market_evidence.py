"""Step 3S: validate the committed real-market SIP evidence without widening it."""
import hashlib
import json
from pathlib import Path
import unittest


EVIDENCE = (Path(__file__).parent / "evidence" /
            "STEP3S_LIVE_SIP_MARKET_SESSION_EVIDENCE.json")


class TestStep3SMarketEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    def test_canonical_evidence_sha256_matches(self):
        body = dict(self.evidence)
        expected = body.pop("evidence_sha256")
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), expected)

    def test_ack_and_all_message_counts_match(self):
        e = self.evidence
        self.assertTrue(e["observations"]["subscription_ack_verified"])
        received = e["terminal"]["received"]
        self.assertEqual(received, 550546)
        self.assertEqual(e["terminal"]["handled"], received)
        self.assertEqual(e["metrics_drain"]["acked_messages"], received)
        self.assertEqual(e["terminal"]["received_not_confirmed_handled"], 0)
        self.assertEqual(e["payload_free_ledger"]["last_sequence"], received)

    def test_no_overflow_and_no_sip_407_frame_observed(self):
        o = self.evidence["observations"]
        self.assertFalse(o["dispatch_overflow_observed"])
        self.assertFalse(o["capture_overflow_observed"])
        self.assertFalse(o["alpaca_error_frame_407_observed"])
        self.assertEqual(self.evidence["terminal"]["dispatch_queue"]["overflows"], 0)
        self.assertIsNone(self.evidence["terminal"]["capture_before_teardown"]["invalid_reason"])

    def test_transport_evidence_does_not_escalate_scope(self):
        claims = self.evidence["claims"]
        self.assertTrue(claims["transport_measurement_only"])
        for key in ("production_generation_fence_proven",
                    "full_session_coverage_proven", "sip_continuity_proven",
                    "direct_handoff_authorized", "shadow_deploy_authorized",
                    "retroactive_entries_allowed"):
            self.assertFalse(claims[key])
        ledger = self.evidence["payload_free_ledger"]
        self.assertEqual(ledger["retained_payloads"], 0)
        self.assertEqual(ledger["retained_symbols"], 0)


if __name__ == "__main__":
    unittest.main()
