"""Step 3H: read-only comparison of observed session E/B with canonical Redis.

This does NOT infer missing feed coverage, authorize backfill, overwrite
existing records, grant SIP trust, or create retroactive opportunities.
"""
import hashlib,json,math
from datetime import timedelta
from recovery_chronology import ChronologyUnsafe,_utc
from recovery_signal_replay import RecoveredSignal
from early_core_score import ARTIFACT
from state_store import validate_record,SchemaError,canonical_json

class CanonicalAuditUnsafe(ChronologyUnsafe):pass

B_FEATURE_KEYS=("opportunity","failure_pressure")
B_DIAGNOSTIC_KEYS=("price","vwap","demand_efficiency",
                   "price_acceptance","volume_acceleration")

def _finite_equal(a,b):
    if isinstance(a,bool) or isinstance(b,bool):return False
    try:
        x,y=float(a),float(b)
    except (TypeError,ValueError,OverflowError):return False
    return math.isfinite(x) and math.isfinite(y) and math.isclose(
        x,y,rel_tol=1e-9,abs_tol=1e-10)

def _validate_canonical(rec,typ,session,symbol,now):
    if rec is None:return None
    if not isinstance(rec,dict):
        raise CanonicalAuditUnsafe("CANONICAL_NOT_A_RECORD")
    try:
        validate_record(rec,typ)
        if (rec["session"]!=session or rec["symbol"]!=symbol
            or not isinstance(rec["worker_instance_id"],str)
            or not rec["worker_instance_id"]
            or isinstance(rec["leader_generation"],bool)
            or not isinstance(rec["leader_generation"],int)
            or rec["leader_generation"]<1):
            raise CanonicalAuditUnsafe("CANONICAL_IDENTITY_OR_LEADERSHIP_INVALID")
        for field in ("bar_start_ts","bar_end_ts","received_at",
                      "decision_available_ts","created_at","updated_at"):
            if not isinstance(rec[field],str):
                raise CanonicalAuditUnsafe("CANONICAL_TIME_INVALID")
        start,end,received,decision,created,updated=map(
            _utc,(rec["bar_start_ts"],rec["bar_end_ts"],
                  rec["received_at"],rec["decision_available_ts"],
                  rec["created_at"],rec["updated_at"]))
        width=timedelta(minutes=5 if typ=="early_core" else 1)
        if (end!=start+width or end>decision or received>decision
            or decision>now or created>now or updated>now
            or updated<created):
            raise CanonicalAuditUnsafe("CANONICAL_TIME_INVALID")
        if typ=="early_core":
            if (not _finite_equal(rec["threshold"],ARTIFACT["threshold"])
                or not _finite_equal(rec["score"],rec["score"])
                or not _finite_equal(rec["observed_weight"],rec["observed_weight"])):
                raise CanonicalAuditUnsafe("CANONICAL_E_METRICS_INVALID")
        else:
            if not isinstance(rec["features"],dict) or any(
                not _finite_equal(rec["features"].get(k),rec["features"].get(k))
                for k in B_FEATURE_KEYS+B_DIAGNOSTIC_KEYS):
                raise CanonicalAuditUnsafe("CANONICAL_B_FEATURES_INVALID")
    except (SchemaError,ChronologyUnsafe,KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,CanonicalAuditUnsafe):raise
        raise CanonicalAuditUnsafe("CANONICAL_SCHEMA_OR_TIME_INVALID") from exc
    return rec

def _match(signal,rec):
    if (_utc(rec["bar_start_ts"])!=_utc(signal.bar_start_ts)
        or _utc(rec["bar_end_ts"])!=_utc(signal.bar_end_ts)):
        return False,"BAR_TIME_DIVERGENCE"
    if signal.kind=="E":
        if (not _finite_equal(rec["score"],signal.score)
            or not _finite_equal(rec["observed_weight"],signal.observed_weight)):
            return False,"E_FROZEN_METRICS_DIVERGENCE"
    else:
        if (not isinstance(signal.features,dict)
            or not isinstance(signal.diagnostics,dict)):
            raise CanonicalAuditUnsafe("OBSERVED_B_FEATURES_MISSING")
        for k in B_FEATURE_KEYS:
            if not _finite_equal(rec["features"][k],signal.features.get(k)):
                return False,"B_FEATURE_DIVERGENCE"
        for k in B_DIAGNOSTIC_KEYS:
            if not _finite_equal(rec["features"][k],signal.diagnostics.get(k)):
                return False,"B_DIAGNOSTIC_DIVERGENCE"
    return True,None

def audit_session_canonical(signals,reader,*,session,symbols,as_of,
                            max_symbols=80,require_atomic_snapshot=False):
    """Read every E/B key for one bounded batch before comparing signals.

    Existing canonical without observed signal is INDETERMINATE: REST/SIP
    history might be incomplete. Observed without canonical is UNCOMMITTED,
    not permission to write it or create a historical entry.
    """
    if (not isinstance(session,str) or not session
        or not isinstance(symbols,(list,tuple)) or not symbols
        or len(symbols)>max_symbols or len(set(symbols))!=len(symbols)
        or any(not isinstance(s,str) or not s for s in symbols)
        or (not require_atomic_snapshot and
            (not hasattr(reader,"get_e") or not hasattr(reader,"get_b")))
        or (require_atomic_snapshot and
            not hasattr(reader,"get_eb_snapshot"))
        or len(signals)>len(symbols)*2):
        raise CanonicalAuditUnsafe("CANONICAL_AUDIT_INVALID_BATCH")
    now=_utc(as_of);allowed=set(symbols);observed={}
    for signal in signals:
        if (not isinstance(signal,RecoveredSignal)
            or signal.session!=session or signal.symbol not in allowed
            or signal.kind not in ("E","B")
            or (signal.symbol,signal.kind) in observed):
            raise CanonicalAuditUnsafe("CANONICAL_AUDIT_INVALID_SIGNAL")
        if (_utc(signal.bar_end_ts)>now
            or _utc(signal.bar_start_ts)>=_utc(signal.bar_end_ts)
            or _utc(signal.decision_available_ts)!=now):
            raise CanonicalAuditUnsafe("CANONICAL_AUDIT_SIGNAL_TIME_INVALID")
        observed[(signal.symbol,signal.kind)]=signal
    matched=[];uncommitted=[];unobserved=[];divergent=[]
    # A single Redis MGET is atomic with respect to other Redis commands,
    # unlike two serial GETs. This is ONLY an E/B point-in-time snapshot:
    # it cannot prove historical first-of-session or feed completeness.
    snapshot=None
    if require_atomic_snapshot:
        try:snapshot=reader.get_eb_snapshot(session,sorted(allowed))
        except Exception as exc:
            raise CanonicalAuditUnsafe("CANONICAL_SNAPSHOT_READ_FAILED") from exc
        if (not isinstance(snapshot,dict)
            or set(snapshot)!={(sym,kind) for sym in allowed for kind in ("E","B")}):
            raise CanonicalAuditUnsafe("CANONICAL_SNAPSHOT_INCOMPLETE")
    # Legacy serial GET remains available for older test adapters only.
    # Neither read path grants write, trust or backfill permission.
    for sym in sorted(allowed):
        for kind,getter,typ in (("E",reader.get_e,"early_core"),
                                ("B",reader.get_b,"base_ready")):
            if require_atomic_snapshot:
                rec=snapshot[(sym,kind)]
            else:
                try:rec=getter(session,sym)
                except Exception as exc:
                    raise CanonicalAuditUnsafe("CANONICAL_READ_FAILED") from exc
            rec=_validate_canonical(rec,typ,session,sym,now)
            signal=observed.get((sym,kind))
            label=f"{sym}:{kind}"
            if rec is None and signal is not None:uncommitted.append(label)
            elif rec is not None and signal is None:unobserved.append(label)
            elif rec is not None and signal is not None:
                ok,reason=_match(signal,rec)
                if ok:matched.append(label)
                else:divergent.append({"signal":label,"reason":reason})
    audit={"kind":"READ_ONLY_CANONICAL_SESSION_COMPARISON",
           "session":session,"symbols":len(symbols),
           "matched":matched,"observed_without_canonical":uncommitted,
           "canonical_without_observed":unobserved,"divergent":divergent,
           "read_only":True,"canonical_writes":0,
           "multi_key_snapshot_atomic":bool(require_atomic_snapshot),
           "snapshot_scope":"EB_KEYS_ONLY" if require_atomic_snapshot else "SERIAL_GET",
           "canonical_backfill_authorized":False,
           "retroactive_entries_allowed":False,
           "full_session_coverage_proven":False,
           "first_of_session_proven":False,
           "sip_continuity_proven":False,"direct_handoff_authorized":False}
    # Stable audit digest for comparison between offline test runs only.
    audit["audit_sha256"]=hashlib.sha256(
        canonical_json({k:v for k,v in audit.items() if k!="audit_sha256"}).encode()).hexdigest()
    return audit
