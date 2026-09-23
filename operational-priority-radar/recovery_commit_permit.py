"""Strict, digest-bound permit for historical E/B recovery commits.

The permit is intentionally separate from the recovery implementation.  A
caller may only construct it from independently retained session evidence;
this module validates the complete contract and never infers coverage from
REST pagination or a local SIP sequence by themselves.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


SCHEMA = "OPR_RECOVERY_COMMIT_PERMIT_V1"


class RecoveryCommitPermitUnsafe(RuntimeError):
    pass


def _utc(value):
    if not isinstance(value, str) or not value:
        raise RecoveryCommitPermitUnsafe("PERMIT_TIME_INVALID")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryCommitPermitUnsafe("PERMIT_TIME_INVALID") from exc
    if result.tzinfo is None:
        raise RecoveryCommitPermitUnsafe("PERMIT_TIME_INVALID")
    return result.astimezone(timezone.utc)


def symbols_sha256(symbols):
    if (not isinstance(symbols, (list, tuple)) or not symbols
            or len(set(symbols)) != len(symbols)
            or any(not isinstance(s, str) or not s for s in symbols)):
        raise RecoveryCommitPermitUnsafe("PERMIT_SYMBOLS_INVALID")
    raw = json.dumps(sorted(symbols), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_body(proof):
    body = dict(proof)
    body.pop("permit_sha256", None)
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def validate_recovery_commit_permit(proof, *, worker_instance_id,
                                    leader_generation, session, symbols):
    """Validate an independently produced full-session recovery permit.

    Success authorizes only idempotent E/B insertion.  It never authorizes a
    retroactive opportunity, DIRECT processing, or a Shadow deployment.
    """
    if not isinstance(proof, dict):
        raise RecoveryCommitPermitUnsafe("PERMIT_MISSING")
    if proof.get("schema") != SCHEMA:
        raise RecoveryCommitPermitUnsafe("PERMIT_SCHEMA_INVALID")
    if (not isinstance(worker_instance_id, str) or not worker_instance_id
            or isinstance(leader_generation, bool)
            or not isinstance(leader_generation, int)
            or leader_generation < 1
            or not isinstance(session, str) or not session):
        raise RecoveryCommitPermitUnsafe("PERMIT_EXPECTED_IDENTITY_INVALID")
    if (proof.get("worker_instance_id") != worker_instance_id
            or proof.get("leader_generation") != leader_generation
            or proof.get("session") != session):
        raise RecoveryCommitPermitUnsafe("PERMIT_IDENTITY_MISMATCH")
    if (proof.get("symbol_count") != len(symbols)
            or proof.get("symbols_sha256") != symbols_sha256(symbols)):
        raise RecoveryCommitPermitUnsafe("PERMIT_SYMBOL_SCOPE_MISMATCH")
    start, end, as_of = map(_utc, (proof.get("session_start_utc"),
                                  proof.get("session_end_utc"),
                                  proof.get("as_of_utc")))
    if not start < end <= as_of:
        raise RecoveryCommitPermitUnsafe("PERMIT_WINDOW_INVALID")
    epoch = proof.get("epoch")
    received = proof.get("received")
    if (isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1
            or isinstance(received, bool) or not isinstance(received, int)
            or received < 1):
        raise RecoveryCommitPermitUnsafe("PERMIT_COUNTS_INVALID")
    exact = {
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch": True,
        "rest_pagination_proven": True,
        "native_session_reconciled": True,
        "sip_trades_reconciled": True,
        "sip_statuses_reconciled": True,
        "capture_overflow_observed": False,
        "dispatch_overflow_observed": False,
        "full_session_coverage_proven": True,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    for key, value in exact.items():
        if proof.get(key) is not value:
            raise RecoveryCommitPermitUnsafe("PERMIT_REQUIREMENT_FAILED:" + key)
    if (proof.get("handled") != received
            or proof.get("metrics_acked") != received
            or proof.get("first_sequence") != 1
            or proof.get("last_sequence") != received
            or proof.get("capture_acked_upto") != received
            or proof.get("capture_buffered") != 0
            or proof.get("dispatch_overflows") != 0):
        raise RecoveryCommitPermitUnsafe("PERMIT_RECONCILIATION_MISMATCH")
    evidence_sha = proof.get("market_session_evidence_sha256")
    semantic_sha = proof.get("semantic_reconciliation_evidence_sha256")
    permit_sha = proof.get("permit_sha256")
    if (not isinstance(evidence_sha, str) or len(evidence_sha) != 64
            or not isinstance(semantic_sha, str) or len(semantic_sha) != 64
            or not isinstance(permit_sha, str) or len(permit_sha) != 64):
        raise RecoveryCommitPermitUnsafe("PERMIT_DIGEST_MISSING")
    try:
        int(evidence_sha, 16); int(semantic_sha, 16); int(permit_sha, 16)
    except ValueError as exc:
        raise RecoveryCommitPermitUnsafe("PERMIT_DIGEST_INVALID") from exc
    actual = hashlib.sha256(_canonical_body(proof).encode("utf-8")).hexdigest()
    if actual != permit_sha:
        raise RecoveryCommitPermitUnsafe("PERMIT_DIGEST_MISMATCH")
    return {
        "schema": SCHEMA,
        "session": session,
        "epoch": epoch,
        "symbol_count": len(symbols),
        "permit_sha256": permit_sha,
        "market_session_evidence_sha256": evidence_sha,
        "semantic_reconciliation_evidence_sha256": semantic_sha,
        "full_session_coverage_proven": True,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
