from __future__ import annotations
from datetime import datetime,timedelta,timezone
from statistics import mean
import math
UTC=timezone.utc
try:
    from zoneinfo import ZoneInfo
    NY=ZoneInfo("America/New_York")
except Exception:
    NY=UTC

def parse_dt(value):
    return datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(UTC)

def clamp(value,low=0.0,high=100.0):
    return max(low,min(high,float(value)))

def efficiency_ratio_45(bars):
    if len(bars)<2:return None
    closes=[float(b["c"]) for b in bars]
    path=sum(abs(c-p) for p,c in zip(closes,closes[1:]))
    return (closes[-1]-closes[0])/path if path>1e-12 else 0.0

def phase2_features(history,signal_time):
    completed=sorted([b for b in history if b.get("t") and parse_dt(b["t"])+timedelta(minutes=1)<=signal_time],key=lambda b:b["t"])
    if len(completed)<24:return None
    signal_time=parse_dt(completed[-1]["t"])
    tail=completed[-30:]; closes=[float(b["c"]) for b in tail]; highs=[float(b["h"]) for b in tail]
    total_volume=sum(float(b.get("v") or 0) for b in completed)
    vwap=(sum(float(b.get("vw") or b["c"])*float(b.get("v") or 0) for b in completed)/total_volume if total_volume else mean(closes))
    price=closes[-1]; recent=completed[-12:]; prior=completed[-24:-12]
    recent_volume=mean(float(b.get("v") or 0) for b in recent)
    prior_volume=mean(float(b.get("v") or 0) for b in prior) if prior else max(1.0,recent_volume)
    acceleration=recent_volume/max(1.0,prior_volume)
    low,high=min(closes),max(closes); span=max(1e-9,high-low)
    acceptance=clamp(100*mean(1 if c>=low+.55*span else 0 for c in closes))
    close_position=clamp(100*(price-low)/span)
    demand=clamp(.5*close_position+.25*acceptance+.25*min(100,acceleration*40))
    resistance=max(highs[-21:-1] or highs[-1:])
    reclaim=100 if price>=resistance*.998 else clamp(50+(price/resistance-1)*1000)
    pullback=clamp(100-max(0,(high-price)/max(high,1e-9)*500))
    reference=float(completed[0]["o"]); session_change=(price/reference-1)*100
    extension=clamp(max(0,session_change-12)*4+max(0,(price/max(vwap,1e-9)-1)*100-8)*5)
    trajectory=clamp(50+(closes[-1]/max(closes[max(0,len(closes)-10)],1e-9)-1)*700)
    continuity=clamp(mean(1 if float(b.get("n") or 0)>0 else 0 for b in recent)*100)
    spread_proxy=clamp(mean((float(b["h"])-float(b["l"]))/max(float(b["c"]),1e-9)*100 for b in recent),0,25)
    spread_quality=clamp(100-spread_proxy*15)
    participation=clamp(mean([min(100,acceleration*45),continuity,min(100,math.log10(total_volume+1)*18)]))
    persistence=clamp(mean([acceptance,pullback,trajectory])); liquidity=clamp(mean([spread_quality,continuity])); context=clamp(mean([100-extension,trajectory]))
    opportunity=clamp(.24*participation+.34*demand+.18*persistence+.10*liquidity+.14*context)
    failure=clamp(.30*(100-demand)+.25*(100-acceptance)+.20*(100-pullback)+.15*(100-reclaim)+.10*(100-spread_quality))
    window_start=signal_time-timedelta(minutes=45); window45=[b for b in completed if parse_dt(b["t"])>=window_start]
    if len(window45)<5:return None
    first_open=float(window45[0]["o"]); price_change=(float(window45[-1]["c"])/first_open-1)*100; er45=efficiency_ratio_45(window45)
    if er45 is None or price<=0:return None
    local=signal_time.astimezone(NY); minutes_since_open=local.hour*60+local.minute-570
    features={"price_change_pct_last45m":float(price_change),"er45":float(er45),"price_change_x_er45":float(price_change*er45),"log_signal_price":float(math.log(price)),"opportunity":float(opportunity),"failure_pressure":float(failure),"minutes_since_regular_open":float(minutes_since_open)}
    diagnostics={"price":price,"vwap":vwap,"resistance":resistance,"demand_efficiency":demand,"price_acceptance":acceptance,"volume_acceleration":acceleration,"spread_proxy_pct":spread_proxy,"bars_used":len(completed),"bars_used_last45m":len(window45),"base_ready":bool(opportunity>=88 and failure<=35 and price>=vwap and demand>=65 and acceptance>=62 and acceleration>=1)}
    return features,diagnostics
