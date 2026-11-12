"""Step 3F: session-scoped, read-only native REST E/B reconstruction.

Unlike a rolling-window preview, explicitly separates pre-session 5Min
feature warm-up from eligible session E crossings. The frozen score and
Base Ready engines are unchanged. A complete REST page chain is not proof
of complete market-data coverage; this module cannot authorize trust.
"""
from collections import defaultdict
from datetime import timedelta
from recovery_chronology import NativeEvent,ChronologyUnsafe,_utc,_normalize
from recovery_signal_replay import RecoveredSignal
from production_adapters import OperationalBaseReady
from early_core_features import ctr_score_at
from early_core_score import ARTIFACT

EARLY_WARMUP_MINUTES=max(int(d["anchor_minutes"]) for d in ARTIFACT["definitions"])+65

def reconstruct_session_signals(events,session,*,session_start,session_end,
                                requested_start,recovered_at,
                                max_events=100000,base_factory=OperationalBaseReady,
                                early_score=ctr_score_at):
    """First observed E and B within explicit session bounds, never canonical.

    Native 5m history before session_start is retained for frozen E features
    but pre-session crossings cannot become session E. B's history begins
    at session_start. No synthetic 5m, extrapolated missing bars, historical
    entry timestamps, Redis writes, SIP ACKs, or trust assertions.
    """
    try:
        start,end,request,now=map(
            _utc,(session_start,session_end,requested_start,recovered_at))
    except ChronologyUnsafe:
        raise
    if (not isinstance(session,str) or not session
        or not request<=start<end<=now
        or not isinstance(max_events,int) or max_events<1
        or len(events)>max_events):
        raise ChronologyUnsafe("SESSION_REPLAY_INVALID_WINDOW_OR_LIMIT")
    lanes=defaultdict(lambda:{"1m":[],"5m":[]})
    seen=set()
    for e in events:
        if not isinstance(e,NativeEvent) or e.timeframe not in ("1m","5m"):
            raise ChronologyUnsafe("SESSION_REPLAY_UNVALIDATED_EVENT")
        if (_utc(e.recovered_at)!=now or _utc(e.start)>=_utc(e.end)
            or e.start<request or e.end>now):
            raise ChronologyUnsafe("SESSION_REPLAY_EVENT_OUTSIDE_FETCH")
        if e.timeframe=="5m" and e.bar.get("_timeframe")!="native_5Min":
            raise ChronologyUnsafe("SESSION_REPLAY_SYNTHETIC_5M")
        # A forged NativeEvent wrapper must not override the actual bar's
        # timestamp or duration; this audit is a prerequisite for replay.
        delta=timedelta(minutes=1 if e.timeframe=="1m" else 5)
        if (_utc(e.bar.get("t"))!=e.start or e.end!=e.start+delta
            or (e.bar.get("S") is not None and e.bar["S"]!=e.symbol)):
            raise ChronologyUnsafe("SESSION_REPLAY_BAR_PROVENANCE_MISMATCH")
        _normalize(e.bar,e.timeframe)
        key=(e.symbol,e.timeframe,e.start)
        if key in seen:raise ChronologyUnsafe("SESSION_REPLAY_DUPLICATE_EVENT")
        seen.add(key)
        lanes[e.symbol][e.timeframe].append(e)
    results=[]
    pre_session_5m=0
    for symbol in sorted(lanes):
        lane=lanes[symbol]
        # B never uses yesterday's/pre-session 1m history.
        base=base_factory()
        for e in sorted(lane["1m"],key=lambda x:x.end):
            if e.start<start or e.end>end:continue
            candidate=base.on_completed_native_1m(symbol,e.bar,now)
            if candidate.get("base_ready"):
                results.append(RecoveredSignal(
                    "B",symbol,session,e.start,e.end,now,max(now,e.end),
                    features=candidate["features"],
                    diagnostics=candidate["diagnostics"]))
                break
        five=sorted(lane["5m"],key=lambda x:x.end)
        pre_session_5m+=sum(e.end<=start for e in five)
        # Frozen ctr_score_at() uses only completed native5 rows at each
        # cutoff. Warm-up bars can inform E but cannot trigger session E.
        history=[]
        for e in five:
            if e.end>end:break
            history.append(e.bar)
            if e.start<start:continue
            score,weight=early_score(history,e.end)
            if score is not None and score>=ARTIFACT["threshold"]:
                results.append(RecoveredSignal(
                    "E",symbol,session,e.start,e.end,now,max(now,e.end),
                    score=score,observed_weight=weight))
                break
    results.sort(key=lambda x:(x.bar_end_ts,x.bar_start_ts,x.symbol,x.kind))
    warmup_requested=request<=start-timedelta(minutes=EARLY_WARMUP_MINUTES)
    return tuple(results),{
        "kind":"READ_ONLY_SESSION_SCOPED_NATIVE_EB",
        "session":session,"session_start":start.isoformat(),
        "session_end":end.isoformat(),
        "requested_start":request.isoformat(),
        "warmup_minutes_required":EARLY_WARMUP_MINUTES,
        "warmup_window_requested":warmup_requested,
        "pre_session_native5_observations":pre_session_5m,
        "native_events_consumed":len(events),
        "session_observed_E":sum(s.kind=="E" for s in results),
        "session_observed_B":sum(s.kind=="B" for s in results),
        "frozen_early_core_threshold":ARTIFACT["threshold"],
        "first_observed_in_requested_session":True,
        "full_session_coverage_proven":False,
        "first_of_session_proven":False,
        "rest_pagination_proven":False,
        "sip_continuity_proven":False,
        "canonical_writes":0,"entry_opportunities_created":0,
        "retroactive_entries_allowed":False,
        "direct_handoff_authorized":False}
