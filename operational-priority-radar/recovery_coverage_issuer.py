"""Issue the narrow E/B commit permit from two independently bound audits.

Transport observation alone is insufficient.  This issuer also requires a
digest-bound semantic session reconciliation for the same ACK epoch, counts,
window, evidence file, and symbol scope.  It performs no Redis writes and
never authorizes DIRECT, Shadow, opportunities, or retroactive entries.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from full_session_transport_evidence import SCHEMA as TRANSPORT_SCHEMA
from recovery_commit_permit import (
    SCHEMA as PERMIT_SCHEMA, symbols_sha256,
    validate_recovery_commit_permit,
)


SEMANTIC_SCHEMA = "OPR_SEMANTIC_SESSION_RECONCILIATION_V1"


class RecoveryCoverageIssuerUnsafe(RuntimeError):
    pass


def _identity(token):
    worker = getattr(token, "worker_instance_id", None)
    generation = getattr(token, "leader_generation", None)
    if (not isinstance(worker, str) or not worker
            or type(generation) is not int or generation < 1):
        raise RecoveryCoverageIssuerUnsafe("ISSUER_LEADERSHIP_INVALID")
    return worker, generation


def _utc(value):
    if not isinstance(value, str) or not value:
        raise RecoveryCoverageIssuerUnsafe("ISSUER_TIME_INVALID")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryCoverageIssuerUnsafe("ISSUER_TIME_INVALID") from exc
    if dt.tzinfo is None:
        raise RecoveryCoverageIssuerUnsafe("ISSUER_TIME_INVALID")
    return dt.astimezone(timezone.utc)


def _digest_body(document, digest_field):
    if not isinstance(document, dict):
        raise RecoveryCoverageIssuerUnsafe("ISSUER_EVIDENCE_MISSING")
    body = dict(document)
    expected = body.pop(digest_field, None)
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if expected != actual:
        raise RecoveryCoverageIssuerUnsafe("ISSUER_EVIDENCE_DIGEST_MISMATCH")
    return expected


def issue_recovery_commit_permit(transport_audit, semantic_evidence,
                                 leadership_token, symbols):
    """Return a V1 permit only when both independent contracts are exact."""
    worker, generation = _identity(leadership_token)
    if (not isinstance(symbols, (list, tuple)) or not symbols
            or len(symbols) > 12000):
        raise RecoveryCoverageIssuerUnsafe("ISSUER_SYMBOL_SCOPE_INVALID")
    symbol_hash = symbols_sha256(list(symbols))
    if (not isinstance(transport_audit, dict)
            or transport_audit.get("schema") != TRANSPORT_SCHEMA):
        raise RecoveryCoverageIssuerUnsafe("ISSUER_TRANSPORT_AUDIT_INVALID")
    required_transport = {
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch_observed": True,
        "capture_overflow_observed": False,
        "dispatch_overflow_observed": False,
        "sip_error_frame_407_observed": False,
        "transport_full_window_observed": True,
        "upstream_market_completeness_proven": False,
        "semantic_eb_replay_completed": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }
    for key, expected in required_transport.items():
        if transport_audit.get(key) is not expected:
            raise RecoveryCoverageIssuerUnsafe(
                "ISSUER_TRANSPORT_REQUIREMENT_FAILED:" + key)

    semantic_sha = _digest_body(semantic_evidence, "semantic_evidence_sha256")
    if semantic_evidence.get("schema") != SEMANTIC_SCHEMA:
        raise RecoveryCoverageIssuerUnsafe("ISSUER_SEMANTIC_SCHEMA_INVALID")
    required_semantic = {
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
        "capture_buffered": 0,
        "retroactive_entries_created": 0,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "full_session_coverage_proven": True,
    }
    for key, expected in required_semantic.items():
        if semantic_evidence.get(key) != expected:
            raise RecoveryCoverageIssuerUnsafe(
                "ISSUER_SEMANTIC_REQUIREMENT_FAILED:" + key)
    if (semantic_evidence.get("worker_instance_id") != worker
            or semantic_evidence.get("leader_generation") != generation):
        raise RecoveryCoverageIssuerUnsafe(
            "ISSUER_SEMANTIC_LEADERSHIP_MISMATCH")

    session = semantic_evidence.get("session")
    if not isinstance(session, str) or not session:
        raise RecoveryCoverageIssuerUnsafe("ISSUER_SESSION_INVALID")
    start = _utc(semantic_evidence.get("session_start_utc"))
    end = _utc(semantic_evidence.get("session_end_utc"))
    as_of = _utc(semantic_evidence.get("as_of_utc"))
    if not start < end <= as_of:
        raise RecoveryCoverageIssuerUnsafe("ISSUER_SEMANTIC_WINDOW_INVALID")

    mapping = {
        "epoch": transport_audit.get("epoch"),
        "received": transport_audit.get("received"),
        "session_start_utc": transport_audit.get("session_start_utc"),
        "session_end_utc": transport_audit.get("session_end_utc"),
        "market_session_evidence_sha256": transport_audit.get(
            "market_session_evidence_sha256"),
    }
    for key, expected in mapping.items():
        actual = semantic_evidence.get(key)
        if key.endswith("_utc"):
            if _utc(actual) != _utc(expected):
                raise RecoveryCoverageIssuerUnsafe(
                    "ISSUER_TRANSPORT_SEMANTIC_MISMATCH:" + key)
        elif actual != expected:
            raise RecoveryCoverageIssuerUnsafe(
                "ISSUER_TRANSPORT_SEMANTIC_MISMATCH:" + key)
    received = mapping["received"]
    epoch = mapping["epoch"]
    if (type(received) is not int or received < 1
            or type(epoch) is not int or epoch < 1
            or semantic_evidence.get("handled") != received
            or semantic_evidence.get("metrics_acked") != received
            or semantic_evidence.get("first_sequence") != 1
            or semantic_evidence.get("last_sequence") != received
            or semantic_evidence.get("capture_acked_upto") != received
            or semantic_evidence.get("dispatch_overflows") != 0):
        raise RecoveryCoverageIssuerUnsafe("ISSUER_COUNT_OR_SEQUENCE_MISMATCH")
    if (semantic_evidence.get("symbol_count") != len(symbols)
            or semantic_evidence.get("symbols_sha256") != symbol_hash):
        raise RecoveryCoverageIssuerUnsafe("ISSUER_SYMBOL_SCOPE_MISMATCH")

    body = {
        "schema": PERMIT_SCHEMA,
        "worker_instance_id": worker,
        "leader_generation": generation,
        "session": session,
        "symbol_count": len(symbols),
        "symbols_sha256": symbol_hash,
        "session_start_utc": start.isoformat(),
        "session_end_utc": end.isoformat(),
        "as_of_utc": as_of.isoformat(),
        "epoch": epoch,
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch": True,
        "received": received,
        "handled": received,
        "metrics_acked": received,
        "first_sequence": 1,
        "last_sequence": received,
        "capture_acked_upto": received,
        "capture_buffered": 0,
        "capture_overflow_observed": False,
        "dispatch_overflow_observed": False,
        "dispatch_overflows": 0,
        "rest_pagination_proven": True,
        "native_session_reconciled": True,
        "sip_bars_reconciled": True,
        "sip_trades_reconciled": True,
        "sip_statuses_reconciled": True,
        "full_session_coverage_proven": True,
        "market_session_evidence_sha256": mapping[
            "market_session_evidence_sha256"],
        "semantic_reconciliation_evidence_sha256": semantic_sha,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    body["permit_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    validate_recovery_commit_permit(
        body, worker_instance_id=worker, leader_generation=generation,
        session=session, symbols=sorted(symbols))
    return body
