from datetime import datetime,timedelta,timezone
from collections import deque
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
 def required_history_minutes(self):
  # Frozen Early Core contains anchors as far back as 240m. Its deepest return
  # feature (ret_60m) needs 13 native 5m closes before that cutoff.
  max_anchor=max(int(d["anchor_minutes"]) for d in ARTIFACT["definitions"])
  return max_anchor + 13*5

 def telemetry(self,rows):
  # Observability only: mirror first_crossing row eligibility (parseable ts + c>0).
  # This never changes crossing, threshold, persistence, or alert decisions.
  threshold=float(ARTIFACT["threshold"]);scores=[];max_den=0.0;score_attempts=0
  invalid_close_bars=0;eligible_above_threshold=0
  for r in rows or []:
   try:
    ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00"))
    c=float(r.get("c"))
   except Exception:
    invalid_close_bars+=1
    continue
   if c<=0:
    invalid_close_bars+=1
    continue
   try:
    ev=ts+timedelta(minutes=5)
    sc,den=ctr_score_at(rows,ev)
    score_attempts+=1
    max_den=max(max_den,float(den or 0.0))
    if sc is not None:
     score=float(sc);scores.append(score)
     if score>=threshold:eligible_above_threshold+=1
   except Exception:continue
  max_score=max(scores) if scores else None
  return {"score_attempts":score_attempts,
          "eligible_rows":score_attempts,
          "invalid_close_bars":invalid_close_bars,
          "scoreable_bars":len(scores),
          "unscoreable_bars":max(0,score_attempts-len(scores)),
          "eligible_above_threshold":eligible_above_threshold,
          "max_observed_weight":max_den,
          "max_score":max_score,
          "gap_to_threshold":None if max_score is None else threshold-max_score,
          "near_threshold":bool(max_score is not None and max_score>=threshold*0.90),
          "threshold":threshold,
          "required_history_minutes":self.required_history_minutes()}

class OperationalBaseReady:
 _BAR_KEYS=("t","o","h","l","c","v","vw","n")
 def __init__(self):self.history={}
 @classmethod
 def _compact_bar(cls,bar):
  # Representation-only memory optimization: store each bar as a fixed tuple.
  # phase2_features() remains untouched and still receives the exact dict shape it expects.
  return tuple(bar.get(k) for k in cls._BAR_KEYS)
 @classmethod
 def _materialize_history(cls,history):
  # Thin compatibility boundary. Dicts exist only for the duration of feature evaluation.
  return [dict(zip(cls._BAR_KEYS,row)) for row in history]
 def on_completed_native_1m(self,symbol,bar,received_at=None):
  h=self.history.get(symbol)
  if h is None:
   h=deque(maxlen=60);self.history[symbol]=h
  h.append(self._compact_bar(bar))
  bar_end=datetime.fromisoformat(str(bar["t"]).replace("Z","+00:00"))+timedelta(minutes=1)
  received_at=received_at or datetime.now(UTC)
  ts=max(bar_end,received_at)
  x=phase2_features(self._materialize_history(h),bar_end)
  if x is None:return {"accepted":False,"base_ready":False}
  features,diag=x
  return {"accepted":True,"base_ready":diag["base_ready"],"features":features,"diagnostics":diag,"decision_available_ts":ts}
 def release_symbol(self,symbol):
  self.history.pop(symbol,None)
 def buffered_bars(self):
  return sum(len(v) for v in self.history.values())
