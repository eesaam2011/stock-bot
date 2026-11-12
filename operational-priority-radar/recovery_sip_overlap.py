"""Step 2Y: non-destructive REST/SIP overlap audit for one ACK epoch.

This is NOT a replay coordinator or continuity proof. REST and SIP native1
bars with identical symbol/start must agree on OHLCV; conflicts fail closed.
Unmatched SIP bars, trade and halt/status events are preserved in event-time
order. Captured receive time and historical market time remain distinct.
"""
from dataclasses import dataclass
from datetime import timedelta
from recovery_chronology import ChronologyUnsafe,NativeEvent,_utc,_normalize
from sip_epoch_capture import CapturedSIP

class SIPOverlapUnsafe(ChronologyUnsafe):pass

@dataclass(frozen=True)
class OverlapEvent:
    kind:str
    symbol:str
    event_time:object
    available_at:object
    source:str
    sequence:int
    payload:dict

def merge_native_and_captured(native_events,captured,*,epoch,as_of,max_events=100000):
    """Return chronological audit events; do not ACK capture or mutate Redis.

    A SIP bar is an already completed native 1m bar with t=start. Native 5m
    only comes from verified REST native_5Min. Event time for bars is close
    time, not start time. A duplicate REST/SIP bar is represented once.
    """
    now=_utc(as_of)
    if not isinstance(epoch,int) or epoch<1 or max_events<1:
        raise SIPOverlapUnsafe("INVALID_MERGE_EPOCH_OR_LIMIT")
    if len(native_events)+len(captured)>max_events:
        raise SIPOverlapUnsafe("MERGE_BOUNDED_LIMIT")
    seen_rest={};seen_sip={};result=[]
    for ev in native_events:
        if not isinstance(ev,NativeEvent) or ev.timeframe not in {"1m","5m"}:
            raise SIPOverlapUnsafe("UNVALIDATED_NATIVE_EVENT")
        if ev.end>now or ev.recovered_at>now:
            raise SIPOverlapUnsafe("NATIVE_EVENT_AFTER_AUDIT")
        if ev.timeframe=="5m" and ev.bar.get("_timeframe")!="native_5Min":
            raise SIPOverlapUnsafe("UNPROVEN_NATIVE5")
        normalized=_normalize(ev.bar,ev.timeframe)
        key=(ev.symbol,ev.timeframe,ev.start)
        if key in seen_rest:
            raise SIPOverlapUnsafe("DUPLICATE_NATIVE_EVENT")
        seen_rest[key]=normalized
        result.append(OverlapEvent(
            "BAR_1M" if ev.timeframe=="1m" else "BAR_NATIVE_5M",
            ev.symbol,ev.end,max(ev.end,ev.recovered_at),
            "REST",0,dict(ev.bar)))
    previous_seq=None;overlap=0;new_bars=0;trade_count=0;status_count=0
    for item in captured:
        if not isinstance(item,CapturedSIP) or item.epoch!=epoch:
            raise SIPOverlapUnsafe("CAPTURE_EPOCH_MISMATCH")
        if (not isinstance(item.sequence,int) or item.sequence<1
            or (previous_seq is not None and item.sequence!=previous_seq+1)):
            raise SIPOverlapUnsafe("CAPTURE_SEQUENCE_GAP")
        previous_seq=item.sequence
        event_ts=_utc(item.event_ts)
        if not item.received_at:
            raise SIPOverlapUnsafe("CAPTURE_RECEIVE_TIME_MISSING")
        received_at=_utc(item.received_at)
        if received_at>now or event_ts>now:
            raise SIPOverlapUnsafe("CAPTURE_FUTURE_EVENT_OR_RECEIVE")
        msg=item.payload
        if not isinstance(msg,dict) or not msg.get("S"):
            raise SIPOverlapUnsafe("CAPTURE_SYMBOL_MISSING")
        symbol=msg["S"]
        # event_ts is transport metadata, not an independent trusted clock.
        # If a caller mutates payload.t after capture, chronology must reject it.
        try:
            payload_ts=_utc(msg.get("t"))
        except ChronologyUnsafe as exc:
            raise SIPOverlapUnsafe("CAPTURE_PAYLOAD_TIME_INVALID") from exc
        if payload_ts!=event_ts:
            raise SIPOverlapUnsafe("CAPTURE_EVENT_TIME_MISMATCH")
        if item.kind=="BAR":
            if msg.get("T")!="b":raise SIPOverlapUnsafe("CAPTURE_KIND_MISMATCH")
            end=event_ts+timedelta(minutes=1)
            if end>now:raise SIPOverlapUnsafe("INCOMPLETE_CAPTURED_BAR")
            vals=_normalize(msg,"1m")
            key=(symbol,"1m",event_ts)
            if key in seen_sip:
                if seen_sip[key]!=vals:
                    raise SIPOverlapUnsafe("CONFLICTING_SIP_DUPLICATE")
                continue
            seen_sip[key]=vals
            if key in seen_rest:
                if seen_rest[key]!=vals:
                    raise SIPOverlapUnsafe("REST_SIP_BAR_CONFLICT")
                overlap+=1
                continue
            new_bars+=1
            result.append(OverlapEvent(
                "BAR_1M",symbol,end,max(end,received_at),
                "SIP",item.sequence,dict(msg)))
        elif item.kind=="TRADE":
            if msg.get("T")!="t":raise SIPOverlapUnsafe("CAPTURE_KIND_MISMATCH")
            trade_count+=1
            result.append(OverlapEvent(
                "TRADE",symbol,event_ts,max(event_ts,received_at),
                "SIP",item.sequence,dict(msg)))
        elif item.kind=="STATUS":
            if msg.get("T")!="s":raise SIPOverlapUnsafe("CAPTURE_KIND_MISMATCH")
            status_count+=1
            result.append(OverlapEvent(
                "STATUS",symbol,event_ts,max(event_ts,received_at),
                "SIP",item.sequence,dict(msg)))
        else:
            raise SIPOverlapUnsafe("CAPTURE_KIND_UNSUPPORTED")
    # Ties retain SIP sequence; status/trade order must not be invented.
    result.sort(key=lambda x:(x.event_time,0 if x.source=="REST" else 1,
                              x.sequence,x.symbol,x.kind))
    return tuple(result),{
        "epoch":epoch,"native_events":len(native_events),
        "sip_captured":len(captured),"overlap_equal_1m_bars":overlap,
        "unmatched_sip_1m_bars":new_bars,"sip_trades":trade_count,
        "sip_statuses":status_count,"chronological_events":len(result),
        "capture_acknowledged":False,"canonical_writes":0,
        "session_coverage_proven":False,"sip_continuity_proven":False,
        "direct_handoff_authorized":False,"retroactive_entries_allowed":False}

def audit_draining_capture(native_events,capture,*,epoch,as_of,max_events=100000):
    """Inspect a frozen prefix without ACKing it or changing capture phase.

    A live producer may append while we inspect; this function cannot assert
    that the queue is drained or that a zero-loss DIRECT handoff occurred.
    """
    # The live websocket may append while this audit is running on another
    # thread. Read one atomic deep-copied prefix rather than iterating a
    # concurrently mutating deque. The prefix remains unacknowledged.
    try:
        items,prefix=capture.snapshot_prefix(epoch,min(capture.max_messages,max_events))
    except Exception as exc:
        from sip_epoch_capture import EpochCaptureError
        if isinstance(exc,EpochCaptureError):
            raise SIPOverlapUnsafe("CAPTURE_NOT_DRAINING_IN_EPOCH") from exc
        raise
    events,audit=merge_native_and_captured(native_events,items,epoch=epoch,
                                           as_of=as_of,max_events=max_events)
    if not capture.prefix_still_valid(epoch,prefix["first_sequence"],
                                      prefix["last_sequence"]):
        raise SIPOverlapUnsafe("CAPTURE_INVALIDATED_DURING_AUDIT")
    audit["audited_prefix"]=prefix
    audit["live_capture_may_have_appended"]=True
    return events,audit
