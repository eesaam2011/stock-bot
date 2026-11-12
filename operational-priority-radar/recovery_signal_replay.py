"""Step 2X: deterministic, read-only first E/B reconstruction from native REST.

Never writes canonical Redis, creates opportunities, sends alerts or supplies
continuity proof. A window-local first signal is NOT necessarily the session's
first signal. All recovered signals are available only at recovery time; they
must never authorize retroactive entries.
"""
from dataclasses import dataclass
from datetime import datetime,timezone
from collections import defaultdict
from recovery_chronology import NativeEvent,ChronologyUnsafe,_utc
from production_adapters import OperationalBaseReady
from early_core_engine import FrozenEarlyCoreEngine
from early_core_score import ARTIFACT

@dataclass(frozen=True)
class RecoveredSignal:
    kind:str
    symbol:str
    session:str
    bar_start_ts:datetime
    bar_end_ts:datetime
    recovered_at:datetime
    decision_available_ts:datetime
    score:float|None=None
    observed_weight:float|None=None
    features:dict|None=None
    diagnostics:dict|None=None

def reconstruct_window_signals(events,session,*,recovered_at,
                               max_events=100000,base_factory=OperationalBaseReady,
                               early_engine_factory=FrozenEarlyCoreEngine):
    """Evaluate frozen native E/B engines in completed-bar chronological order.

    Native5 is evaluated with the full *bounded native5 history* via the frozen
    engine's first_crossing; the engine itself enforces cutoff at each bar.
    Base Ready consumes native1 one completed bar at a time, never future bars.
    Any missing/invalid provenance, mixed timestamps or conflicting duplicates
    fails closed rather than being silently ignored.
    """
    now=_utc(recovered_at)
    if not isinstance(session,str) or not session or len(events)>max_events:
        raise ChronologyUnsafe("REPLAY_INVALID_INPUT_OR_LIMIT")
    grouped=defaultdict(lambda:{"1m":[],"5m":[]})
    seen={}
    for e in events:
        if not isinstance(e,NativeEvent):
            raise ChronologyUnsafe("REPLAY_REQUIRES_VALIDATED_NATIVE_EVENTS")
        if e.timeframe not in ("1m","5m") or e.recovered_at!=now:
            raise ChronologyUnsafe("REPLAY_EPOCH_OR_TIMEFRAME_MISMATCH")
        if e.end>now or e.start>=e.end:
            raise ChronologyUnsafe("REPLAY_INCOMPLETE_BAR")
        if e.timeframe=="5m" and e.bar.get("_timeframe")!="native_5Min":
            raise ChronologyUnsafe("REPLAY_SYNTHETIC_5M_FORBIDDEN")
        key=(e.symbol,e.timeframe,e.start)
        if key in seen:
            raise ChronologyUnsafe("REPLAY_DUPLICATE_EVENT")
        seen[key]=True
        grouped[e.symbol][e.timeframe].append(e)
    result=[]
    for symbol in sorted(grouped):
        lanes=grouped[symbol]
        bars1=sorted(lanes["1m"],key=lambda e:e.end)
        bars5=sorted(lanes["5m"],key=lambda e:e.end)
        base=base_factory()
        for e in bars1:
            candidate=base.on_completed_native_1m(symbol,e.bar,now)
            if candidate.get("base_ready"):
                result.append(RecoveredSignal(
                    "B",symbol,session,e.start,e.end,now,max(now,e.end),
                    features=candidate["features"],diagnostics=candidate["diagnostics"]))
                break
        if bars5:
            # The engine uses frozen threshold and native 5m bars. Its
            # first_crossing evaluates each historical completed bar cutoff.
            rows=[e.bar for e in bars5]
            class NativeProvider:
                def fetch_native_5min(self,*args):
                    return rows
            crossing=early_engine_factory(NativeProvider()).first_crossing(
                symbol,session,bars5[0].start,bars5[-1].end,now)
            if crossing:
                if crossing.bar_end_ts>now:
                    raise ChronologyUnsafe("REPLAY_E_FUTURE_CROSSING")
                result.append(RecoveredSignal(
                    "E",symbol,session,crossing.bar_start_ts,
                    crossing.bar_end_ts,now,max(now,crossing.bar_end_ts),
                    score=crossing.score,observed_weight=crossing.observed_weight))
    result.sort(key=lambda x:(x.bar_end_ts,x.bar_start_ts,x.symbol,x.kind))
    return tuple(result),{
        "window_local_E":sum(s.kind=="E" for s in result),
        "window_local_B":sum(s.kind=="B" for s in result),
        "native_events_consumed":len(events),
        "frozen_early_core_threshold":ARTIFACT["threshold"],
        "first_of_session_proven":False,
        "canonical_writes":0,
        "entry_opportunities_created":0,
        "retroactive_entries_allowed":False,
        "sip_merged":False,
        "continuity_proven":False}
