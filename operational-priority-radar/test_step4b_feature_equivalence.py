import math, unittest
from datetime import datetime, timezone, timedelta
import numpy as np
from early_core_features import fd_features, ctr_score_at, first_early_core_crossing
from early_core_score import ARTIFACT

def reference_fd_features(rows, cutoff):
    # Independent literal reference transcription of historical _fd_features.
    rr=[]
    for r in rows:
        try:
            ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00")); o=float(r.get("o")); h=float(r.get("h")); l=float(r.get("l")); c=float(r.get("c")); v=float(r.get("v") or 0)
        except Exception: continue
        if ts >= cutoff or min(o,h,l,c)<=0: continue
        rr.append((ts,o,h,l,c,max(v,0.0)))
    rr.sort(key=lambda x:x[0])
    if len(rr)<4:return None
    closes=np.array([x[4] for x in rr],dtype=float); highs=np.array([x[2] for x in rr],dtype=float); lows=np.array([x[3] for x in rr],dtype=float); vols=np.array([x[5] for x in rr],dtype=float)
    def ret(n):
        if len(closes)<=n:return float("nan")
        return float((closes[-1]/closes[-1-n]-1)*100)
    n12=min(12,len(rr)); n6=min(6,len(rr)); prev12=vols[-24:-12] if len(vols)>=24 else vols[:-n12]
    dv=closes*vols; typical=(highs+lows+closes)/3; denom=float(vols[-n12:].sum()); vwap=float((typical[-n12:]*vols[-n12:]).sum()/denom) if denom>0 else float(np.mean(typical[-n12:]))
    ranges=(highs-lows)/closes*100; prior_high=float(np.max(highs[:-1])) if len(highs)>1 else highs[-1]
    recent_low=float(np.min(lows[-n12:])); recent_high=float(np.max(highs[-n12:])); span=max(recent_high-recent_low,1e-12)
    return {"ret_5m":ret(1),"ret_15m":ret(3),"ret_30m":ret(6),"ret_60m":ret(12),"accel_15_vs_60":ret(3)-(ret(12)/4 if math.isfinite(ret(12)) else 0.0),"volume_60m":float(vols[-n12:].sum()),"dollar_volume_60m":float(dv[-n12:].sum()),"volume_ratio_prev60":float(vols[-n12:].mean()/max(float(prev12.mean()) if len(prev12) else 1.0,1.0)),"range_pct_30m":float(np.mean(ranges[-n6:])),"range_expansion":float(np.mean(ranges[-n6:])/max(float(np.mean(ranges[-2*n6:-n6])) if len(ranges)>=2*n6 else float(np.mean(ranges)),1e-9)),"vwap_distance_pct":float((closes[-1]/vwap-1)*100) if vwap>0 else float("nan"),"close_position_60m":float((closes[-1]-recent_low)/span),"distance_prior_high_pct":float((closes[-1]/prior_high-1)*100) if prior_high>0 else float("nan"),"drawdown_from_60m_high_pct":float((closes[-1]/recent_high-1)*100),"higher_low_30m":float(1.0 if len(lows)>=6 and np.min(lows[-3:])>np.min(lows[-6:-3]) else 0.0),"bars_available":float(len(rr))}

def bars(n=80, start=datetime(2026,9,16,13,0,tzinfo=timezone.utc)):
    out=[]
    for i in range(n):
        base=5.0 + i*0.012 + math.sin(i/4)*0.025
        out.append({"t":(start+timedelta(minutes=5*i)).isoformat().replace("+00:00","Z"),
                    "o":base,"h":base+0.035+(i%3)*.002,"l":base-.025,"c":base+.01,
                    "v":10000+i*137+(i%7)*1000})
    return out

class TestStep4BFeatureEquivalence(unittest.TestCase):
    def assertFeatureMapsEqual(self,a,b):
        self.assertEqual(set(a),set(b))
        for k in a:
            if math.isnan(a[k]) and math.isnan(b[k]): continue
            self.assertEqual(a[k],b[k],k)

    def test_literal_feature_equivalence_multiple_cutoffs(self):
        rr=bars()
        for mins in (25,60,120,240,360):
            cutoff=datetime(2026,9,16,13,0,tzinfo=timezone.utc)+timedelta(minutes=mins)
            self.assertFeatureMapsEqual(fd_features(rr,cutoff),reference_fd_features(rr,cutoff))

    def test_cutoff_is_strict_timestamp_less_than(self):
        rr=bars(10); cutoff=datetime.fromisoformat(rr[5]["t"].replace("Z","+00:00"))
        f=fd_features(rr,cutoff)
        self.assertEqual(f["bars_available"],5.0)

    def test_invalid_and_nonpositive_rows_are_ignored(self):
        rr=bars(10); rr.append({"t":"bad"}); rr.append({"t":"2026-09-16T14:00:00Z","o":0,"h":1,"l":1,"c":1,"v":5})
        cutoff=datetime(2026,9,16,15,0,tzinfo=timezone.utc)
        self.assertFeatureMapsEqual(fd_features(rr,cutoff),reference_fd_features(rr,cutoff))

    def test_negative_volume_is_clamped_zero(self):
        rr=bars(10); rr[3]["v"]=-500
        cutoff=datetime(2026,9,16,15,0,tzinfo=timezone.utc)
        self.assertFeatureMapsEqual(fd_features(rr,cutoff),reference_fd_features(rr,cutoff))

    def test_less_than_four_valid_bars_unscoreable(self):
        self.assertIsNone(fd_features(bars(3),datetime(2026,9,17,tzinfo=timezone.utc)))

    def test_native_5m_event_availability_is_bar_start_plus_5m(self):
        rr=bars(20)
        # first crossing engine's event times must always be aligned to source start +5m
        x=first_early_core_crossing(rr)
        if x is not None:
            starts={datetime.fromisoformat(r["t"].replace("Z","+00:00")) for r in rr}
            self.assertIn(x[0]-timedelta(minutes=5),starts)

    def test_score_equivalence_against_reference_feature_path(self):
        rr=bars(80); ev=datetime(2026,9,16,19,0,tzinfo=timezone.utc)
        a=ctr_score_at(rr,ev)
        # independently compute exact same frozen definition loop from reference features
        num=den=0.0
        for d in ARTIFACT["definitions"]:
            f=reference_fd_features(rr,ev-timedelta(minutes=d["anchor_minutes"]))
            v=(f or {}).get(d["feature"])
            if not isinstance(v,(int,float)) or not math.isfinite(v): continue
            z=d["direction"]*(float(v)-d["hard_negative_center"])/d["pooled_symbol_sd"]
            num+=d["weight"]*max(-3,min(3,z)); den+=d["weight"]
        b=(num/den,den) if den>=.5 else (None,den)
        self.assertEqual(a,b)

if __name__=="__main__":
    unittest.main()
