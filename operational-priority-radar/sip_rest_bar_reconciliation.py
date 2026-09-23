"""Exact, digest-bound equality proof for regular-session SIP and REST bars."""
from __future__ import annotations

import json

from native_session_recovery_batches import AGGREGATE_SCHEMA
from production_sip_semantic_journal import JOURNAL_SCHEMA
from sip_semantic_digest import canonical_sha256


SCHEMA = "OPR_SIP_REST_BAR_RECONCILIATION_V1"


class SIPRESTBarReconciliationUnsafe(RuntimeError):
    pass


def _verify(document, field):
    if not isinstance(document, dict):
        raise SIPRESTBarReconciliationUnsafe("BAR_RECONCILIATION_EVIDENCE_MISSING")
    body = dict(document)
    expected = body.pop(field, None)
    if expected != canonical_sha256(body):
        raise SIPRESTBarReconciliationUnsafe("BAR_RECONCILIATION_DIGEST_INVALID")
    return expected


def reconcile_sip_rest_bars(native_evidence, semantic_snapshot):
    native_sha = _verify(native_evidence, "aggregate_evidence_sha256")
    semantic_sha = _verify(semantic_snapshot, "snapshot_sha256")
    if (native_evidence.get("schema") != AGGREGATE_SCHEMA
            or semantic_snapshot.get("schema") != JOURNAL_SCHEMA):
        raise SIPRESTBarReconciliationUnsafe("BAR_RECONCILIATION_SCHEMA_INVALID")
    required_native = {
        "exact_disjoint_symbol_partition": True,
        "all_rest_page_chains_exhausted": True,
        "native_session_batches_replayed": True,
        "sip_semantics_reconciled": False,
        "full_session_coverage_proven": False,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    required_semantic = {
        "payloads_retained": 0,
        "sip_semantics_validated": True,
        "sip_semantics_reconciled": False,
        "full_session_coverage_proven": False,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
    }
    for document, requirements in ((native_evidence, required_native),
                                   (semantic_snapshot, required_semantic)):
        for key, expected in requirements.items():
            if document.get(key) != expected:
                raise SIPRESTBarReconciliationUnsafe(
                    "BAR_RECONCILIATION_REQUIREMENT_FAILED:" + key)
    if (native_evidence.get("session") != semantic_snapshot.get("session")
            or native_evidence.get("symbols_sha256")
            != semantic_snapshot.get("symbols_sha256")
            or native_evidence.get("session_start_utc")
            != semantic_snapshot.get("bar_window_start_utc")
            or native_evidence.get("session_end_utc")
            != semantic_snapshot.get("bar_window_end_utc")):
        raise SIPRESTBarReconciliationUnsafe("BAR_RECONCILIATION_SCOPE_MISMATCH")
    rest_count = native_evidence.get("session_native_1m_bar_count")
    sip_count = semantic_snapshot.get("bar_window_count")
    rest_digest = native_evidence.get("session_native_1m_bar_multiset_sha256")
    sip_digest = semantic_snapshot.get("bar_multiset_sha256")
    if (type(rest_count) is not int or rest_count < 0
            or type(sip_count) is not int or sip_count < 0
            or rest_count != sip_count or rest_digest != sip_digest):
        raise SIPRESTBarReconciliationUnsafe("BAR_RECONCILIATION_MULTISET_MISMATCH")
    body = {
        "schema": SCHEMA,
        "session": native_evidence["session"],
        "session_start_utc": native_evidence["session_start_utc"],
        "session_end_utc": native_evidence["session_end_utc"],
        "symbols_sha256": native_evidence["symbols_sha256"],
        "epoch": semantic_snapshot["epoch"],
        "native_aggregate_evidence_sha256": native_sha,
        "sip_semantic_snapshot_sha256": semantic_sha,
        "bar_count": rest_count,
        "bar_multiset_sha256": rest_digest,
        "rest_pagination_proven": True,
        "native_session_batches_replayed": True,
        "sip_bars_reconciled": True,
        "bar_reconciliation_gaps": 0,
        "bar_reconciliation_conflicts": 0,
        "sip_trades_reconciled": False,
        "sip_statuses_reconciled": False,
        "full_session_coverage_proven": False,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    body["bar_reconciliation_sha256"] = canonical_sha256(body)
    return body
