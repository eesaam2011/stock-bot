"""Convert validated recovered E/B signals into canonical, non-actionable records."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from early_core_score import ARTIFACT
from recovery_signal_replay import RecoveredSignal
from state_store import base_record, canonical_json, record_key, validate_record


class RecoveredEBRecordUnsafe(RuntimeError):
    pass


_B_FEATURES = ("opportunity", "failure_pressure")
_B_DIAGNOSTICS = (
    "price", "vwap", "demand_efficiency", "price_acceptance",
    "volume_acceleration",
)


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RecoveredEBRecordUnsafe("RECOVERED_EB_TIME_INVALID")
    return value.astimezone(timezone.utc)


def _finite(value):
    return (type(value) in (int, float)
            and math.isfinite(float(value)))


def recovered_records_sha256(records, *, session, scope_symbols):
    """Digest one exact canonical recovered-record set and its full scope."""
    if (not isinstance(records, (tuple, list))
            or not isinstance(session, str) or not session
            or not isinstance(scope_symbols, (tuple, list))
            or not scope_symbols or len(set(scope_symbols)) != len(scope_symbols)
            or any(not isinstance(symbol, str) or not symbol
                   for symbol in scope_symbols)):
        raise RecoveredEBRecordUnsafe("RECOVERED_EB_DIGEST_ARGUMENT_INVALID")
    canonical_records = []
    for record in records:
        if (not isinstance(record, dict)
                or record.get("record_type") not in {"early_core", "base_ready"}):
            raise RecoveredEBRecordUnsafe("RECOVERED_EB_DIGEST_RECORD_INVALID")
        validate_record(record, record["record_type"])
        canonical_records.append(json.loads(canonical_json(record)))
    digest_body = {
        "session": session,
        "scope_symbols_sha256": hashlib.sha256(json.dumps(
            sorted(scope_symbols), separators=(",", ":")).encode()).hexdigest(),
        "record_count": len(records),
        "records": canonical_records,
    }
    raw = json.dumps(digest_body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def recovered_signals_to_record_batches(
        signals, *, session, scope_symbols, worker_instance_id,
        leader_generation, as_of, max_records_per_batch=80):
    """Build canonical E/B batches without creating any actionable state."""
    if (not isinstance(signals, (tuple, list))
            or not isinstance(session, str) or not session
            or not isinstance(scope_symbols, (tuple, list)) or not scope_symbols
            or len(set(scope_symbols)) != len(scope_symbols)
            or any(not isinstance(s, str) or not s for s in scope_symbols)
            or not isinstance(worker_instance_id, str) or not worker_instance_id
            or type(leader_generation) is not int or leader_generation < 1
            or type(max_records_per_batch) is not int
            or not 1 <= max_records_per_batch <= 80):
        raise RecoveredEBRecordUnsafe("RECOVERED_EB_ARGUMENT_INVALID")
    cutoff = _utc(as_of)
    scope = set(scope_symbols)
    records = []
    seen = set()
    for signal in signals:
        if not isinstance(signal, RecoveredSignal):
            raise RecoveredEBRecordUnsafe("RECOVERED_EB_SIGNAL_INVALID")
        if (signal.kind not in {"E", "B"} or signal.session != session
                or signal.symbol not in scope):
            raise RecoveredEBRecordUnsafe("RECOVERED_EB_SIGNAL_SCOPE_INVALID")
        identity = (signal.symbol, signal.kind)
        if identity in seen:
            raise RecoveredEBRecordUnsafe("RECOVERED_EB_DUPLICATE_SIGNAL")
        seen.add(identity)
        start = _utc(signal.bar_start_ts)
        end = _utc(signal.bar_end_ts)
        recovered = _utc(signal.recovered_at)
        available = _utc(signal.decision_available_ts)
        expected_delta = 300 if signal.kind == "E" else 60
        if (int((end - start).total_seconds()) != expected_delta
                or not end <= recovered <= available <= cutoff):
            raise RecoveredEBRecordUnsafe("RECOVERED_EB_CHRONOLOGY_INVALID")
        record_type = "early_core" if signal.kind == "E" else "base_ready"
        record = base_record(
            record_type, session, signal.symbol,
            "FIRST_CROSSING" if signal.kind == "E" else "FIRST_TRUE",
            cutoff.isoformat())
        record.update({
            "bar_start_ts": start.isoformat(),
            "bar_end_ts": end.isoformat(),
            "received_at": recovered.isoformat(),
            "decision_available_ts": available.isoformat(),
            "source_timeframe": (
                "native_5Min" if signal.kind == "E" else "completed_1Min"),
            "leader_generation": leader_generation,
            "worker_instance_id": worker_instance_id,
            "source": "alpaca_rest_sip_recovery",
        })
        if signal.kind == "E":
            if not _finite(signal.score) or not _finite(signal.observed_weight):
                raise RecoveredEBRecordUnsafe("RECOVERED_E_METRICS_INVALID")
            record.update({
                "score": float(signal.score),
                "threshold": ARTIFACT["threshold"],
                "observed_weight": float(signal.observed_weight),
            })
        else:
            if not isinstance(signal.features, dict) or not isinstance(
                    signal.diagnostics, dict):
                raise RecoveredEBRecordUnsafe("RECOVERED_B_METRICS_MISSING")
            try:
                features = {
                    **{k: signal.features[k] for k in _B_FEATURES},
                    **{k: signal.diagnostics[k] for k in _B_DIAGNOSTICS},
                }
            except KeyError as exc:
                raise RecoveredEBRecordUnsafe(
                    "RECOVERED_B_METRICS_MISSING") from exc
            if any(not _finite(value) for value in features.values()):
                raise RecoveredEBRecordUnsafe("RECOVERED_B_METRICS_INVALID")
            record["features"] = features
        validate_record(record, record_type)
        records.append(record)
    records.sort(key=lambda r: (
        r["bar_end_ts"], r["bar_start_ts"], r["symbol"], r["record_type"]))
    keys = [record_key(record) for record in records]
    if len(keys) != len(set(keys)):
        raise RecoveredEBRecordUnsafe("RECOVERED_EB_DUPLICATE_KEY")
    batches = tuple(
        tuple(records[offset:offset + max_records_per_batch])
        for offset in range(0, len(records), max_records_per_batch))
    return batches, {
        "schema": "OPR_RECOVERED_EB_RECORD_BATCHES_V1",
        "session": session,
        "scope_symbol_count": len(scope_symbols),
        "record_count": len(records),
        "batch_count": len(batches),
        "max_records_per_batch": max_records_per_batch,
        "records_sha256": recovered_records_sha256(
            records, session=session, scope_symbols=scope_symbols),
        "canonical_record_types": ["early_core", "base_ready"],
        "opportunities_created": 0,
        "trades_created": 0,
        "outboxes_created": 0,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
