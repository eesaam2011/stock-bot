"""Step 2Z: one-batch end-to-end REST+SIP+frozen E/B preview.

A bounded *read-only* bridge between validated REST chronology, immutable
captured SIP overlap and the actual frozen E/B engines. This deliberately
does not write canonical state, acknowledge SIP, prove coverage or trust,
reconcile trades, or create retroactive opportunities.
"""
from datetime import timedelta
from recovery_chronology import NativeEvent,_utc
from recovery_signal_replay import reconstruct_window_signals
from recovery_sip_overlap import audit_draining_capture,SIPOverlapUnsafe

def preview_recovery_batch(native_events,capture,*,session,epoch,as_of,
                           max_events=100000,base_factory=None,
                           early_engine_factory=None):
    now=_utc(as_of)
    if not native_events or not isinstance(session,str) or not session:
        raise SIPOverlapUnsafe("PREVIEW_REQUIRES_NATIVE_HISTORY_AND_SESSION")
    if max_events<1 or len(native_events)>max_events:
        raise SIPOverlapUnsafe("PREVIEW_EVENT_LIMIT")
    overlap,audit=audit_draining_capture(
        native_events,capture,epoch=epoch,as_of=now,max_events=max_events)
    prefix=audit["audited_prefix"]
    if prefix["captured_prefix_count"]!=prefix["queue_size_at_snapshot"]:
        raise SIPOverlapUnsafe("PREVIEW_TRUNCATED_CAPTURE_PREFIX")
    bars=[]
    for item in overlap:
        if item.kind not in {"BAR_1M","BAR_NATIVE_5M"}:
            continue
        tf="1m" if item.kind=="BAR_1M" else "5m"
        delta=timedelta(minutes=1 if tf=="1m" else 5)
        bars.append(NativeEvent(item.symbol,tf,item.event_time-delta,
                                item.event_time,dict(item.payload),now))
    kwargs={}
    if base_factory is not None:kwargs["base_factory"]=base_factory
    if early_engine_factory is not None:
        kwargs["early_engine_factory"]=early_engine_factory
    signals,signal_audit=reconstruct_window_signals(
        tuple(bars),session,recovered_at=now,max_events=max_events,**kwargs)
    # Even if the queue remained stable through this call, the producer can
    # append immediately afterward. Never treat this as a handoff barrier.
    final=capture.snapshot()
    if (final["epoch"]!=epoch or final["phase"]!=capture.DRAINING
        or final["buffered"]!=prefix["queue_size_at_snapshot"]
        or final["last_sequence"]!=prefix["last_sequence_at_snapshot"]):
        raise SIPOverlapUnsafe("CAPTURE_CHANGED_DURING_PREVIEW")
    return signals,{
        "kind":"READ_ONLY_NATIVE_REST_SIP_EB_PREVIEW",
        "epoch":epoch,"overlap":audit,"signals":signal_audit,
        "window_local_E":signal_audit["window_local_E"],
        "window_local_B":signal_audit["window_local_B"],
        "first_of_session_proven":False,
        "capture_acknowledged":False,"canonical_writes":0,
        "active_trades_reconciled":False,"halt_coverage_proven":False,
        "sip_continuity_proven":False,"direct_handoff_authorized":False,
        "retroactive_entries_allowed":False}
