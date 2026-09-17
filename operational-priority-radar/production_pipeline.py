from runtime_provenance import stamp
from datetime import datetime,timedelta,timezone
from state_store import CanonicalStateStore,key_early_core,key_base_ready,key_opportunity,key_trade,canonical_json,base_record,validate_record
from early_core_state import EarlyCoreStateWriter
from base_ready_state import BaseReadyStateWriter
from confluence_state import ConfluenceStateWriter
from entry_engine import SIPTrade,evaluate_entry_price,EntryStatus
from risk_engine import StructureBar,evaluate_structural_risk,RiskStatus
from entry_commit import EntryCommitBuilder
from trade_monitor import TradeState,MarketEvent,apply_event,MonitorEvent
from event_ids import trade_event_id
UTC=timezone.utc
def dt(x):return x if isinstance(x,datetime) else datetime.fromisoformat(str(x).replace("Z","+00:00"))

class ProductionDecisionPipeline:
 def __init__(self,r,lua,leadership,session,symbols,early_core,base_ready,rest,worker_id,shadow=True):
  self.r=r;self.lua=lua;self.leadership=leadership;self.session=session;self.symbols=list(symbols)
  self.ec=early_core;self.br=base_ready;self.rest=rest;self.worker_id=worker_id;self.shadow=shadow
  self.store=CanonicalStateStore(__import__("production_runtime_state").RedisCanonicalBackend(r))
  self.ew=EarlyCoreStateWriter(self.store,leadership);self.bw=BaseReadyStateWriter(self.store,leadership);self.cw=ConfluenceStateWriter(self.store,leadership)
  self.trades={};self.bars={};self.halted=set();self.last_native5={}
 def _get(self,key,typ=None):
  raw=self.r.get(key)
  if not raw:return None
  x=__import__("json").loads(raw);validate_record(x,typ);return x
 def on_bar(self,symbol,bar,received_at,allow_decision):
  h=self.bars.setdefault(symbol,[]);h.append((bar,received_at));self.bars[symbol]=h[-60:]
  if not allow_decision:return
  out=self.br.on_completed_native_1m(symbol,bar,received_at)
  if out.get("base_ready") and not self._get(key_base_ready(self.session,symbol)):
   bs=dt(bar["t"]);be=bs+timedelta(minutes=1)
   try:self.bw.persist_first_b(self.session,symbol,bs,be,received_at,max(be,received_at),out["features"],out["diagnostics"])
   except Exception:pass
   self._confluence(symbol,received_at)
  self._monitor_bar(symbol,bar,received_at)
 def poll_native5(self,now,allow_decision,batch_size=500,max_workers=4):
  # Engineering-only transport batching. Early Core still receives native Alpaca 5Min rows per symbol.
  stats={"eligible_symbols":0,"symbols_with_rows":0,"crossings":0,"batch_size":int(batch_size)}
  if not allow_decision:return stats
  pending=[s for s in self.symbols if not self._get(key_early_core(self.session,s))]
  stats["eligible_symbols"]=len(pending)
  if not pending:return stats
  start=now-timedelta(minutes=60)
  rows_by_symbol=self.rest.bars_multi(pending,start,now,"5Min",batch_size=batch_size,max_workers=max_workers)
  for symbol in pending:
   rows=[{**r,"_timeframe":"native_5Min"} for r in (rows_by_symbol.get(symbol) or [])]
   if rows:stats["symbols_with_rows"]+=1
   crossing=self.ec.evaluate(symbol,rows,now)
   if crossing:
    try:self.ew.persist_first_e(crossing)
    except Exception:pass
    else:stats["crossings"]+=1
    self._confluence(symbol,now)
  return stats
 def _confluence(self,symbol,now):
  if self._get(key_opportunity(self.session,symbol)):return
  e=self._get(key_early_core(self.session,symbol),"early_core");b=self._get(key_base_ready(self.session,symbol),"base_ready")
  try:self.cw.evaluate_and_persist(self.session,symbol,e,b,now)
  except Exception:pass
 def on_trade(self,symbol,msg,received_at,allow_decision):
  price=msg.get("p");ts=msg.get("t")
  if price is None or ts is None:return
  tr=SIPTrade(float(price),dt(ts),received_at,int(msg.get("_seq",0)),True)
  xs=self.trades.setdefault(symbol,[]);xs.append(tr);self.trades[symbol]=xs[-500:]
  if allow_decision:self._try_entry(symbol,received_at)
  self._monitor_trade(symbol,tr)
 def on_status(self,msg):
  symbol=msg.get("S");code=str(msg.get("sc",""))
  if code in {"2","H","P"}:self.halted.add(symbol)
  elif code in {"3","Q","T"}:self.halted.discard(symbol)
 def _try_entry(self,symbol,now):
  opp=self._get(key_opportunity(self.session,symbol),"opportunity")
  if not opp or opp["state"]!="CONFLUENCE_VALID":return
  trigger=dt(opp["entry_trigger_ts"])
  ed=evaluate_entry_price(trigger,now,self.trades.get(symbol,[]),symbol in self.halted)
  if ed.status!=EntryStatus.PRICE_READY:return
  e=self._get(key_early_core(self.session,symbol),"early_core");b=self._get(key_base_ready(self.session,symbol),"base_ready")
  first=min(dt(e["decision_available_ts"]),dt(b["decision_available_ts"]))
  bars=[]
  for bar,recv in self.bars.get(symbol,[]):
   bs=dt(bar["t"]);bars.append(StructureBar(bs,bs+timedelta(minutes=1),float(bar["l"]),recv))
  risk=evaluate_structural_risk(ed.entry_alert_price,first,trigger,bars)
  if risk.status!=RiskStatus.APPROVED:return
  new,trade,out=EntryCommitBuilder(self.leadership).build(opp,risk,ed.entry_alert_price,now)
  new,trade,out=stamp(new),stamp(trade),stamp(out)
  self.lua.atomic_entry(self.worker_id,key_opportunity(self.session,symbol),canonical_json(opp),canonical_json(new),key_trade(trade["trade_id"]),canonical_json(trade),f"operational_priority_radar:v1:outbox:{out['event_id']}",canonical_json(out))
 def _active(self,symbol):
  cur=0
  while True:
   cur,keys=self.r.scan(cursor=cur,match="operational_priority_radar:v1:trade:*",count=100)
   for k in keys:
    x=self._get(k,"trade")
    if x and x["symbol"]==symbol and x["state"] in {"ACTIVE_PRE_T1","ACTIVE_POST_T1"}:return x
   if int(cur)==0:return None
 def _commit_trade(self,t,ns,evt,ts,price=None):
  et={"T1":"T1","T2":"T2","STOP":"STOP","POST_T1_EXIT":"POST_T1_EXIT","MONITORING_EXPIRED":"FINAL"}.get(evt.value)
  if not et:return
  new=dict(t);new["state"]=ns.state;new["updated_at"]=ts.isoformat()
  tok=self.leadership.require_current();new["leader_generation"]=tok.leader_generation;new["worker_instance_id"]=tok.worker_instance_id
  eid=trade_event_id(et,t["trade_id"]);out=base_record("outbox",t["session"],t["symbol"],"PENDING",ts.isoformat())
  payload={"trade_id":t["trade_id"],"event":evt.value,"event_ts":ts.isoformat()}
  if evt==MonitorEvent.STOP:payload["first_observed_breach_price"]=price
  out.update(event_id=eid,event_type=et,payload=payload,attempt_count=0,leader_generation=tok.leader_generation)
  new,out=stamp(new),stamp(out)
  self.lua.atomic_trade_event(self.worker_id,key_trade(t["trade_id"]),canonical_json(t),canonical_json(new),f"operational_priority_radar:v1:outbox:{eid}",canonical_json(out))
 def _state(self,t):return TradeState(t["state"],float(t["entry_alert_price"]),float(t["structural_stop"]),float(t["t1"]),float(t["t2"]),dt(t["monitoring_deadline"]),t.get("pre_halt_state"))
 def _monitor_trade(self,symbol,tr):
  t=self._active(symbol)
  if not t:return
  ns,evt=apply_event(self._state(t),MarketEvent("TRADE",tr.trade_ts,tr.sequence,tr.price,True))
  if evt!=MonitorEvent.NONE:self._commit_trade(t,ns,evt,tr.trade_ts,tr.price)
 def _monitor_bar(self,symbol,bar,received_at):
  t=self._active(symbol)
  if not t:return
  ts=dt(bar["t"])+timedelta(minutes=1)
  ns,evt=apply_event(self._state(t),MarketEvent("BAR_CLOSE",ts,0,float(bar["c"]),True))
  if evt!=MonitorEvent.NONE:self._commit_trade(t,ns,evt,ts)
