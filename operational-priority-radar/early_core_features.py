from __future__ import annotations
from datetime import datetime, timedelta
import math
import numpy as np
from early_core_score import ARTIFACT, score_feature_values

def fd_features(rows, cutoff):
    rr=[]
    for r in rows:
        try:
            ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00"))
            o=float(r.get("o")); h=float(r.get("h")); l=float(r.get("l")); c=float(r.get("c")); v=float(r.get("v") or 0)
        except Exception:
            continue
        if ts >= cutoff or min(o,h,l,c)<=0:
            continue
        rr.append((ts,o,h,l,c,max(v,0.0)))
    rr.sort(key=lambda x:x[0])
    if len(rr)<4:
        return None
    closes=np.array([x[4] for x in rr],dtype=float)
    highs=np.array([x[2] for x in rr],dtype=float)
    lows=np.array([x[3] for x in rr],dtype=float)
    vols=np.array([x[5] for x in rr],dtype=float)
    def ret(n):
        if len(closes)<=n:return float("nan")
        return float((closes[-1]/closes[-1-n]-1)*100)
    n12=min(12,len(rr)); n6=min(6,len(rr)); prev12=vols[-24:-12] if len(vols)>=24 else vols[:-n12]
    dv=closes*vols; typical=(highs+lows+closes)/3
    denom=float(vols[-n12:].sum())
    vwap=float((typical[-n12:]*vols[-n12:]).sum()/denom) if denom>0 else float(np.mean(typical[-n12:]))
    ranges=(highs-lows)/closes*100
    prior_high=float(np.max(highs[:-1])) if len(highs)>1 else highs[-1]
    recent_low=float(np.min(lows[-n12:])); recent_high=float(np.max(highs[-n12:]))
    span=max(recent_high-recent_low,1e-12)
    return {
        "ret_5m":ret(1),"ret_15m":ret(3),"ret_30m":ret(6),"ret_60m":ret(12),
        "accel_15_vs_60":ret(3)-(ret(12)/4 if math.isfinite(ret(12)) else 0.0),
        "volume_60m":float(vols[-n12:].sum()),"dollar_volume_60m":float(dv[-n12:].sum()),
        "volume_ratio_prev60":float(vols[-n12:].mean()/max(float(prev12.mean()) if len(prev12) else 1.0,1.0)),
        "range_pct_30m":float(np.mean(ranges[-n6:])),
        "range_expansion":float(np.mean(ranges[-n6:])/max(float(np.mean(ranges[-2*n6:-n6])) if len(ranges)>=2*n6 else float(np.mean(ranges)),1e-9)),
        "vwap_distance_pct":float((closes[-1]/vwap-1)*100) if vwap>0 else float("nan"),
        "close_position_60m":float((closes[-1]-recent_low)/span),
        "distance_prior_high_pct":float((closes[-1]/prior_high-1)*100) if prior_high>0 else float("nan"),
        "drawdown_from_60m_high_pct":float((closes[-1]/recent_high-1)*100),
        "higher_low_30m":float(1.0 if len(lows)>=6 and np.min(lows[-3:])>np.min(lows[-6:-3]) else 0.0),
        "bars_available":float(len(rr)),
    }

def ctr_score_at(rows, eval_dt, defs=None):
    defs=defs or ARTIFACT["definitions"]
    values={}
    for d in defs:
        f=fd_features(rows, eval_dt-timedelta(minutes=int(d["anchor_minutes"])))
        if f is not None:
            values.setdefault(str(d["anchor_minutes"]),{})[d["feature"]]=f.get(d["feature"])
    return score_feature_values(values)[:2]

def first_early_core_crossing(rows):
    parsed=[]
    for r in rows:
        try:
            ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00")); c=float(r.get("c"))
        except Exception:
            continue
        if c>0: parsed.append((ts,c))
    parsed.sort()
    for ts,c in parsed:
        ev=ts+timedelta(minutes=5)
        sc,den=ctr_score_at(rows,ev)
        if sc is not None and sc>=ARTIFACT["threshold"]:
            return ev,c,sc,den
    return None
