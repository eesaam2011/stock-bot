from datetime import datetime,timedelta,timezone
from early_core_engine import FrozenEarlyCoreEngine
from early_core_features import ctr_score_at
from early_core_score import ARTIFACT
from base_ready import phase2_features
UTC=timezone.utc

class Native5Provider:
 def __init__(self,rest):self.rest=rest
 def fetch_native_5min(self,symbol,session,start,end):return self.rest.native_5m(symbol,start,end)

class OperationalEarlyCore:
 def __init__(self,rest):self.engine=FrozenEarlyCoreEngine(Native5Provider(rest))
 def evaluate(self,symbol,rows,received_at):
  # Live native-5m rows are evaluated literally through frozen first-crossing engine contract.
  if not rows:return None
  start=datetime.fromisoformat(str(rows[0]["t"]).replace("Z","+00:00"))
  end=datetime.fromisoformat(str(rows[-1]["t"]).replace("Z","+00:00"))+timedelta(minutes=5)
  class P:
   def fetch_native_5min(self,*a):return rows
  return FrozenEarlyCoreEngine(P()).first_crossing(symbol,received_at.date().isoformat(),start,end,received_at)
 def telemetry(self,rows):
  # Observability only: never changes crossing, threshold, persistence, or alert decisions.
  threshold=float(ARTIFACT["threshold"]);scores=[]
  for r in rows or []:
   try:
    ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00"))
    ev=ts+timedelta(minutes=5)
    sc,_den=ctr_score_at(rows,ev)
    if sc is not None:scores.append(float(sc))
   except Exception:continue
  max_score=max(scores) if scores else None
  return {"scoreable_bars":len(scores),"max_score":max_score,
          "gap_to_threshold":None if max_score is None else threshold-max_score,
          "near_threshold":bool(max_score is not None and max_score>=threshold*0.90),
          "threshold":threshold}

class OperationalBaseReady:
 def __init__(self):self.history={}
 def on_completed_native_1m(self,symbol,bar,received_at=None):
  h=self.history.setdefault(symbol,[]);h.append(bar);self.history[symbol]=h[-60:]
  bar_end=datetime.fromisoformat(str(bar["t"]).replace("Z","+00:00"))+timedelta(minutes=1)
  received_at=received_at or datetime.now(UTC)
  ts=max(bar_end,received_at)
  x=phase2_features(self.history[symbol],bar_end)
  if x is None:return {"accepted":False,"base_ready":False}
  features,diag=x
  return {"accepted":True,"base_ready":diag["base_ready"],"features":features,"diagnostics":diag,"decision_available_ts":ts}
