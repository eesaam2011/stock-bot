"""Step3M: bounded read-only native 1m/5m session slot diagnostics.

An absent bar may mean no trades, a halt, or missing data. No amount of
slot counting proves full-session coverage without independent evidence.
"""
from datetime import timedelta
from hashlib import sha256
import json
from recovery_chronology import _utc,ChronologyUnsafe

class SlotCoverageUnsafe(ChronologyUnsafe):pass

def audit_native_session_slots(events,symbols,*,session_start,rest_cutoff,
                               max_symbols=80,max_minutes=1440,
                               max_sample=8):
    syms=tuple(symbols)
    if (not syms or len(syms)>max_symbols or len(set(syms))!=len(syms)
        or any(not isinstance(s,str) or not s for s in syms)
        or not isinstance(max_sample,int) or isinstance(max_sample,bool)
        or not 0<=max_sample<=32):
        raise SlotCoverageUnsafe("SLOT_AUDIT_INVALID_BATCH")
    start,end=map(_utc,(session_start,rest_cutoff))
    if (start.second or start.microsecond or start.minute%5
        or end<=start or end-start>timedelta(minutes=max_minutes,seconds=59)):
        raise SlotCoverageUnsafe("SLOT_AUDIT_INVALID_WINDOW")
    complete_1m=int((end-start).total_seconds()//60)
    complete_5m=complete_1m//5
    if not complete_1m or complete_1m>max_minutes:
        raise SlotCoverageUnsafe("SLOT_AUDIT_INVALID_WINDOW")
    observed={(s,tf):set() for s in syms for tf in ("1m","5m")}
    for event in events:
        if (event.symbol not in syms or event.timeframe not in ("1m","5m")
            or event.end-event.start!=timedelta(minutes=1 if event.timeframe=="1m" else 5)):
            raise SlotCoverageUnsafe("SLOT_AUDIT_UNEXPECTED_EVENT")
        if event.start<start:continue  # earlier REST warmup, not session slots
        if event.end>end:
            raise SlotCoverageUnsafe("SLOT_AUDIT_EVENT_AFTER_REST_CUTOFF")
        delta=int((event.start-start).total_seconds())
        width=60 if event.timeframe=="1m" else 300
        if delta<0 or delta%width or (event.start-start).total_seconds()!=delta:
            raise SlotCoverageUnsafe("SLOT_AUDIT_MISALIGNED_BAR")
        index=delta//width
        if index >= (complete_1m if width==60 else complete_5m):
            raise SlotCoverageUnsafe("SLOT_AUDIT_OUT_OF_GRID")
        slot=observed[(event.symbol,event.timeframe)]
        if index in slot:raise SlotCoverageUnsafe("SLOT_AUDIT_DUPLICATE_NATIVE_EVENT")
        slot.add(index)
    sample=[];anomalies=0;unknown_both=0
    for symbol in syms:
        m1=observed[(symbol,"1m")]
        m5=observed[(symbol,"5m")]
        for index in range(complete_5m):
            count=sum(i in m1 for i in range(index*5,index*5+5))
            has5=index in m5
            if (count>0)!=has5:
                anomalies+=1
                if len(sample)<max_sample:
                    sample.append({"symbol":symbol,
                        "slot_start":(start+timedelta(minutes=index*5)).isoformat(),
                        "native_1m_bars":count,"native_5m_bar":has5,
                        "classification":"CROSS_TIMEFRAME_OBSERVATION_MISMATCH"})
            elif count==0:
                unknown_both+=1
                if len(sample)<max_sample:
                    sample.append({"symbol":symbol,
                        "slot_start":(start+timedelta(minutes=index*5)).isoformat(),
                        "classification":"BOTH_ABSENT_CAUSE_UNKNOWN"})
    observed_1m=sum(len(observed[(s,"1m")]) for s in syms)
    observed_5m=sum(len(observed[(s,"5m")]) for s in syms)
    digest=sha256()
    for symbol in sorted(syms):
        for tf in ("1m","5m"):
            digest.update(json.dumps([symbol,tf,sorted(observed[(symbol,tf)])],
                                     separators=(",",":")).encode()+b"\n")
    return {
        "symbols":len(syms),"session_start":start.isoformat(),
        "rest_cutoff":end.isoformat(),
        "complete_1m_slots_per_symbol":complete_1m,
        "complete_5m_slots_per_symbol":complete_5m,
        "observed_native_1m_slots":observed_1m,
        "observed_native_5m_slots":observed_5m,
        "unobserved_1m_slots_unknown_cause":len(syms)*complete_1m-observed_1m,
        "unobserved_5m_slots_unknown_cause":len(syms)*complete_5m-observed_5m,
        "cross_timeframe_observation_mismatches":anomalies,
        "both_timeframes_absent_5m_windows_unknown_cause":unknown_both,
        "diagnostic_sample":sample,
        "slot_observation_sha256":digest.hexdigest(),
        "rest_pagination_exhausted_is_not_coverage_proof":True,
        "halt_coverage_proven":False,
        "independent_no_trade_evidence_provided":False,
        "full_session_coverage_proven":False,
        "canonical_backfill_authorized":False,
        "direct_handoff_authorized":False,
        "canonical_writes":0
    }
