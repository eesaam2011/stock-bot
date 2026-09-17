from datetime import datetime,timedelta,timezone
from early_core_engine import FrozenEarlyCoreEngine
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
