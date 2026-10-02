"""Step3W: a commit permit requires matching transport + semantic audits."""
import copy
import hashlib
import json
import unittest

from full_session_transport_evidence import SCHEMA as TRANSPORT_SCHEMA
from recovery_commit_permit import validate_recovery_commit_permit
from recovery_coverage_issuer import (
    SEMANTIC_SCHEMA, RecoveryCoverageIssuerUnsafe,
    issue_recovery_commit_permit,
)
from recovery_commit_permit import symbols_sha256


SYMBOLS = ["A", "B"]
RECEIVED = 900000
MARKET_SHA = "a" * 64


class Token:
    worker_instance_id = "W"
    leader_generation = 4


def transport():
    return {
        "schema": TRANSPORT_SCHEMA,
        "market_session_evidence_sha256": MARKET_SHA,
        "epoch": 1,
        "received": RECEIVED,
        "handled": RECEIVED,
        "metrics_acked": RECEIVED,
        "session_start_utc": "2026-09-24T13:30:00+00:00",
        "session_end_utc": "2026-09-24T20:00:00+00:00",
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch_observed": True,
        "capture_overflow_observed": False,
        "dispatch_overflow_observed": False,
        "sip_error_frame_407_observed": False,
        "payload_retained": False,
        "transport_full_window_observed": True,
        "upstream_market_completeness_proven": False,
        "semantic_eb_replay_completed": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }


def semantic():
    body = {
        "schema": SEMANTIC_SCHEMA,
        "worker_instance_id": "W",
        "leader_generation": 4,
        "session": "2026-09-24",
        "session_start_utc": "2026-09-24T13:30:00+00:00",
        "session_end_utc": "2026-09-24T20:00:00+00:00",
        "as_of_utc": "2026-09-24T20:05:00+00:00",
        "market_session_evidence_sha256": MARKET_SHA,
        "epoch": 1,
        "received": RECEIVED,
        "handled": RECEIVED,
        "metrics_acked": RECEIVED,
        "first_sequence": 1,
        "last_sequence": RECEIVED,
        "capture_acked_upto": RECEIVED,
        "capture_buffered": 0,
        "dispatch_overflows": 0,
        "symbol_count": len(SYMBOLS),
        "symbols_sha256": symbols_sha256(SYMBOLS),
        "rest_pagination_proven": True,
        "native_session_reconciled": True,
        "sip_bars_reconciled": True,
        "sip_trades_reconciled": True,
        "sip_statuses_reconciled": True,
        "single_uninterrupted_epoch": True,
        "capture_overflow_observed": False,
        "dispatch_overflow_observed": False,
        "reconciliation_gaps": 0,
        "reconciliation_conflicts": 0,
        "retroactive_entries_created": 0,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "full_session_coverage_proven": True,
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    body["semantic_evidence_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    return body


def resign(document):
    document = copy.deepcopy(document)
    document.pop("semantic_evidence_sha256", None)
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    document["semantic_evidence_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    return document


class TestRecoveryCoverageIssuer(unittest.TestCase):
    def test_exact_two_audit_join_issues_narrow_valid_permit(self):
        permit = issue_recovery_commit_permit(
            transport(), semantic(), Token(), SYMBOLS)
        validated = validate_recovery_commit_permit(
            permit, worker_instance_id="W", leader_generation=4,
            session="2026-09-24", symbols=SYMBOLS)
        self.assertTrue(validated["full_session_coverage_proven"])
        self.assertEqual(validated["market_session_evidence_sha256"], MARKET_SHA)
        self.assertEqual(validated["semantic_reconciliation_evidence_sha256"],
                         semantic()["semantic_evidence_sha256"])
        self.assertFalse(permit["retroactive_entries_allowed"])
        self.assertFalse(permit["direct_handoff_authorized"])
        self.assertFalse(permit["shadow_deploy_authorized"])

    def test_transport_alone_can_never_issue(self):
        with self.assertRaises(RecoveryCoverageIssuerUnsafe):
            issue_recovery_commit_permit(transport(), None, Token(), SYMBOLS)

    def test_semantic_digest_and_every_required_claim_fail_closed(self):
        bad = semantic(); bad["received"] -= 1
        with self.assertRaisesRegex(RecoveryCoverageIssuerUnsafe, "DIGEST"):
            issue_recovery_commit_permit(transport(), bad, Token(), SYMBOLS)
        for key, value in (
            ("rest_pagination_proven", False),
            ("native_session_reconciled", False),
            ("sip_bars_reconciled", False),
            ("sip_trades_reconciled", False),
            ("sip_statuses_reconciled", False),
            ("single_uninterrupted_epoch", False),
            ("reconciliation_gaps", 1),
            ("reconciliation_conflicts", 1),
            ("retroactive_entries_created", 1),
            ("direct_handoff_authorized", True),
        ):
            with self.subTest(key=key):
                bad = semantic(); bad[key] = value; bad = resign(bad)
                with self.assertRaises(RecoveryCoverageIssuerUnsafe):
                    issue_recovery_commit_permit(
                        transport(), bad, Token(), SYMBOLS)

    def test_cross_audit_window_epoch_count_digest_and_scope_mismatch_rejected(self):
        mutations = (
            lambda x: x.__setitem__("epoch", 2),
            lambda x: x.__setitem__("received", RECEIVED - 1),
            lambda x: x.__setitem__("session_start_utc",
                                    "2026-09-24T13:31:00+00:00"),
            lambda x: x.__setitem__("market_session_evidence_sha256", "c" * 64),
            lambda x: x.__setitem__("symbols_sha256", "d" * 64),
        )
        for mutate in mutations:
            evidence = semantic(); mutate(evidence); evidence = resign(evidence)
            with self.assertRaises(RecoveryCoverageIssuerUnsafe):
                issue_recovery_commit_permit(
                    transport(), evidence, Token(), SYMBOLS)

    def test_permit_is_bound_to_current_leadership(self):
        permit = issue_recovery_commit_permit(
            transport(), semantic(), Token(), SYMBOLS)
        self.assertEqual(permit["worker_instance_id"], "W")
        self.assertEqual(permit["leader_generation"], 4)
        with self.assertRaises(Exception):
            validate_recovery_commit_permit(
                permit, worker_instance_id="SUCCESSOR", leader_generation=5,
                session="2026-09-24", symbols=SYMBOLS)

    def test_semantic_disposition_leadership_must_be_current(self):
        evidence = semantic()
        evidence["leader_generation"] = 3
        evidence = resign(evidence)
        with self.assertRaisesRegex(
                RecoveryCoverageIssuerUnsafe, "LEADERSHIP_MISMATCH"):
            issue_recovery_commit_permit(
                transport(), evidence, Token(), SYMBOLS)


if __name__ == "__main__":
    unittest.main()
