"""Adjudicate a full live session's payload-free semantic validation.

This contract joins the exact raw live evidence with its recomputed transport
audit and semantic ledger.  It proves that every received item was validated
and accounted for in one epoch.  It deliberately does not claim REST/SIP
equality, trade/status reconciliation, E/B coverage, DIRECT, or Shadow.
"""
from __future__ import annotations

from full_session_transport_evidence import adjudicate_full_session_transport
from sip_semantic_digest import canonical_sha256


SCHEMA = "OPR_FULL_SESSION_SEMANTIC_VALIDATION_V1"
LEDGER_SCHEMA = "OPR_LIVE_SIP_SEMANTIC_LEDGER_V1"


class FullSessionSemanticUnsafe(RuntimeError):
    pass


def _signed(document, digest_field):
    if not isinstance(document, dict):
        raise FullSessionSemanticUnsafe("SEMANTIC_EVIDENCE_MISSING")
    body = dict(document)
    expected = body.pop(digest_field, None)
    if expected != canonical_sha256(body):
        raise FullSessionSemanticUnsafe("SEMANTIC_EVIDENCE_DIGEST_INVALID")
    return expected


def _digest(value, error):
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise FullSessionSemanticUnsafe(error)
    return value


def adjudicate_full_session_semantics(evidence, transport_audit, symbols):
    """Return a narrow signed validation audit or fail closed."""
    evidence_sha = _signed(evidence, "evidence_sha256")
    expected_transport = adjudicate_full_session_transport(evidence)
    if transport_audit != expected_transport:
        raise FullSessionSemanticUnsafe("SEMANTIC_TRANSPORT_AUDIT_MISMATCH")
    if (not isinstance(symbols, (tuple, list)) or not symbols
            or len(symbols) > 12000 or len(set(symbols)) != len(symbols)
            or any(not isinstance(symbol, str) or not symbol
                   for symbol in symbols)):
        raise FullSessionSemanticUnsafe("SEMANTIC_SYMBOL_SCOPE_INVALID")

    ledger = evidence.get("semantic_ledger")
    ledger_sha = _signed(ledger, "snapshot_sha256")
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise FullSessionSemanticUnsafe("SEMANTIC_LEDGER_SCHEMA_INVALID")
    window = evidence.get("session_window")
    payload_ledger = evidence.get("payload_free_ledger")
    claims = evidence.get("claims")
    if not all(isinstance(value, dict) for value in (
            window, payload_ledger, claims)):
        raise FullSessionSemanticUnsafe("SEMANTIC_EVIDENCE_INCOMPLETE")

    received = expected_transport["received"]
    counts = {kind: ledger.get(f"{kind.lower()}_count")
              for kind in ("BAR", "TRADE", "STATUS")}
    payload_counts = payload_ledger.get("counts")
    if (any(type(value) is not int or value < 0 for value in counts.values())
            or sum(counts.values()) != received
            or ledger.get("item_count") != received
            or ledger.get("epoch") != expected_transport["epoch"]
            or ledger.get("first_sequence") != 1
            or ledger.get("last_sequence") != received
            or payload_counts != counts):
        raise FullSessionSemanticUnsafe("SEMANTIC_COUNT_OR_SEQUENCE_MISMATCH")
    window_bars = ledger.get("bar_window_count")
    if (type(window_bars) is not int or not 0 <= window_bars <= counts["BAR"]):
        raise FullSessionSemanticUnsafe("SEMANTIC_BAR_WINDOW_COUNT_INVALID")

    symbol_hash = canonical_sha256(sorted(symbols))
    session_start = expected_transport["session_start_utc"]
    session_end = expected_transport["session_end_utc"]
    if (evidence.get("symbols_requested") != len(symbols)
            or ledger.get("symbol_count") != len(symbols)
            or ledger.get("symbols_sha256") != symbol_hash
            or ledger.get("bar_window_start_utc") != session_start
            or ledger.get("bar_window_end_utc") != session_end
            or ledger.get("session") != session_start[:10]):
        raise FullSessionSemanticUnsafe("SEMANTIC_SCOPE_OR_WINDOW_MISMATCH")
    semantic_chain = _digest(
        ledger.get("semantic_chain_sha256"), "SEMANTIC_CHAIN_INVALID")
    bar_multiset = _digest(
        ledger.get("bar_multiset_sha256"), "SEMANTIC_BAR_MULTISET_INVALID")

    required_ledger = {
        "payloads_retained": 0,
        "entry_processing_enabled": False,
        "active_trade_scope_count": 0,
        "halted_trade_scope_count": 0,
        "pipeline_writes": 0,
        "trade_transition_commits": 0,
        "status_transition_commits": 0,
        "sip_semantics_validated": True,
        "sip_semantics_reconciled": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "retroactive_entries_allowed": False,
    }
    for key, expected in required_ledger.items():
        if ledger.get(key) is not expected:
            raise FullSessionSemanticUnsafe(
                "SEMANTIC_LEDGER_REQUIREMENT_FAILED:" + key)
    required_claims = {
        "transport_measurement_only": True,
        "semantic_validation_only": True,
        "production_generation_fence_proven": False,
        "full_session_coverage_proven": False,
        "sip_continuity_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }
    for key, expected in required_claims.items():
        if claims.get(key) is not expected:
            raise FullSessionSemanticUnsafe(
                "SEMANTIC_CLAIM_ESCALATION:" + key)

    body = {
        "schema": SCHEMA,
        "session": ledger["session"],
        "session_start_utc": session_start,
        "session_end_utc": session_end,
        "market_session_evidence_sha256": evidence_sha,
        "semantic_ledger_snapshot_sha256": ledger_sha,
        "epoch": expected_transport["epoch"],
        "received": received,
        "handled": received,
        "metrics_acked": received,
        "first_sequence": 1,
        "last_sequence": received,
        "symbol_count": len(symbols),
        "symbols_sha256": symbol_hash,
        "bar_count": counts["BAR"],
        "trade_count": counts["TRADE"],
        "status_count": counts["STATUS"],
        "bar_window_count": window_bars,
        "semantic_chain_sha256": semantic_chain,
        "bar_multiset_sha256": bar_multiset,
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
    }
    body["semantic_validation_sha256"] = canonical_sha256(body)
    return body
