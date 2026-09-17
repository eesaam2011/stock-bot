from dataclasses import dataclass
from datetime import datetime,timedelta
from early_core_features import ctr_score_at
from early_core_score import ARTIFACT
class Native5MinUnavailable(RuntimeError): pass
class Synthetic5MinForbidden(RuntimeError): pass
@dataclass(frozen=True)
class EarlyCoreCrossing:
    symbol:str; session:str; bar_start_ts:datetime; bar_end_ts:datetime
    received_at:datetime; decision_available_ts:datetime; score:float; observed_weight:float
    source_timeframe:str="native_5Min"
class FrozenEarlyCoreEngine:
    def __init__(self,provider): self.provider=provider
    def first_crossing(self,symbol,session,start,end,received_at):
        rows=self.provider.fetch_native_5min(symbol,session,start,end)
        if rows is None: raise Native5MinUnavailable("EARLY_CORE_DATA_UNAVAILABLE")
        parsed=[]
        for r in rows:
            if r.get("_timeframe") not in (None,"native_5Min"):
                raise Synthetic5MinForbidden("synthetic 5Min forbidden")
            try: ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00")); c=float(r.get("c"))
            except Exception: continue
            if c>0: parsed.append((ts,c))
        parsed.sort()
        for ts,c in parsed:
            ev=ts+timedelta(minutes=5); sc,den=ctr_score_at(rows,ev)
            if sc is not None and sc>=ARTIFACT["threshold"]:
                return EarlyCoreCrossing(symbol,session,ts,ev,received_at,max(ev,received_at),sc,den)
        return None
