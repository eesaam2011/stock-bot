"""Bounded production-native session replay batches.

This module turns independently exhausted Alpaca REST page chains into
validated first-observed session E/B signals.  It deliberately stops before
claiming SIP continuity or full-session coverage: those claims require the
separate full-session transport and semantic reconciliation contracts.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from recovery_chronology import _utc, plan_native_batch
from recovery_session_replay import EARLY_WARMUP_MINUTES, reconstruct_session_signals


SCHEMA = "OPR_NATIVE_SESSION_RECOVERY_BATCH_V1"
AGGREGATE_SCHEMA = "OPR_NATIVE_SESSION_RECOVERY_AGGREGATE_V1"


class NativeSessionRecoveryUnsafe(RuntimeError):
    pass


def _digest(body):
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _signal_body(signal):
    return {
        "kind": signal.kind,
        "symbol": signal.symbol,
        "session": signal.session,
        "bar_start_ts": _utc(signal.bar_start_ts).isoformat(),
        "bar_end_ts": _utc(signal.bar_end_ts).isoformat(),
        "recovered_at": _utc(signal.recovered_at).isoformat(),
        "decision_available_ts": _utc(signal.decision_available_ts).isoformat(),
        "score": signal.score,
        "observed_weight": signal.observed_weight,
        "features": signal.features,
        "diagnostics": signal.diagnostics,
    }


def _validate_scope(symbols, *, maximum=80):
    if (not isinstance(symbols, (tuple, list)) or not symbols
            or len(symbols) > maximum or len(set(symbols)) != len(symbols)
            or any(not isinstance(symbol, str) or not symbol
                   for symbol in symbols)):
        raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_SCOPE_INVALID")
    return tuple(sorted(symbols))


def recover_native_session_batch(rest, symbols, *, session, session_start,
                                 session_end, as_of, batch_index,
                                 max_events=100000):
    """Fetch, validate and replay one disjoint batch of at most 80 symbols."""
    scope = _validate_scope(symbols)
    if (not isinstance(session, str) or not session
            or type(batch_index) is not int or batch_index < 0
            or type(max_events) is not int or max_events < 1
            or not callable(getattr(rest, "native_recovery_batch_audited", None))):
        raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_ARGUMENT_INVALID")
    start, end, recovered = map(_utc, (session_start, session_end, as_of))
    if not start < end <= recovered:
        raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_WINDOW_INVALID")
    requested = start - timedelta(minutes=EARLY_WARMUP_MINUTES)
    rows1, rows5, pagination = rest.native_recovery_batch_audited(
        list(scope), requested, end, batch_size=len(scope), max_workers=2)
    if (not isinstance(pagination, dict)
            or pagination.get("both_api_page_chains_exhausted") is not True
            or not isinstance(pagination.get("native_1m"), dict)
            or not isinstance(pagination.get("native_5m"), dict)
            or pagination["native_1m"].get("api_pagination_exhausted") is not True
            or pagination["native_5m"].get("api_pagination_exhausted") is not True):
        raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_PAGINATION_UNPROVEN")
    plan, chronology = plan_native_batch(
        rows1, rows5, scope, window_start=requested, window_end=end,
        recovered_at=recovered, max_events=max_events)
    signals, replay = reconstruct_session_signals(
        plan, session, session_start=start, session_end=end,
        requested_start=requested, recovered_at=recovered,
        max_events=max_events)
    if (replay.get("warmup_window_requested") is not True
            or any(signal.symbol not in scope for signal in signals)):
        raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_REPLAY_UNSAFE")
    signal_rows = [_signal_body(signal) for signal in signals]
    body = {
        "schema": SCHEMA,
        "batch_index": batch_index,
        "session": session,
        "session_start_utc": start.isoformat(),
        "session_end_utc": end.isoformat(),
        "requested_start_utc": requested.isoformat(),
        "as_of_utc": recovered.isoformat(),
        "symbols": list(scope),
        "symbol_count": len(scope),
        "symbols_sha256": _digest(list(scope)),
        "native_1m_rows": chronology["native_1m"],
        "native_5m_rows": chronology["native_5m"],
        "native_events": chronology["event_count"],
        "chronological_sha256": chronology["chronological_sha256"],
        "observed_interbar_gaps": chronology["observed_interbar_gaps"],
        "empty_symbol_timeframe_lanes": chronology[
            "empty_symbol_timeframe_lanes"],
        "rest_pages_1m": pagination["native_1m"].get("pages"),
        "rest_pages_5m": pagination["native_5m"].get("pages"),
        "rest_pagination_proven": True,
        "warmup_window_requested": True,
        "signal_count": len(signals),
        "signal_rows": signal_rows,
        "signals_sha256": _digest(signal_rows),
        "first_observed_session_E": replay["session_observed_E"],
        "first_observed_session_B": replay["session_observed_B"],
        "native_batch_replayed": True,
        "sip_semantics_reconciled": False,
        "full_session_coverage_proven": False,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    body["batch_evidence_sha256"] = _digest(body)
    return signals, body


def combine_native_session_batches(batch_results, *, expected_symbols):
    """Prove an exact, disjoint REST partition without upgrading SIP claims."""
    expected = _validate_scope(expected_symbols, maximum=12000)
    if not isinstance(batch_results, (tuple, list)) or not batch_results:
        raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_BATCHES_MISSING")
    common = None
    seen_symbols = set()
    seen_indices = set()
    signals = []
    proofs = []
    identities = set()
    for result in batch_results:
        if (not isinstance(result, (tuple, list)) or len(result) != 2
                or not isinstance(result[1], dict)):
            raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_BATCH_INVALID")
        batch_signals, proof = result
        signed = dict(proof)
        claimed = signed.pop("batch_evidence_sha256", None)
        if proof.get("schema") != SCHEMA or claimed != _digest(signed):
            raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_BATCH_DIGEST_INVALID")
        if (proof.get("rest_pagination_proven") is not True
                or proof.get("native_batch_replayed") is not True
                or proof.get("sip_semantics_reconciled") is not False
                or proof.get("full_session_coverage_proven") is not False
                or proof.get("retroactive_entries_allowed") is not False
                or proof.get("direct_handoff_authorized") is not False
                or proof.get("shadow_deploy_authorized") is not False):
            raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_BATCH_CLAIM_INVALID")
        index = proof.get("batch_index")
        scope = proof.get("symbols")
        if (type(index) is not int or index < 0 or index in seen_indices
                or not isinstance(scope, list) or scope != sorted(scope)
                or proof.get("symbol_count") != len(scope)
                or proof.get("symbols_sha256") != _digest(scope)
                or seen_symbols.intersection(scope)):
            raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_PARTITION_INVALID")
        fields = tuple(proof.get(key) for key in (
            "session", "session_start_utc", "session_end_utc",
            "requested_start_utc", "as_of_utc"))
        if common is None:
            common = fields
        elif fields != common:
            raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_WINDOW_MISMATCH")
        rows = [_signal_body(signal) for signal in batch_signals]
        if (proof.get("signal_count") != len(rows)
                or proof.get("signal_rows") != rows
                or proof.get("signals_sha256") != _digest(rows)):
            raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_SIGNALS_MISMATCH")
        for signal in batch_signals:
            identity = (signal.symbol, signal.kind)
            if identity in identities:
                raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_SIGNAL_DUPLICATE")
            identities.add(identity)
        seen_indices.add(index)
        seen_symbols.update(scope)
        signals.extend(batch_signals)
        proofs.append(proof)
    if seen_indices != set(range(len(batch_results))) or seen_symbols != set(expected):
        raise NativeSessionRecoveryUnsafe("NATIVE_SESSION_PARTITION_INCOMPLETE")
    signals.sort(key=lambda item: (
        item.bar_end_ts, item.bar_start_ts, item.symbol, item.kind))
    proof_digests = [proof["batch_evidence_sha256"]
                     for proof in sorted(proofs, key=lambda item: item["batch_index"])]
    body = {
        "schema": AGGREGATE_SCHEMA,
        "session": common[0],
        "session_start_utc": common[1],
        "session_end_utc": common[2],
        "requested_start_utc": common[3],
        "as_of_utc": common[4],
        "symbol_count": len(expected),
        "symbols_sha256": _digest(list(expected)),
        "batch_count": len(proofs),
        "batch_evidence_sha256": proof_digests,
        "native_1m_rows": sum(p["native_1m_rows"] for p in proofs),
        "native_5m_rows": sum(p["native_5m_rows"] for p in proofs),
        "native_events": sum(p["native_events"] for p in proofs),
        "observed_interbar_gaps": sum(p["observed_interbar_gaps"] for p in proofs),
        "empty_symbol_timeframe_lanes": sum(
            p["empty_symbol_timeframe_lanes"] for p in proofs),
        "signal_count": len(signals),
        "signals_sha256": _digest([_signal_body(signal) for signal in signals]),
        "exact_disjoint_symbol_partition": True,
        "all_rest_page_chains_exhausted": True,
        "native_session_batches_replayed": True,
        "absence_of_bars_is_not_feed_loss_proof": True,
        "sip_semantics_reconciled": False,
        "full_session_coverage_proven": False,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    body["aggregate_evidence_sha256"] = _digest(body)
    return tuple(signals), body
