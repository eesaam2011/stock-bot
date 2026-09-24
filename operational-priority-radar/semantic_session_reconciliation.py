"""Assemble the exact semantic session evidence accepted by the permit issuer.

The proof is limited to historical E/B recovery for one frozen session and
symbol scope.  It requires a full transport epoch, validation of every received
SIP message, exhausted native REST replay, exact REST/SIP 1m-bar equality, and
an independently generation-fenced trade/status disposition.
"""
from __future__ import annotations

from empty_trade_status_disposition import DISPOSITION_SCHEMA
from full_session_semantic_validation import SCHEMA as VALIDATION_SCHEMA
from full_session_transport_evidence import SCHEMA as TRANSPORT_SCHEMA
from native_session_recovery_batches import AGGREGATE_SCHEMA
from recovery_coverage_issuer import SEMANTIC_SCHEMA
from sip_rest_bar_reconciliation import SCHEMA as BAR_SCHEMA
from sip_semantic_digest import canonical_sha256


class SemanticSessionReconciliationUnsafe(RuntimeError):
    pass


def _verify(document, field):
    if not isinstance(document, dict):
        raise SemanticSessionReconciliationUnsafe(
            "SEMANTIC_RECONCILIATION_EVIDENCE_MISSING")
    body = dict(document)
    expected = body.pop(field, None)
    if expected != canonical_sha256(body):
        raise SemanticSessionReconciliationUnsafe(
            "SEMANTIC_RECONCILIATION_DIGEST_INVALID")
    return expected


def _require(document, required, label):
    for key, expected in required.items():
        if document.get(key) != expected:
            raise SemanticSessionReconciliationUnsafe(
                f"SEMANTIC_RECONCILIATION_{label}_FAILED:{key}")


def assemble_semantic_session_reconciliation(
        transport, validation, native, bar_proof, trade_status_disposition):
    """Join five signed, independently narrow inputs into one exact proof."""
    market_sha = transport.get("market_session_evidence_sha256")
    validation_sha = _verify(validation, "semantic_validation_sha256")
    native_sha = _verify(native, "aggregate_evidence_sha256")
    bar_sha = _verify(bar_proof, "bar_reconciliation_sha256")
    disposition_sha = _verify(
        trade_status_disposition, "trade_status_disposition_sha256")
    if (transport.get("schema") != TRANSPORT_SCHEMA
            or validation.get("schema") != VALIDATION_SCHEMA
            or native.get("schema") != AGGREGATE_SCHEMA
            or bar_proof.get("schema") != BAR_SCHEMA
            or trade_status_disposition.get("schema") != DISPOSITION_SCHEMA):
        raise SemanticSessionReconciliationUnsafe(
            "SEMANTIC_RECONCILIATION_SCHEMA_INVALID")
    if (not isinstance(market_sha, str) or len(market_sha) != 64
            or any(char not in "0123456789abcdef" for char in market_sha)):
        raise SemanticSessionReconciliationUnsafe(
            "SEMANTIC_RECONCILIATION_MARKET_DIGEST_INVALID")

    _require(transport, {
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch_observed": True,
        "capture_overflow_observed": False,
        "dispatch_overflow_observed": False,
        "sip_error_frame_407_observed": False,
        "transport_full_window_observed": True,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }, "TRANSPORT")
    _require(validation, {
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch_observed": True,
        "all_received_messages_semantically_validated": True,
        "payloads_retained": 0,
        "pipeline_writes": 0,
        "sip_bars_reconciled": False,
        "sip_trades_reconciled": False,
        "sip_statuses_reconciled": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }, "VALIDATION")
    _require(native, {
        "exact_disjoint_symbol_partition": True,
        "all_rest_page_chains_exhausted": True,
        "native_session_batches_replayed": True,
        "sip_semantics_reconciled": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }, "NATIVE")
    _require(bar_proof, {
        "rest_pagination_proven": True,
        "native_session_batches_replayed": True,
        "sip_bars_reconciled": True,
        "bar_reconciliation_gaps": 0,
        "bar_reconciliation_conflicts": 0,
        "sip_trades_reconciled": False,
        "sip_statuses_reconciled": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }, "BAR")
    _require(trade_status_disposition, {
        "active_trade_scope_count": 0,
        "halted_active_trade_scope_count": 0,
        "entry_processing_enabled": False,
        "pipeline_writes": 0,
        "trade_transition_commits": 0,
        "status_transition_commits": 0,
        "sip_trades_reconciled": True,
        "sip_statuses_reconciled": True,
        "nonempty_scope_reconciliation_supported": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }, "DISPOSITION")

    session = validation.get("session")
    start = validation.get("session_start_utc")
    end = validation.get("session_end_utc")
    epoch = validation.get("epoch")
    symbol_count = validation.get("symbol_count")
    symbols_sha = validation.get("symbols_sha256")
    received = validation.get("received")
    common = (session, start, end)
    for document in (native, bar_proof, trade_status_disposition):
        if tuple(document.get(key) for key in (
                "session", "session_start_utc", "session_end_utc")) != common:
            raise SemanticSessionReconciliationUnsafe(
                "SEMANTIC_RECONCILIATION_WINDOW_MISMATCH")
    for document in (native, trade_status_disposition):
        if (document.get("symbol_count") != symbol_count
                or document.get("symbols_sha256") != symbols_sha):
            raise SemanticSessionReconciliationUnsafe(
                "SEMANTIC_RECONCILIATION_SCOPE_MISMATCH")
    if (bar_proof.get("symbols_sha256") != symbols_sha
            or bar_proof.get("epoch") != epoch
            or trade_status_disposition.get("epoch") != epoch):
        raise SemanticSessionReconciliationUnsafe(
            "SEMANTIC_RECONCILIATION_EPOCH_OR_SCOPE_MISMATCH")
    if (validation.get("market_session_evidence_sha256") != market_sha
            or bar_proof.get("native_aggregate_evidence_sha256") != native_sha
            or bar_proof.get("sip_semantic_snapshot_sha256")
            != validation.get("semantic_ledger_snapshot_sha256")
            or trade_status_disposition.get("semantic_validation_sha256")
            != validation_sha):
        raise SemanticSessionReconciliationUnsafe(
            "SEMANTIC_RECONCILIATION_COMPONENT_BINDING_MISMATCH")
    if (bar_proof.get("bar_count") != validation.get("bar_window_count")
            or bar_proof.get("bar_multiset_sha256")
            != validation.get("bar_multiset_sha256")
            or native.get("session_native_1m_bar_count")
            != bar_proof.get("bar_count")
            or native.get("session_native_1m_bar_multiset_sha256")
            != bar_proof.get("bar_multiset_sha256")):
        raise SemanticSessionReconciliationUnsafe(
            "SEMANTIC_RECONCILIATION_BAR_MISMATCH")
    if (type(received) is not int or received < 1
            or validation.get("handled") != received
            or validation.get("metrics_acked") != received
            or validation.get("first_sequence") != 1
            or validation.get("last_sequence") != received
            or transport.get("received") != received
            or transport.get("handled") != received
            or transport.get("metrics_acked") != received
            or transport.get("epoch") != epoch):
        raise SemanticSessionReconciliationUnsafe(
            "SEMANTIC_RECONCILIATION_COUNT_MISMATCH")
    as_of = native.get("as_of_utc")
    if not isinstance(as_of, str) or not as_of:
        raise SemanticSessionReconciliationUnsafe(
            "SEMANTIC_RECONCILIATION_AS_OF_INVALID")

    body = {
        "schema": SEMANTIC_SCHEMA,
        "session": session,
        "session_start_utc": start,
        "session_end_utc": end,
        "as_of_utc": as_of,
        "market_session_evidence_sha256": market_sha,
        "full_session_semantic_validation_sha256": validation_sha,
        "native_aggregate_evidence_sha256": native_sha,
        "bar_reconciliation_sha256": bar_sha,
        "trade_status_disposition_sha256": disposition_sha,
        "epoch": epoch,
        "received": received,
        "handled": received,
        "metrics_acked": received,
        "first_sequence": 1,
        "last_sequence": received,
        "capture_acked_upto": received,
        "capture_buffered": 0,
        "dispatch_overflows": 0,
        "symbol_count": symbol_count,
        "symbols_sha256": symbols_sha,
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
        "coverage_scope": "HISTORICAL_EB_COMMIT_ONLY_EMPTY_TRADE_SCOPE",
    }
    body["semantic_evidence_sha256"] = canonical_sha256(body)
    return body
