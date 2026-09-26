from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol
from constants import CANONICAL_SCHEMA_VERSION, REDIS_NAMESPACE

class StateStoreError(RuntimeError): pass
class SchemaError(StateStoreError): pass
class CanonicalConflict(StateStoreError): pass

class BackendProtocol(Protocol):
    def get(self, key: str) -> Optional[str]: ...
    def set_if_absent(self, key: str, value: str) -> bool: ...
    def compare_and_set(self, key: str, expected: Optional[str], value: str) -> bool: ...

class InMemoryRedis:
    """Contract-test adapter only; not production Redis."""
    def __init__(self): self._data: Dict[str, str] = {}
    def get(self, key): return self._data.get(key)
    def set_if_absent(self, key, value):
        if key in self._data: return False
        self._data[key] = value; return True
    def compare_and_set(self, key, expected, value):
        if self._data.get(key) != expected: return False
        self._data[key] = value; return True

def canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _required(record, names):
    missing = [n for n in names if n not in record]
    if missing: raise SchemaError("missing required fields: " + ",".join(missing))

COMMON_REQUIRED = ("schema_version","record_type","session","symbol","state","created_at","updated_at")
TYPE_REQUIRED = {
    "early_core": ("score","threshold","bar_start_ts","bar_end_ts","received_at","decision_available_ts","source_timeframe","observed_weight","leader_generation","worker_instance_id"),
    "base_ready": ("features","bar_start_ts","bar_end_ts","received_at","decision_available_ts","source_timeframe","leader_generation","worker_instance_id"),
    "opportunity": ("e_id","b_id","delta_seconds","first_event","entry_trigger_ts","expiry","terminal_reason","leader_generation","worker_instance_id"),
    "trade": ("trade_id","entry_alert_price","structure_low","structural_stop","risk_pct","t1","t2","monitoring_deadline","leader_generation","worker_instance_id"),
    "outbox": ("event_id","event_type","payload","attempt_count","leader_generation"),
}

def validate_record(record, expected_type=None):
    _required(record, COMMON_REQUIRED)
    if record["schema_version"] != CANONICAL_SCHEMA_VERSION:
        raise SchemaError("unsupported schema_version")
    typ = record["record_type"]
    if expected_type is not None and typ != expected_type: raise SchemaError("record_type mismatch")
    if typ not in TYPE_REQUIRED: raise SchemaError("unknown record_type")
    _required(record, TYPE_REQUIRED[typ])
    if typ == "early_core" and record["source_timeframe"] != "native_5Min":
        raise SchemaError("early_core source_timeframe must be native_5Min")
    if typ == "base_ready" and record["source_timeframe"] != "completed_1Min":
        raise SchemaError("base_ready source_timeframe must be completed_1Min")

def key_early_core(session,symbol): return f"{REDIS_NAMESPACE}:early_core:{session}:{symbol}"
def key_base_ready(session,symbol): return f"{REDIS_NAMESPACE}:base_ready:{session}:{symbol}"
def key_opportunity(session,symbol): return f"{REDIS_NAMESPACE}:opportunity:{session}:{symbol}"
def key_trade(trade_id): return f"{REDIS_NAMESPACE}:trade:{trade_id}"
def key_outbox(event_id): return f"{REDIS_NAMESPACE}:outbox:{event_id}"

def record_key(r):
    typ = r["record_type"]
    if typ == "early_core": return key_early_core(r["session"],r["symbol"])
    if typ == "base_ready": return key_base_ready(r["session"],r["symbol"])
    if typ == "opportunity": return key_opportunity(r["session"],r["symbol"])
    if typ == "trade": return key_trade(r["trade_id"])
    if typ == "outbox": return key_outbox(r["event_id"])
    raise SchemaError("unknown record_type")

@dataclass
class CanonicalStateStore:
    backend: BackendProtocol
    def create(self, record):
        validate_record(record)
        key = record_key(record); raw = canonical_json(record)
        # Production Redis must atomically check owner + generation with SET NX.
        # Contract-test backends retain their in-memory implementation.
        if hasattr(self.backend, "set_if_absent_fenced"):
            inserted = self.backend.set_if_absent_fenced(
                key, raw, record["worker_instance_id"], record["leader_generation"])
        else:
            inserted = self.backend.set_if_absent(key, raw)
        if not inserted:
            if self.backend.get(key) == raw: return key
            raise CanonicalConflict("canonical record already exists")
        return key
    def read(self, key, expected_type=None):
        raw = self.backend.get(key)
        if raw is None: raise KeyError(key)
        try: record = json.loads(raw)
        except json.JSONDecodeError as exc: raise SchemaError("invalid canonical JSON") from exc
        validate_record(record, expected_type)
        return record
    def compare_and_replace(self,key,expected_record,new_record,expected_type=None):
        validate_record(expected_record,expected_type); validate_record(new_record,expected_type)
        if expected_record["record_type"] != new_record["record_type"]: raise SchemaError("record_type cannot change")
        if not self.backend.compare_and_set(key,canonical_json(expected_record),canonical_json(new_record)):
            raise CanonicalConflict("compare-and-replace conflict")

def base_record(record_type,session,symbol,state,now):
    return {"schema_version":CANONICAL_SCHEMA_VERSION,"record_type":record_type,"session":session,"symbol":symbol,
            "state":state,"created_at":now,"updated_at":now}
