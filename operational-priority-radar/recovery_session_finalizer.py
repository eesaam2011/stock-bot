"""Production-shaped, write-free assembly of a recovered E/B session.

This coordinator executes every bounded read and proof producer required before
the existing Redis E/B committer may be called.  It deliberately stops at a
permit and canonical record batches: no Redis write, opportunity, trade,
outbox, DIRECT handoff, or Shadow authorization occurs here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from empty_trade_status_disposition import issue_empty_trade_status_disposition
from full_session_semantic_validation import adjudicate_full_session_semantics
from full_session_transport_evidence import adjudicate_full_session_transport
from native_session_recovery_batches import (
    combine_native_session_batches, recover_native_session_batch,
)
from recovered_eb_records import recovered_signals_to_record_batches
from recovery_coverage_issuer import issue_recovery_commit_permit
from semantic_session_reconciliation import (
    assemble_semantic_session_reconciliation,
)
from sip_rest_bar_reconciliation import reconcile_sip_rest_bars
from sip_semantic_digest import canonical_sha256


SCHEMA = "OPR_EMPTY_SCOPE_SESSION_FINALIZATION_V1"


class RecoverySessionFinalizationUnsafe(RuntimeError):
    pass


def _identity(token):
    worker = getattr(token, "worker_instance_id", None)
    generation = getattr(token, "leader_generation", None)
    if (not isinstance(worker, str) or not worker
            or type(generation) is not int or generation < 1):
        raise RecoverySessionFinalizationUnsafe(
            "SESSION_FINALIZATION_LEADERSHIP_INVALID")
    return worker, generation


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RecoverySessionFinalizationUnsafe(
                "SESSION_FINALIZATION_TIME_INVALID") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RecoverySessionFinalizationUnsafe(
            "SESSION_FINALIZATION_TIME_INVALID")
    return value.astimezone(timezone.utc)


def _scope(symbols):
    if (not isinstance(symbols, (tuple, list)) or not symbols
            or len(symbols) > 12000 or len(set(symbols)) != len(symbols)
            or any(not isinstance(symbol, str) or not symbol
                   for symbol in symbols)):
        raise RecoverySessionFinalizationUnsafe(
            "SESSION_FINALIZATION_SCOPE_INVALID")
    return tuple(sorted(symbols))


def recover_native_full_scope(rest, symbols, *, session, session_start,
                              session_end, as_of, batch_size=80,
                              max_events_per_batch=100000,
                              leadership=None, expected_identity=None):
    """Fetch an exact disjoint full scope in bounded native REST batches."""
    scope = _scope(symbols)
    if (type(batch_size) is not int or not 1 <= batch_size <= 80
            or type(max_events_per_batch) is not int
            or max_events_per_batch < 1):
        raise RecoverySessionFinalizationUnsafe(
            "SESSION_FINALIZATION_BATCH_LIMIT_INVALID")
    results = []
    for index, offset in enumerate(range(0, len(scope), batch_size)):
        result = recover_native_session_batch(
            rest, list(scope[offset:offset + batch_size]),
            session=session, session_start=session_start,
            session_end=session_end, as_of=as_of, batch_index=index,
            max_events=max_events_per_batch)
        results.append(result)
        if leadership is not None:
            identity = _identity(leadership.require_current())
            if expected_identity is not None and identity != expected_identity:
                raise RecoverySessionFinalizationUnsafe(
                    "SESSION_FINALIZATION_LEADERSHIP_CHANGED_DURING_REST")
    return tuple(results)


def finalize_empty_scope_session_recovery(
        evidence, rest, symbols, scope_snapshot, leadership, *, as_of,
        batch_size=80, max_events_per_batch=100000):
    """Return a current-generation permit and E/B batches without writing."""
    scope = _scope(symbols)
    cutoff = _utc(as_of)
    before_token = leadership.require_current()
    before_identity = _identity(before_token)

    transport = adjudicate_full_session_transport(evidence)
    validation = adjudicate_full_session_semantics(
        evidence, transport, list(scope))
    disposition = issue_empty_trade_status_disposition(
        validation, scope_snapshot)
    if (disposition.get("worker_instance_id"),
            disposition.get("leader_generation")) != before_identity:
        raise RecoverySessionFinalizationUnsafe(
            "SESSION_FINALIZATION_SCOPE_LEADERSHIP_STALE")

    native_results = recover_native_full_scope(
        rest, scope, session=validation["session"],
        session_start=validation["session_start_utc"],
        session_end=validation["session_end_utc"], as_of=cutoff,
        batch_size=batch_size, max_events_per_batch=max_events_per_batch,
        leadership=leadership, expected_identity=before_identity)
    signals, native = combine_native_session_batches(
        native_results, expected_symbols=list(scope))
    bar_proof = reconcile_sip_rest_bars(
        native, evidence["semantic_ledger"])
    semantic = assemble_semantic_session_reconciliation(
        transport, validation, native, bar_proof, disposition)

    permit_token = leadership.require_current()
    if _identity(permit_token) != before_identity:
        raise RecoverySessionFinalizationUnsafe(
            "SESSION_FINALIZATION_LEADERSHIP_CHANGED_BEFORE_PERMIT")
    permit = issue_recovery_commit_permit(
        transport, semantic, permit_token, list(scope))
    worker, generation = before_identity
    record_batches, record_audit = recovered_signals_to_record_batches(
        signals, session=validation["session"], scope_symbols=list(scope),
        worker_instance_id=worker, leader_generation=generation,
        as_of=cutoff)
    if _identity(leadership.require_current()) != before_identity:
        raise RecoverySessionFinalizationUnsafe(
            "SESSION_FINALIZATION_LEADERSHIP_CHANGED_AFTER_RECORD_BUILD")

    audit = {
        "schema": SCHEMA,
        "session": validation["session"],
        "worker_instance_id": worker,
        "leader_generation": generation,
        "symbol_count": len(scope),
        "symbols_sha256": validation["symbols_sha256"],
        "market_session_evidence_sha256": transport[
            "market_session_evidence_sha256"],
        "semantic_evidence_sha256": semantic["semantic_evidence_sha256"],
        "permit_sha256": permit["permit_sha256"],
        "records_sha256": record_audit["records_sha256"],
        "record_count": record_audit["record_count"],
        "record_batch_count": record_audit["batch_count"],
        "native_batch_count": native["batch_count"],
        "rest_pagination_proven": True,
        "sip_bars_reconciled": True,
        "sip_trades_reconciled": True,
        "sip_statuses_reconciled": True,
        "full_session_coverage_proven": True,
        "redis_writes": 0,
        "opportunities_created": 0,
        "trades_created": 0,
        "outboxes_created": 0,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    audit["finalization_sha256"] = canonical_sha256(audit)
    return {
        "transport_audit": transport,
        "semantic_validation": validation,
        "native_aggregate": native,
        "bar_reconciliation": bar_proof,
        "trade_status_disposition": disposition,
        "semantic_reconciliation": semantic,
        "permit": permit,
        "record_batches": record_batches,
        "record_audit": record_audit,
        "finalization_audit": audit,
    }
