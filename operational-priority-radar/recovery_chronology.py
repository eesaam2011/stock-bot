"""Bounded, read-only native REST chronology audit.

An ordered plan is NOT canonical E/B replay or SIP continuity evidence.
Only native Alpaca 5Min rows may enter the 5m lane. Missing symbol
responses, conflicting duplicates, malformed OHLCV and future bars fail
closed. Market-hours gaps are diagnostics, not proof of a missing feed.
"""
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from hashlib import sha256
import json,math

UTC=timezone.utc
class ChronologyUnsafe(RuntimeError):pass

@dataclass(frozen=True)
class NativeEvent:
    symbol:str
    timeframe:str
    start:datetime
    end:datetime
    bar:dict
    recovered_at:datetime
    @property
    def decision_available_ts(self):
        return max(self.end,self.recovered_at)

def _utc(value):
    if isinstance(value,str):
        try:value=datetime.fromisoformat(value.replace("Z","+00:00"))
        except ValueError as exc:raise ChronologyUnsafe("INVALID_BAR_TIME") from exc
    if not isinstance(value,datetime) or value.tzinfo is None:
        raise ChronologyUnsafe("NAIVE_OR_MISSING_TIME")
    return value.astimezone(UTC)

def _normalize(row,timeframe):
    if not isinstance(row,dict):raise ChronologyUnsafe("INVALID_BAR")
    if timeframe=="5m" and row.get("_timeframe")!="native_5Min":
        raise ChronologyUnsafe("SYNTHETIC_OR_UNPROVEN_NATIVE5")
    try:
        vals={k:float(row[k]) for k in ("o","h","l","c","v")}
    except (KeyError,TypeError,ValueError) as exc:
        raise ChronologyUnsafe("MISSING_OR_INVALID_OHLCV") from exc
    if (not all(math.isfinite(x) for x in vals.values())
        or min(vals[k] for k in ("o","h","l","c"))<=0
        or vals["v"]<0
        or vals["l"]>min(vals["o"],vals["c"])
        or vals["h"]<max(vals["o"],vals["c"])
        or vals["h"]<vals["l"]):
        raise ChronologyUnsafe("INCONSISTENT_OHLCV")
    return vals

def plan_native_batch(rows1,rows5,symbols,*,window_start,window_end,
                      recovered_at,max_events=50000):
    """Return a bounded deterministic plan and diagnostics, never write Redis."""
    syms=tuple(symbols)
    if not syms or len(set(syms))!=len(syms) or max_events<1:
        raise ChronologyUnsafe("INVALID_BATCH")
    if set(rows1)!=set(syms) or set(rows5)!=set(syms):
        raise ChronologyUnsafe("INCOMPLETE_OR_EXTRA_SYMBOL_RESPONSE")
    start,end,now=map(_utc,(window_start,window_end,recovered_at))
    if not start<end<=now:raise ChronologyUnsafe("INVALID_RECOVERY_WINDOW")
    events=[];observed_gaps=0;empty_lanes=0
    for symbol in syms:
        for tf,source,delta in (("1m",rows1,timedelta(minutes=1)),
                                ("5m",rows5,timedelta(minutes=5))):
            rows=source[symbol]
            if not isinstance(rows,list):raise ChronologyUnsafe("INVALID_SYMBOL_ROWS")
            unique={}
            for row in rows:
                if not isinstance(row,dict) or "t" not in row:
                    raise ChronologyUnsafe("MISSING_BAR_START")
                bs=_utc(row["t"]);be=bs+delta
                if be<=start or bs>=end:continue
                if be>now:raise ChronologyUnsafe("INCOMPLETE_OR_FUTURE_BAR")
                normalized=_normalize(row,tf)
                if bs in unique:
                    if unique[bs][0]!=normalized:
                        raise ChronologyUnsafe("CONFLICTING_DUPLICATE_BAR")
                    continue
                unique[bs]=(normalized,dict(row))
                if len(events)+len(unique)>max_events:
                    raise ChronologyUnsafe("REPLAY_PLAN_LIMIT")
            times=sorted(unique)
            if not times:empty_lanes+=1
            observed_gaps+=sum((b-a)>delta for a,b in zip(times,times[1:]))
            for bs in times:
                if len(events)>=max_events:raise ChronologyUnsafe("REPLAY_PLAN_LIMIT")
                events.append(NativeEvent(symbol,tf,bs,bs+delta,
                                          unique[bs][1],now))
    events.sort(key=lambda x:(x.end,x.start,x.symbol,x.timeframe))
    h=sha256()
    for e in events:
        h.update(json.dumps([e.symbol,e.timeframe,e.start.isoformat(),
                             _normalize(e.bar,e.timeframe)],
                            sort_keys=True,separators=(",",":")).encode())
        h.update(b"\n")
    audit={"native_1m":sum(e.timeframe=="1m" for e in events),
           "native_5m":sum(e.timeframe=="5m" for e in events),
           "empty_symbol_timeframe_lanes":empty_lanes,
           "observed_interbar_gaps":observed_gaps,
           "event_count":len(events),"chronological_sha256":h.hexdigest(),
           "plan_built":True,"canonical_replay_completed":False,
           "continuity_proven":False,"retroactive_entries_allowed":False}
    return tuple(events),audit
