"""Step 3G: full requested-session REST/SIP E/B preview, read-only.

Requires one externally DRAINING ACK epoch and its entire unacknowledged
capture, with all symbols in one bounded REST batch. This is NOT the live
drain/ACK coordinator or proof of feed completeness.
"""
from datetime import timedelta
from recovery_chronology import NativeEvent,_utc
from recovery_sip_overlap import audit_draining_capture,SIPOverlapUnsafe
from recovery_session_replay import reconstruct_session_signals

def preview_session_overlap(native_events,capture,*,session,epoch,symbols,
                            session_start,session_end,requested_start,
                            as_of,max_events=100000,base_factory=None,
                            early_score=None):
    if (not isinstance(symbols,(list,tuple)) or not symbols
        or len(symbols)>80 or len(set(symbols))!=len(symbols)
        or any(not isinstance(s,str) or not s for s in symbols)
        or not isinstance(epoch,int) or epoch<1
        or not isinstance(max_events,int) or max_events<1
        or not native_events or len(native_events)>max_events):
        raise SIPOverlapUnsafe("SESSION_PREVIEW_INVALID_BATCH_OR_LIMIT")
    now=_utc(as_of)
    overlap,audit=audit_draining_capture(
        native_events,capture,epoch=epoch,as_of=now,max_events=max_events)
    prefix=audit["audited_prefix"]
    if prefix["captured_prefix_count"]!=prefix["queue_size_at_snapshot"]:
        raise SIPOverlapUnsafe("SESSION_PREVIEW_TRUNCATED_CAPTURE")
    if (prefix["acked_upto_at_snapshot"]!=0
        or (prefix["first_sequence"] is not None
            and prefix["first_sequence"]!=1)):
        raise SIPOverlapUnsafe("SESSION_PREVIEW_PRIOR_ACK")
    allowed=set(symbols)
    if any(e.symbol not in allowed for e in overlap):
        raise SIPOverlapUnsafe("SESSION_PREVIEW_OUT_OF_BATCH_SYMBOL")
    bars=[]
    for item in overlap:
        if item.kind not in ("BAR_1M","BAR_NATIVE_5M"):
            continue
        tf="1m" if item.kind=="BAR_1M" else "5m"
        delta=timedelta(minutes=1 if tf=="1m" else 5)
        bars.append(NativeEvent(item.symbol,tf,item.event_time-delta,
                                item.event_time,dict(item.payload),now))
    kwargs={}
    if base_factory is not None:kwargs["base_factory"]=base_factory
    if early_score is not None:kwargs["early_score"]=early_score
    signals,session_audit=reconstruct_session_signals(
        tuple(bars),session,session_start=session_start,
        session_end=session_end,requested_start=requested_start,
        recovered_at=now,max_events=max_events,**kwargs)
    # A live producer can append at any point; stable snapshot only means
    # this *preview* was internally coherent. It cannot authorize DIRECT.
    final=capture.snapshot()
    if (final["epoch"]!=epoch or final["phase"]!=capture.DRAINING
        or final["buffered"]!=prefix["queue_size_at_snapshot"]
        or final["last_sequence"]!=prefix["last_sequence_at_snapshot"]
        or final["acked_upto"]!=prefix["acked_upto_at_snapshot"]):
        raise SIPOverlapUnsafe("SESSION_PREVIEW_CAPTURE_CHANGED")
    return signals,{
        "kind":"READ_ONLY_SESSION_REST_SIP_EB_PREVIEW",
        "epoch":epoch,"symbols":len(symbols),
        "overlap":audit,"session_signals":session_audit,
        "session_observed_E":session_audit["session_observed_E"],
        "session_observed_B":session_audit["session_observed_B"],
        "sip_trades_observed_not_reconciled":audit["sip_trades"],
        "sip_statuses_observed_not_reconciled":audit["sip_statuses"],
        "first_of_session_proven":False,
        "full_session_coverage_proven":False,
        "sip_continuity_proven":False,"halt_coverage_proven":False,
        "active_trades_reconciled":False,"capture_acknowledged":False,
        "canonical_writes":0,"direct_handoff_authorized":False,
        "retroactive_entries_allowed":False}
