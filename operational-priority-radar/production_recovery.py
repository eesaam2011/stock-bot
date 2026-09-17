import json
from datetime import datetime,timedelta,timezone
from state_store import key_early_core,key_base_ready,key_opportunity,validate_record,SchemaError
UTC=timezone.utc
class RecoveryFailure(RuntimeError):pass


class ProductionGapRecovery:
 def __init__(self,rest,overlap_seconds=120):self.rest=rest;self.overlap_seconds=overlap_seconds
 def recover(self,symbol,gap_start,gap_end):
  start=gap_start-timedelta(seconds=self.overlap_seconds)
  r1=self.rest.native_1m_gap(symbol,start,gap_end)
  try:r5=self.rest.native_5m(symbol,start,gap_end)
  except Exception:
   return {"status":"EARLY_CORE_DATA_UNAVAILABLE","rows1":r1,"rows5":None,"trusted":False}
  rows=[]
  for r in r1:
   if "t" in r:rows.append({"symbol":symbol,"timeframe":"1m","bar_start_ts":r["t"],**r})
  for r in r5:
   if "t" in r:rows.append({"symbol":symbol,"timeframe":"5m","bar_start_ts":r["t"],**r})
  d={}
  for r in rows:d[(r.get("timeframe"),r.get("bar_start_ts"))]=r
  return {"status":"RECOVERED","rows":[d[k] for k in sorted(d,key=lambda x:(x[1],x[0]))],"rows1":r1,"rows5":r5,"trusted":False}


class RedisCanonicalReader:
 def __init__(self,r,prefix="operational_priority_radar:v1"):self.r=r;self.prefix=prefix
 def _read(self,key,typ):
  raw=self.r.get(key)
  if raw is None:return None
  try:x=json.loads(raw)
  except Exception as e:raise RecoveryFailure("INVALID_CANONICAL_JSON") from e
  validate_record(x,typ);return x
 def get_e(self,sess,sym):return self._read(key_early_core(sess,sym),"early_core")
 def get_b(self,sess,sym):return self._read(key_base_ready(sess,sym),"base_ready")
 def get_opportunity(self,sess,sym):return self._read(key_opportunity(sess,sym),"opportunity")
 def earliest_decision_anchor(self,sess):
  earliest=None
  for typ in ("early_core","base_ready"):
   cur=0
   while True:
    cur,keys=self.r.scan(cursor=cur,match=f"{self.prefix}:{typ}:{sess}:*",count=500)
    for k in keys:
     raw=self.r.get(k)
     if not raw:continue
     try:x=json.loads(raw);ts=x.get("decision_available_ts")
     except Exception:continue
     if not ts:continue
     dt=datetime.fromisoformat(ts.replace("Z","+00:00"))
     if earliest is None or dt<earliest:earliest=dt
    if int(cur)==0:break
  return earliest
 def active_trades(self):
  cur=0;out=[]
  while True:
   cur,keys=self.r.scan(cursor=cur,match=f"{self.prefix}:trade:*",count=200)
   for k in keys:
    raw=self.r.get(k)
    if raw:
     x=json.loads(raw);validate_record(x,"trade")
     if x["state"] in {"ACTIVE_PRE_T1","ACTIVE_POST_T1","HALTED_ACTIVE"}:out.append(x)
   if int(cur)==0:return out

class ProductionStartupRecovery:
 def __init__(self,reader,rest,trade_reconciler,session,symbols,now_fn=None,overlap_seconds=120,status_tracker=None):
  self.reader=reader;self.rest=rest;self.trade_reconciler=trade_reconciler
  self.session=session;self.symbols=list(symbols);self.now_fn=now_fn or (lambda:datetime.now(UTC));self.overlap_seconds=overlap_seconds
  self.status_tracker=status_tracker;self.recovered_1m={};self.recovered_5m={};self.pending_halted={}
  self._base_reconciled=False
 def run(self):
  try:
   trades=self.reader.active_trades()
   # P0 first. HALTED_ACTIVE waits for authoritative SIP status after subscription.
   for t in trades:
    if t["state"]=="HALTED_ACTIVE":
     self.pending_halted[t["symbol"]]=t;continue
    r=self.trade_reconciler.reconcile(t)
    if r is False or (isinstance(r,dict) and r.get("ambiguous")):
     return {"gap_recovered":False,"reconciled":False,"reason":"ACTIVE_TRADE_RECONCILIATION_FAILED"}
   now=self.now_fn()
   # Startup recovery must not issue two serial REST requests per universe symbol.
   # Use one bounded broad-universe window and Alpaca multi-symbol batches when available.
   # The 60-minute default is the inherited recovery horizon; overlap remains unchanged.
   if hasattr(self.reader,"earliest_decision_anchor"):
    anchor=self.reader.earliest_decision_anchor(self.session) or (now-timedelta(minutes=60))
   else:
    anchors=[]
    for sym in self.symbols:
     e=self.reader.get_e(self.session,sym);b=self.reader.get_b(self.session,sym)
     anchors.extend(datetime.fromisoformat(x.get("decision_available_ts").replace("Z","+00:00")) for x in (e,b) if x and x.get("decision_available_ts"))
    anchor=min(anchors,default=now-timedelta(minutes=60))
   start=anchor-timedelta(seconds=self.overlap_seconds)
   if hasattr(self.rest,"native_recovery_batch"):
    r1,r5=self.rest.native_recovery_batch(self.symbols,start,now)
    for sym in self.symbols:
     self.recovered_1m[sym]=self._dedup(r1.get(sym,[]))
     self.recovered_5m[sym]=self._dedup(r5.get(sym,[]))
   else:
    # Deterministic adapter/test compatibility; production AlpacaREST always uses batch path.
    for sym in self.symbols:
     self.recovered_1m[sym]=self._dedup(self.rest.native_1m_gap(sym,start,now))
     self.recovered_5m[sym]=self._dedup(self.rest.native_5m(sym,start,now))
   self._base_reconciled=True
   result={"gap_recovered":True,"reconciled":not bool(self.pending_halted)}
   if self.pending_halted:result["pending_halt_status"]=sorted(self.pending_halted)
   return result
  except Exception as e:
   return {"gap_recovered":False,"reconciled":False,"reason":type(e).__name__}
 def _dedup(self,rows):
  d={}
  for r in rows:
   t=r.get("t")
   if t:d[t]=r
  return [d[k] for k in sorted(d)]
 def ready_after_stream(self):
  return self._base_reconciled and not self.pending_halted
 def on_disconnect(self):pass
 def on_status(self,msg):
  if self.status_tracker is None:return
  rec=self.status_tracker.ingest(msg)
  if not rec:return
  sym=rec["symbol"]
  if sym not in self.pending_halted:return
  if rec["state"]=="HALTED":return
  if rec["state"]!="TRADING":return
  trade=self.pending_halted[sym]
  # Resume is proven by SIP status; restore pre-halt state only for chronological reconciliation.
  pre=trade.get("pre_halt_state")
  if pre not in {"ACTIVE_PRE_T1","ACTIVE_POST_T1"}:return
  resumed=dict(trade);resumed["state"]=pre
  r=self.trade_reconciler.reconcile(resumed)
  if r is False or (isinstance(r,dict) and r.get("ambiguous")):return
  self.pending_halted.pop(sym,None)
 def on_stream_message(self,kind,msg):pass
