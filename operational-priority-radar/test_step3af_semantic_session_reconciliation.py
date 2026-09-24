"""Step3AF: assemble exact semantic evidence from five narrow proofs."""
import copy
import unittest
from dataclasses import dataclass

from recovery_commit_permit import symbols_sha256
from recovery_coverage_issuer import issue_recovery_commit_permit
from semantic_session_reconciliation import (
    SemanticSessionReconciliationUnsafe,
    assemble_semantic_session_reconciliation,
)
from sip_semantic_digest import canonical_sha256


SYMBOLS = ["A", "B"]
SESSION = "2026-09-24"
START = "2026-09-24T13:30:00+00:00"
END = "2026-09-24T20:00:00+00:00"
MARKET = "a" * 64
LEDGER = "b" * 64
BARSET = "c" * 64
SCOPE = symbols_sha256(SYMBOLS)


def sign(body, field):
    body = copy.deepcopy(body); body[field] = canonical_sha256(body); return body


def documents():
    transport = {
        "schema": "OPR_FULL_SESSION_TRANSPORT_AUDIT_V1",
        "market_session_evidence_sha256": MARKET, "epoch": 1,
        "received": 100, "handled": 100, "metrics_acked": 100,
        "session_start_utc": START, "session_end_utc": END,
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch_observed": True,
        "capture_overflow_observed": False,
        "dispatch_overflow_observed": False,
        "sip_error_frame_407_observed": False, "payload_retained": False,
        "transport_full_window_observed": True,
        "upstream_market_completeness_proven": False,
        "semantic_eb_replay_completed": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }
    validation = sign({
        "schema": "OPR_FULL_SESSION_SEMANTIC_VALIDATION_V1",
        "session": SESSION, "session_start_utc": START,
        "session_end_utc": END, "market_session_evidence_sha256": MARKET,
        "semantic_ledger_snapshot_sha256": LEDGER, "epoch": 1,
        "received": 100, "handled": 100, "metrics_acked": 100,
        "first_sequence": 1, "last_sequence": 100,
        "symbol_count": 2, "symbols_sha256": SCOPE,
        "bar_count": 10, "trade_count": 89, "status_count": 1,
        "bar_window_count": 10, "semantic_chain_sha256": "d" * 64,
        "bar_multiset_sha256": BARSET,
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch_observed": True,
        "all_received_messages_semantically_validated": True,
        "payloads_retained": 0, "pipeline_writes": 0,
        "sip_bars_reconciled": False, "sip_trades_reconciled": False,
        "sip_statuses_reconciled": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }, "semantic_validation_sha256")
    native = sign({
        "schema": "OPR_NATIVE_SESSION_RECOVERY_AGGREGATE_V1",
        "session": SESSION, "session_start_utc": START,
        "session_end_utc": END, "requested_start_utc": START,
        "as_of_utc": "2026-09-24T20:05:00+00:00",
        "symbol_count": 2, "symbols_sha256": SCOPE,
        "batch_count": 1, "batch_evidence_sha256": ["e" * 64],
        "native_1m_rows": 10, "native_5m_rows": 2, "native_events": 12,
        "session_native_1m_bar_count": 10,
        "session_native_1m_bar_multiset_sha256": BARSET,
        "observed_interbar_gaps": 0, "empty_symbol_timeframe_lanes": 0,
        "signal_count": 1, "signals_sha256": "f" * 64,
        "exact_disjoint_symbol_partition": True,
        "all_rest_page_chains_exhausted": True,
        "native_session_batches_replayed": True,
        "absence_of_bars_is_not_feed_loss_proof": True,
        "sip_semantics_reconciled": False,
        "full_session_coverage_proven": False,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }, "aggregate_evidence_sha256")
    bar = sign({
        "schema": "OPR_SIP_REST_BAR_RECONCILIATION_V1",
        "session": SESSION, "session_start_utc": START,
        "session_end_utc": END, "symbols_sha256": SCOPE, "epoch": 1,
        "native_aggregate_evidence_sha256": native["aggregate_evidence_sha256"],
        "sip_semantic_snapshot_sha256": LEDGER,
        "bar_count": 10, "bar_multiset_sha256": BARSET,
        "rest_pagination_proven": True,
        "native_session_batches_replayed": True,
        "sip_bars_reconciled": True, "bar_reconciliation_gaps": 0,
        "bar_reconciliation_conflicts": 0,
        "sip_trades_reconciled": False, "sip_statuses_reconciled": False,
        "full_session_coverage_proven": False,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }, "bar_reconciliation_sha256")
    disposition = sign({
        "schema": "OPR_EMPTY_TRADE_STATUS_DISPOSITION_V1",
        "session": SESSION, "session_start_utc": START,
        "session_end_utc": END, "worker_instance_id": "W",
        "leader_generation": 3,
        "semantic_validation_sha256": validation["semantic_validation_sha256"],
        "scope_snapshot_sha256": "1" * 64, "epoch": 1,
        "symbol_count": 2, "symbols_sha256": SCOPE,
        "trade_message_count": 89, "status_message_count": 1,
        "active_trade_scope_count": 0,
        "halted_active_trade_scope_count": 0,
        "entry_processing_enabled": False, "pipeline_writes": 0,
        "trade_transition_commits": 0, "status_transition_commits": 0,
        "disposition_reason": "NO_ACTIVE_OR_HALTED_CANONICAL_TRADES",
        "sip_trades_reconciled": True, "sip_statuses_reconciled": True,
        "nonempty_scope_reconciliation_supported": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }, "trade_status_disposition_sha256")
    return transport, validation, native, bar, disposition


@dataclass(frozen=True)
class Token:
    worker_instance_id: str = "W"
    leader_generation: int = 3


class TestSemanticSessionReconciliation(unittest.TestCase):
    def test_exact_five_way_join_is_accepted_by_existing_permit_issuer(self):
        docs = documents()
        evidence = assemble_semantic_session_reconciliation(*docs)
        self.assertTrue(evidence["full_session_coverage_proven"])
        self.assertEqual(evidence["coverage_scope"],
                         "HISTORICAL_EB_COMMIT_ONLY_EMPTY_TRADE_SCOPE")
        permit = issue_recovery_commit_permit(
            docs[0], evidence, Token(), SYMBOLS)
        self.assertTrue(permit["full_session_coverage_proven"])
        self.assertFalse(permit["direct_handoff_authorized"])
        self.assertFalse(permit["shadow_deploy_authorized"])
        self.assertFalse(permit["retroactive_entries_allowed"])

    def test_disposition_leadership_must_equal_permit_leadership(self):
        docs = documents()
        evidence = assemble_semantic_session_reconciliation(*docs)
        with self.assertRaisesRegex(Exception, "LEADERSHIP_MISMATCH"):
            issue_recovery_commit_permit(
                docs[0], evidence,
                Token(worker_instance_id="other", leader_generation=4),
                SYMBOLS)

    def test_each_component_digest_is_mandatory(self):
        for index, field in ((1, "received"), (2, "native_1m_rows"),
                             (3, "bar_count"), (4, "trade_message_count")):
            docs = list(documents()); docs[index][field] += 1
            with self.subTest(index=index):
                with self.assertRaisesRegex(
                        SemanticSessionReconciliationUnsafe, "DIGEST"):
                    assemble_semantic_session_reconciliation(*docs)

    def test_window_scope_epoch_count_and_binding_mismatch_fail(self):
        cases = ((2, "session_end_utc", "2026-09-24T19:59:00+00:00"),
                 (2, "symbols_sha256", "0" * 64), (3, "epoch", 2),
                 (4, "semantic_validation_sha256", "9" * 64),
                 (1, "received", 99))
        digest_fields = {1: "semantic_validation_sha256",
                         2: "aggregate_evidence_sha256",
                         3: "bar_reconciliation_sha256",
                         4: "trade_status_disposition_sha256"}
        for index, field, value in cases:
            docs = list(documents()); docs[index][field] = value
            if index:
                docs[index].pop(digest_fields[index], None)
                docs[index][digest_fields[index]] = canonical_sha256(docs[index])
            with self.subTest(index=index, field=field):
                with self.assertRaises(SemanticSessionReconciliationUnsafe):
                    assemble_semantic_session_reconciliation(*docs)

    def test_any_escalated_or_missing_narrow_claim_fails(self):
        cases = ((0, "sip_error_frame_407_observed", True),
                 (1, "pipeline_writes", 1),
                 (2, "all_rest_page_chains_exhausted", False),
                 (3, "bar_reconciliation_gaps", 1),
                 (4, "active_trade_scope_count", 1))
        digest_fields = {1: "semantic_validation_sha256",
                         2: "aggregate_evidence_sha256",
                         3: "bar_reconciliation_sha256",
                         4: "trade_status_disposition_sha256"}
        for index, field, value in cases:
            docs = list(documents()); docs[index][field] = value
            if index:
                docs[index].pop(digest_fields[index], None)
                docs[index][digest_fields[index]] = canonical_sha256(docs[index])
            with self.subTest(index=index, field=field):
                with self.assertRaises(SemanticSessionReconciliationUnsafe):
                    assemble_semantic_session_reconciliation(*docs)


if __name__ == "__main__":
    unittest.main()
