"""Commit a fully finalized recovered E/B session in fenced bounded batches."""
from __future__ import annotations

from recovered_eb_records import recovered_records_sha256
from recovery_commit_permit import validate_recovery_commit_permit
from recovery_session_finalizer import SCHEMA as FINALIZATION_SCHEMA
from sip_semantic_digest import canonical_sha256
from state_store import record_key


SCHEMA = "OPR_RECOVERY_SESSION_COMMIT_V1"


class RecoverySessionCommitUnsafe(RuntimeError):
    pass


def _identity(token):
    worker = getattr(token, "worker_instance_id", None)
    generation = getattr(token, "leader_generation", None)
    if (not isinstance(worker, str) or not worker
            or type(generation) is not int or generation < 1):
        raise RecoverySessionCommitUnsafe("SESSION_COMMIT_LEADERSHIP_INVALID")
    return worker, generation


def _verify(document, field, error):
    if not isinstance(document, dict):
        raise RecoverySessionCommitUnsafe(error)
    body = dict(document)
    expected = body.pop(field, None)
    if expected != canonical_sha256(body):
        raise RecoverySessionCommitUnsafe(error)
    return expected


def commit_finalized_eb_session(finalization, committer, leadership, symbols):
    """Apply only the canonical batches authorized by one finalization."""
    if (not isinstance(finalization, dict)
            or not isinstance(symbols, (tuple, list)) or not symbols
            or len(symbols) > 12000 or len(set(symbols)) != len(symbols)
            or any(not isinstance(symbol, str) or not symbol
                   for symbol in symbols)
            or not callable(getattr(committer, "commit", None))):
        raise RecoverySessionCommitUnsafe("SESSION_COMMIT_ARGUMENT_INVALID")
    scope = sorted(symbols)
    permit = finalization.get("permit")
    record_audit = finalization.get("record_audit")
    audit = finalization.get("finalization_audit")
    batches = finalization.get("record_batches")
    _verify(audit, "finalization_sha256", "SESSION_COMMIT_AUDIT_INVALID")
    required_audit = {
        "schema": FINALIZATION_SCHEMA,
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
    for key, expected in required_audit.items():
        if audit.get(key) != expected:
            raise RecoverySessionCommitUnsafe(
                "SESSION_COMMIT_FINALIZATION_REQUIREMENT_FAILED:" + key)
    required_records = {
        "schema": "OPR_RECOVERED_EB_RECORD_BATCHES_V1",
        "max_records_per_batch": 80,
        "opportunities_created": 0,
        "trades_created": 0,
        "outboxes_created": 0,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    if not isinstance(record_audit, dict):
        raise RecoverySessionCommitUnsafe("SESSION_COMMIT_RECORD_AUDIT_INVALID")
    for key, expected in required_records.items():
        if record_audit.get(key) != expected:
            raise RecoverySessionCommitUnsafe(
                "SESSION_COMMIT_RECORD_REQUIREMENT_FAILED:" + key)
    if not isinstance(batches, (tuple, list)):
        raise RecoverySessionCommitUnsafe("SESSION_COMMIT_BATCHES_INVALID")
    records = []
    for batch in batches:
        if not isinstance(batch, (tuple, list)) or not 1 <= len(batch) <= 80:
            raise RecoverySessionCommitUnsafe("SESSION_COMMIT_BATCH_INVALID")
        records.extend(batch)
    session = audit.get("session")
    records_sha = recovered_records_sha256(
        records, session=session, scope_symbols=scope)
    keys = []
    for record in records:
        if (record.get("session") != session
                or record.get("symbol") not in scope
                or record.get("worker_instance_id")
                != audit.get("worker_instance_id")
                or record.get("leader_generation")
                != audit.get("leader_generation")):
            raise RecoverySessionCommitUnsafe(
                "SESSION_COMMIT_RECORD_SCOPE_OR_LEADERSHIP_INVALID")
        keys.append(record_key(record))
    if len(keys) != len(set(keys)):
        raise RecoverySessionCommitUnsafe("SESSION_COMMIT_DUPLICATE_RECORD_KEY")
    if (record_audit.get("session") != session
            or record_audit.get("scope_symbol_count") != len(scope)
            or record_audit.get("record_count") != len(records)
            or record_audit.get("batch_count") != len(batches)
            or record_audit.get("records_sha256") != records_sha
            or audit.get("record_count") != len(records)
            or audit.get("record_batch_count") != len(batches)
            or audit.get("records_sha256") != records_sha
            or audit.get("symbol_count") != len(scope)
            or not isinstance(permit, dict)
            or audit.get("symbols_sha256") != permit.get("symbols_sha256")
            or audit.get("market_session_evidence_sha256")
            != permit.get("market_session_evidence_sha256")
            or audit.get("semantic_evidence_sha256")
            != permit.get("semantic_reconciliation_evidence_sha256")
            or audit.get("permit_sha256") != permit.get("permit_sha256")):
        raise RecoverySessionCommitUnsafe("SESSION_COMMIT_RECORD_BINDING_INVALID")

    before = leadership.require_current()
    worker, generation = _identity(before)
    if (audit.get("worker_instance_id") != worker
            or audit.get("leader_generation") != generation):
        raise RecoverySessionCommitUnsafe("SESSION_COMMIT_LEADERSHIP_MISMATCH")
    validate_recovery_commit_permit(
        permit, worker_instance_id=worker, leader_generation=generation,
        session=session, symbols=scope)
    inserted = 0
    identical = 0
    for batch in batches:
        result = committer.commit(
            list(batch), permit, coverage_scope_symbols=scope)
        if (not isinstance(result, dict)
                or type(result.get("inserted")) is not int
                or type(result.get("already_identical")) is not int
                or result["inserted"] < 0
                or result["already_identical"] < 0):
            raise RecoverySessionCommitUnsafe("SESSION_COMMIT_RESULT_INVALID")
        inserted += result["inserted"]
        identical += result["already_identical"]
    if inserted + identical != len(records):
        raise RecoverySessionCommitUnsafe("SESSION_COMMIT_COUNT_MISMATCH")
    if _identity(leadership.require_current()) != (worker, generation):
        raise RecoverySessionCommitUnsafe(
            "SESSION_COMMIT_LEADERSHIP_CHANGED_AFTER_BATCHES")
    body = {
        "schema": SCHEMA,
        "session": session,
        "worker_instance_id": worker,
        "leader_generation": generation,
        "symbol_count": len(scope),
        "permit_sha256": permit["permit_sha256"],
        "finalization_sha256": audit["finalization_sha256"],
        "records_sha256": records_sha,
        "record_count": len(records),
        "batch_count": len(batches),
        "inserted": inserted,
        "already_identical": identical,
        "opportunities_created": 0,
        "trades_created": 0,
        "outboxes_created": 0,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    body["commit_sha256"] = canonical_sha256(body)
    return body
