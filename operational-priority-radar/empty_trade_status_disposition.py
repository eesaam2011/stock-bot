"""Generation-fenced proof for the special case of an empty trade scope.

The ordinary active-trade recovery path remains mandatory whenever any active
or halted canonical trade exists.  This module only closes the vacuous case:
the canonical scope is read twice while the local decision lock is held and
the leader identity is checked around both reads.  A single trade blocks the
proof; it is never silently ignored.
"""
from __future__ import annotations

from datetime import datetime, timezone

from full_session_semantic_validation import SCHEMA as VALIDATION_SCHEMA
from sip_semantic_digest import canonical_sha256


SNAPSHOT_SCHEMA = "OPR_EMPTY_ACTIVE_TRADE_SCOPE_SNAPSHOT_V1"
DISPOSITION_SCHEMA = "OPR_EMPTY_TRADE_STATUS_DISPOSITION_V1"


class EmptyTradeStatusUnsafe(RuntimeError):
    pass


def _identity(token):
    worker = getattr(token, "worker_instance_id", None)
    generation = getattr(token, "leader_generation", None)
    if (not isinstance(worker, str) or not worker
            or type(generation) is not int or generation < 1):
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_LEADERSHIP_INVALID")
    return worker, generation


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_TIME_INVALID") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_TIME_INVALID")
    return value.astimezone(timezone.utc)


def _verify(document, field):
    if not isinstance(document, dict):
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_EVIDENCE_MISSING")
    body = dict(document)
    expected = body.pop(field, None)
    if expected != canonical_sha256(body):
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_EVIDENCE_DIGEST_INVALID")
    return expected


def snapshot_empty_active_trade_scope(reader, leadership, session, *,
                                      pipeline, captured_at):
    """Read an empty canonical active-trade scope under one stable leader."""
    if (not isinstance(session, str) or not session
            or not callable(getattr(reader, "active_trades", None))
            or pipeline is None
            or getattr(pipeline, "r", None) is not getattr(reader, "r", None)
            or not callable(getattr(pipeline, "_decision_gate_open", None))):
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_ARGUMENT_INVALID")
    decision_lock = getattr(pipeline, "decision_lock", None)
    if (not callable(getattr(decision_lock, "acquire", None))
            or not callable(getattr(decision_lock, "release", None))):
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_DECISION_LOCK_INVALID")
    captured = _utc(captured_at)
    with decision_lock:
        if (callable(getattr(decision_lock, "_is_owned", None))
                and not decision_lock._is_owned()):
            raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_DECISION_LOCK_NOT_HELD")
        if pipeline._decision_gate_open():
            raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_DECISION_GATE_OPEN")
        first_identity = _identity(leadership.require_current())
        first = reader.active_trades()
        middle_identity = _identity(leadership.require_current())
        second = reader.active_trades()
        last_identity = _identity(leadership.require_current())
    if first_identity != middle_identity or first_identity != last_identity:
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_LEADERSHIP_CHANGED")
    if not isinstance(first, (list, tuple)) or not isinstance(
            second, (list, tuple)):
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_READ_INVALID")
    # This specialized proof does not attempt to reconcile a non-empty scope.
    # ProductionStartupRecovery + ActiveTradeChronologicalReconciler remains
    # the only admissible path for those records.
    if first or second:
        raise EmptyTradeStatusUnsafe("ACTIVE_TRADE_SCOPE_NOT_EMPTY")
    worker, generation = first_identity
    body = {
        "schema": SNAPSHOT_SCHEMA,
        "session": session,
        "captured_at_utc": captured.isoformat(),
        "worker_instance_id": worker,
        "leader_generation": generation,
        "production_pipeline_lock_held_for_both_reads": True,
        "production_decision_gate_closed": True,
        "leadership_checked_before_between_after": True,
        "stable_double_read": True,
        "first_active_trade_count": 0,
        "second_active_trade_count": 0,
        "active_trade_count": 0,
        "halted_active_trade_count": 0,
        "nonempty_scope_requires_production_recovery": True,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }
    body["scope_snapshot_sha256"] = canonical_sha256(body)
    return body


def issue_empty_trade_status_disposition(semantic_validation, scope_snapshot):
    """Prove no trade/status state was eligible to change in the read-only run."""
    validation_sha = _verify(
        semantic_validation, "semantic_validation_sha256")
    snapshot_sha = _verify(scope_snapshot, "scope_snapshot_sha256")
    if semantic_validation.get("schema") != VALIDATION_SCHEMA:
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_VALIDATION_SCHEMA_INVALID")
    if scope_snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_SNAPSHOT_SCHEMA_INVALID")
    required_validation = {
        "all_received_messages_semantically_validated": True,
        "pipeline_writes": 0,
        "sip_trades_reconciled": False,
        "sip_statuses_reconciled": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }
    required_snapshot = {
        "production_pipeline_lock_held_for_both_reads": True,
        "production_decision_gate_closed": True,
        "leadership_checked_before_between_after": True,
        "stable_double_read": True,
        "first_active_trade_count": 0,
        "second_active_trade_count": 0,
        "active_trade_count": 0,
        "halted_active_trade_count": 0,
        "nonempty_scope_requires_production_recovery": True,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }
    for document, required in ((semantic_validation, required_validation),
                               (scope_snapshot, required_snapshot)):
        for key, expected in required.items():
            if document.get(key) != expected:
                raise EmptyTradeStatusUnsafe(
                    "EMPTY_SCOPE_REQUIREMENT_FAILED:" + key)
    session = semantic_validation.get("session")
    if session != scope_snapshot.get("session"):
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_SESSION_MISMATCH")
    start = _utc(semantic_validation.get("session_start_utc"))
    captured = _utc(scope_snapshot.get("captured_at_utc"))
    if captured > start:
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_NOT_CAPTURED_BEFORE_SESSION")
    worker = scope_snapshot.get("worker_instance_id")
    generation = scope_snapshot.get("leader_generation")
    if (not isinstance(worker, str) or not worker
            or type(generation) is not int or generation < 1):
        raise EmptyTradeStatusUnsafe("EMPTY_SCOPE_LEADERSHIP_INVALID")
    body = {
        "schema": DISPOSITION_SCHEMA,
        "session": session,
        "session_start_utc": start.isoformat(),
        "session_end_utc": semantic_validation["session_end_utc"],
        "worker_instance_id": worker,
        "leader_generation": generation,
        "semantic_validation_sha256": validation_sha,
        "scope_snapshot_sha256": snapshot_sha,
        "epoch": semantic_validation["epoch"],
        "symbol_count": semantic_validation["symbol_count"],
        "symbols_sha256": semantic_validation["symbols_sha256"],
        "trade_message_count": semantic_validation["trade_count"],
        "status_message_count": semantic_validation["status_count"],
        "active_trade_scope_count": 0,
        "halted_active_trade_scope_count": 0,
        "entry_processing_enabled": False,
        "pipeline_writes": 0,
        "trade_transition_commits": 0,
        "status_transition_commits": 0,
        "disposition_reason": "NO_ACTIVE_OR_HALTED_CANONICAL_TRADES",
        "sip_trades_reconciled": True,
        "sip_statuses_reconciled": True,
        "nonempty_scope_reconciliation_supported": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }
    body["trade_status_disposition_sha256"] = canonical_sha256(body)
    return body
