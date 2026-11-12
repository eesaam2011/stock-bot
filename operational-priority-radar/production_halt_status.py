from datetime import datetime,timezone
UTC=timezone.utc
HALT_CODES={"2","H","P"}
RESUME_CODES={"3","Q","T"}
class StatusUnproven(RuntimeError):pass
class ProductionStatusTracker:
 def __init__(self):self.latest={}
 def ingest(self,msg):
  symbol=msg.get("S");code=str(msg.get("sc",""))
  if not symbol or not code:return None
  if code in HALT_CODES:state="HALTED"
  elif code in RESUME_CODES:state="TRADING"
  else:state="OTHER"
  rec={"symbol":symbol,"state":state,"status_code":code,"event_ts":msg.get("t"),"raw":dict(msg)}
  self.latest[symbol]=rec;return rec
 def current(self,symbol):
  x=self.latest.get(symbol)
  return x["state"] if x else "UNKNOWN"
 def reset(self):
  # A prior SIP connection's statuses cannot authorize entries in a new epoch.
  self.latest.clear()
