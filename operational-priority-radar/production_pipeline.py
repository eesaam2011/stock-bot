from runtime_provenance import stamp
from datetime import datetime,timedelta,timezone
import gc,os,time,sys
from bounded_telemetry import capture_sample,estimate_sample
from state_store import CanonicalStateStore,key_early_core,key_base_ready,key_opportunity,key_trade,canonical_json,base_record,validate_record
from early_core_state import EarlyCoreStateWriter
from base_ready_state import BaseReadyStateWriter
from confluence_state import ConfluenceStateWriter
from entry_engine import SIPTrade,evaluate_entry_price,EntryStatus
from risk_engine import StructureBar,evaluate_structural_risk,RiskStatus
from entry_commit import EntryCommitBuilder
from trade_monitor import TradeState,MarketEvent,apply_event,MonitorEvent
from event_ids import trade_event_id
from confluence_diagnostics import ConfluenceDiagnostics,raise_if_leadership_lost
from redis_lua_production import LeaseLost
from leadership import LeadershipError
from liquidity_telemetry import LiquidityTelemetry
from signal_admission import SignalAdmission
from collections import Counter
import threading
from functools import wraps
from constants import MAX_ENTRY_TRADE_AGE_SECONDS,ENTRY_PRICE_WAIT_SECONDS
def synchronized(fn):
 @wraps(fn)
 def wrapped(self,*a,**kw):
  with self._decision_lock:return fn(self,*a,**kw)
 return wrapped
UTC=timezone.utc
class ConfluenceEvaluationError(RuntimeError):pass
def dt(x):return x if isinstance(x,datetime) else datetime.fromisoformat(str(x).replace("Z","+00:00"))

class ProductionDecisionPipeline:
 def __init__(self,r,lua,leadership,session,symbols,early_core,base_ready,rest,worker_id,shadow=True):
  self._decision_lock=threading.RLock();self._admitted_symbols=set(symbols)
  self.r=r;self.lua=lua;self.leadership=leadership;self.session=session;self.symbols=list(symbols)
  self.ec=early_core;self.br=base_ready;self.rest=rest;self.worker_id=worker_id;self.shadow=shadow
  self.store=CanonicalStateStore(__import__("production_runtime_state").RedisCanonicalBackend(r))
  self.ew=EarlyCoreStateWriter(self.store,leadership);self.bw=BaseReadyStateWriter(self.store,leadership);self.cw=ConfluenceStateWriter(self.store,leadership)
  self.utc_now=lambda:datetime.now(UTC)
  self.signal_gate=SignalAdmission.from_environment(session)
  self._sample_lock=threading.Lock();self._sample_next_due=0.0;self._sample_last=None;self.terminal_counts=Counter();self.entry_exit_counts=Counter();self.entry_exit_counts_by_symbol=Counter();self._entry_uncertain=set();self._base_completed=set();self._confluence_retry=set()
  self.trades={};self.bars={};self.halted=set();self.last_native5={}
  self.max_bar_buffer=20;self.entry_trade_buffer_seconds=15
  self.raw_trade_messages_received=0
  # Hot-path caches: SIP trade flow must never perform Redis scans/GETs per tick.
  # These caches mirror canonical Redis state and are refreshed after startup recovery.
  self._active_by_symbol={};self._entry_opportunities={}
  # Structure-bar retention is only needed after a symbol has produced E or B.
  # Keeping 20 bars for every market symbol duplicates the broad BASE_READY history.
  self._structure_watch_symbols=set()
  self.confluence_diagnostics=ConfluenceDiagnostics()
  self.liquidity_telemetry=LiquidityTelemetry(symbols=self.symbols)
 def _current_rss_bytes(self):
  # Linux/Render current resident set size. Diagnostic only; never affects decisions.
  try:
   with open("/proc/self/status","r",encoding="utf-8") as f:
    for line in f:
     if line.startswith("VmRSS:"):
      return int(line.split()[1])*1024
  except Exception:
   pass
  return None
 def _peak_rss_bytes(self):
  # ru_maxrss is KiB on Linux. High-water mark only; diagnostic telemetry.
  try:
   import resource
   return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)*1024
  except Exception:
   return None
 @synchronized
 def _memory_probe(self,stage,**extra):
  if stage not in {"native5_cycle_before","native5_cycle_after"}:return {}
  x={"stage":"SHADOW_MEMORY_PROBE","memory_stage":stage,
     "rss_bytes":self._current_rss_bytes(),"peak_rss_bytes":self._peak_rss_bytes(),
     "structure_symbols":len(self.bars),
     "structure_bars":sum(len(v) for v in self.bars.values()),
     "base_ready_symbols":len(getattr(self.br,"history",{})),
     "base_ready_bars":self.br.buffered_bars() if hasattr(self.br,"buffered_bars") else None,
     "structure_watch_symbols":len(self._structure_watch_symbols)}
  x.update(extra);print(x,flush=True);return x
 def memory_stats(self):
  capture=None;sample_claimed=False
  with self._decision_lock:
   now=time.monotonic()
   if now>=self._sample_next_due and self._sample_lock.acquire(blocking=False):
    sample_claimed=True;self._sample_next_due=now+900.0
    try:capture=capture_sample(self,200)
    except Exception as exc:
     self._sample_last={"status":"ERROR","error_type":type(exc).__name__};self._sample_lock.release();sample_claimed=False
   stats=self._cheap_memory_stats()
  # Snapshot holds at most200 symbols per sampled store; estimate OUTSIDE decision lock.
  if sample_claimed:
   try:self._sample_last=estimate_sample(capture)
   except Exception as exc:self._sample_last={"status":"ERROR","error_type":type(exc).__name__}
   finally:self._sample_lock.release()
  stats["container_memory_sample"]=self._sample_last
  stats["deep_sample_next_due_monotonic"]=self._sample_next_due
  return stats
 def _cheap_memory_stats(self):
  return {"raw_trade_messages_received":self.ws_ingress.received.get("TRADE",0) if hasattr(self,"ws_ingress") else self.raw_trade_messages_received,"ws_ingress":self.ws_ingress.snapshot() if hasattr(self,"ws_ingress") else None,"disconnect_log_errors":getattr(self,"disconnect_log_errors",0),"gc_generation_counts":list(gc.get_count()),"terminal_counts":dict(self.terminal_counts),"entry_exit_counts_by_symbol":[{"symbol":k[0],"reason":k[1],"count":v} for k,v in sorted(self.entry_exit_counts_by_symbol.items())],"entry_trade_buffer_count":sum(len(v) for v in self.trades.values()),"signal_admission":dict(self.signal_gate.counts),"entry_exit_counts":dict(self.entry_exit_counts),"active_trades":len(self._active_by_symbol),"entry_opportunities":len(self._entry_opportunities),"confluence_errors":self.confluence_diagnostics.snapshot(),
          "liquidity_telemetry":self.liquidity_telemetry.snapshot(),
          "structure_symbols":len(self.bars),
          "structure_bars":sum(len(v) for v in self.bars.values()),
          "base_ready_symbols":len(getattr(self.br,"history",{})),
          "base_ready_bars":self.br.buffered_bars() if hasattr(self.br,"buffered_bars") else None,
          "structure_watch_symbols":len(self._structure_watch_symbols),
          "rss_bytes":self._current_rss_bytes(),
          "peak_rss_bytes":self._peak_rss_bytes()}
 def _get(self,key,typ=None):
  raw=self.r.get(key)
  if not raw:return None
  x=__import__("json").loads(raw);validate_record(x,typ);return x
 def _scan_records(self,pattern,typ):
  import json
  cur=0;keys=[]
  while True:
   cur,part=self.r.scan(cursor=cur,match=pattern,count=500);keys.extend(part)
   if int(cur)==0:break
  out=[]
  for i in range(0,len(keys),500):
   chunk=keys[i:i+500]
   vals=self.r.mget(chunk) if hasattr(self.r,"mget") else [self.r.get(k) for k in chunk]
   for raw in vals:
    if not raw:continue
    try:x=json.loads(raw);validate_record(x,typ)
    except Exception:continue
    out.append(x)
  return out
 @synchronized
 def refresh_runtime_caches(self):
  # Called after canonical startup/gap recovery and before SIP is trusted.
  active={}
  for x in self._scan_records("operational_priority_radar:v1:trade:*","trade"):
   if x.get("session")==self.session and x.get("state") in {"ACTIVE_PRE_T1","ACTIVE_POST_T1","HALTED_ACTIVE"}:
    active[x["symbol"]]=x
  opps={}
  for x in self._scan_records("operational_priority_radar:v1:opportunity:*","opportunity"):
   if x.get("session")==self.session and x.get("state")=="CONFLUENCE_VALID":opps[x["symbol"]]=x
  watches=set(active)|set(opps)
  self._base_completed=set()
  for typ,pat in (("early_core","operational_priority_radar:v1:early_core:*"),
                  ("base_ready","operational_priority_radar:v1:base_ready:*")):
   for x in self._scan_records(pat,typ):
    if x.get("session")==self.session:
     watches.add(x["symbol"])
     if typ=="base_ready":self._base_completed.add(x["symbol"])
  self._entry_uncertain.clear()
  self._active_by_symbol=active;self._entry_opportunities=opps;self._structure_watch_symbols=watches
  return {"active_trades":len(active),"entry_opportunities":len(opps),"structure_watch_symbols":len(watches)}
 def _retain_structure_bar(self,symbol,bar,received_at):
  # Structure risk needs only timestamp + low; keep a bounded compact record.
  h=self.bars.setdefault(symbol,[])
  h.append((bar.get("t"),bar.get("l"),received_at))
  self.bars[symbol]=h[-self.max_bar_buffer:]
 @synchronized
 def on_bar(self,symbol,bar,received_at,allow_decision):
  if symbol not in self._admitted_symbols:return
  if not allow_decision:
   self._monitor_bar(symbol,bar,received_at);return
  if symbol in self._base_completed:
   if dt(bar["t"])+timedelta(minutes=1)<=received_at:
    if symbol in self._structure_watch_symbols:self._retain_structure_bar(symbol,bar,received_at)
    self._monitor_bar(symbol,bar,received_at)
   if symbol in self._confluence_retry:
    try:self._confluence(symbol,received_at)
    except ConfluenceEvaluationError as exc:
     self._confluence_retry.add(symbol);self.confluence_diagnostics.record(str(exc),"on_bar")
   return
  end=dt(bar["t"])+timedelta(minutes=1)
  admission_now=self.utc_now()
  reason=self.signal_gate.reason(end,admission_now,1)
  if reason:
   self.signal_gate.record(reason,symbol,"B",end,admission_now)
   if end<=received_at:
    if hasattr(self.br,"accumulate_only"):self.br.accumulate_only(symbol,bar)
    if symbol in self._structure_watch_symbols:self._retain_structure_bar(symbol,bar,received_at)
    self._monitor_bar(symbol,bar,received_at)
   return
  out=self.br.on_completed_native_1m(symbol,bar,received_at)
  b_now=False
  if out.get("base_ready"):
   existing=self._get(key_base_ready(self.session,symbol))
   if not existing:
    bs=dt(bar["t"]);be=bs+timedelta(minutes=1)
    persist_now=self.utc_now()
    reason=self.signal_gate.reason(be,persist_now,1)
    if reason:
     self.signal_gate.record(reason,symbol,"B",be,persist_now)
     if symbol in self._structure_watch_symbols:self._retain_structure_bar(symbol,bar,received_at)
     self._monitor_bar(symbol,bar,received_at);return
    try:_b_key,b_record=self.bw.persist_first_b(self.session,symbol,bs,be,received_at,max(be,received_at),out["features"],out["diagnostics"])
    except Exception as exc:
     try:raise_if_leadership_lost(exc)
     except (LeaseLost,LeadershipError):
      self._confluence_lease_lost=True;raise
     print({"stage":"SHADOW_B_PERSIST_ERROR","symbol":symbol,"error_type":type(exc).__name__},flush=True)
    else:
     b_now=True;self._base_completed.add(symbol)
     print({"stage":"SHADOW_FIRST_B","symbol":symbol,
            **self.liquidity_telemetry.first(symbol,"B",b_record.get("event_id")),
            "received_at":b_record.get("received_at"),"evaluation_utc":self.utc_now().isoformat(),
            "bar_end_ts":b_record.get("bar_end_ts"),
            "decision_available_ts":b_record.get("decision_available_ts"),
            "opportunity":b_record.get("features",{}).get("opportunity"),
            "failure_pressure":b_record.get("features",{}).get("failure_pressure"),
            "demand_efficiency":b_record.get("features",{}).get("demand_efficiency"),
            "price_acceptance":b_record.get("features",{}).get("price_acceptance"),
            "volume_acceleration":b_record.get("features",{}).get("volume_acceleration")},flush=True)
   if existing or b_now:
    self._structure_watch_symbols.add(symbol)
    # BASE_READY is first-event-only for the session; release its 60-bar discovery history.
    if hasattr(self.br,"release_symbol"):self.br.release_symbol(symbol)
   try:self._confluence(symbol,received_at)
   except ConfluenceEvaluationError as exc:
    self._confluence_retry.add(symbol);self.confluence_diagnostics.record(str(exc),"on_bar")
  if symbol in self._structure_watch_symbols:self._retain_structure_bar(symbol,bar,received_at)
  self._monitor_bar(symbol,bar,received_at)
 def poll_native5(self,now,allow_decision,batch_size=500,max_workers=4):
  # Engineering-only transport batching. Early Core still receives native Alpaca 5Min rows per symbol.
  stats={"confluence_errors":0,"eligible_symbols":0,"symbols_with_rows":0,"evaluated_symbols":0,"eligible_rows":0,"invalid_close_bars":0,"scoreable_symbols":0,"scoreable_bars":0,"unscoreable_bars":0,"eligible_above_threshold":0,"near_threshold":0,"max_observed_weight":0.0,"max_score":None,"max_score_symbol":None,"gap_to_threshold":None,"threshold":None,"required_history_minutes":None,"crossings":0,"early_core_crossings_detected":0,"early_core_persisted":0,"early_core_persist_errors":0,"early_core_first_persist_error":None,"batch_size":int(batch_size)}
  if not allow_decision or self.signal_gate.window_reason(now):return stats
  # One Redis round-trip per chunk instead of one GET per symbol. This is transport/runtime
  # optimization only; it does not change Early Core eligibility or event semantics.
  pending=[]
  chunk_size=1000
  if hasattr(getattr(self,"r",None),"mget"):
   for i in range(0,len(self.symbols),chunk_size):
    chunk=self.symbols[i:i+chunk_size]
    keys=[key_early_core(self.session,s) for s in chunk]
    vals=self.r.mget(keys)
    pending.extend(s for s,v in zip(chunk,vals) if not v)
  else:
   # Deterministic injected/test seam; production Redis uses the batched MGET path above.
   pending=[s for s in self.symbols if not self._get(key_early_core(self.session,s))]
  stats["eligible_symbols"]=len(pending)
  if not pending:return stats
  # STEP16X correctness fix: the frozen model has anchors out to 240m and
  # ret_60m at that anchor requires 13 native 5m closes. A 60m fetch can never
  # make the frozen model scoreable. Derive the exact minimum from the artifact.
  history_minutes=int(self.ec.required_history_minutes())
  stats["required_history_minutes"]=history_minutes
  start=now-timedelta(minutes=history_minutes)
  cycle_t0=time.monotonic()
  self._memory_probe("native5_cycle_before",eligible_symbols=len(pending),batch_size=int(batch_size),max_workers=int(max_workers))
  # Memory-safe streaming: never materialize native 5m rows for the full universe at once.
  # Each REST request remains native Alpaca 5Min and uses the frozen engineering batch size.
  for i in range(0,len(pending),batch_size):
   chunk=pending[i:i+batch_size]
   batch_no=(i//batch_size)+1
   batch_t0=time.monotonic()
   self._memory_probe("native5_before_bars_multi",batch_no=batch_no,chunk_symbols=len(chunk))
   rows_by_symbol=self.rest.bars_multi(chunk,start,now,"5Min",batch_size=batch_size,max_workers=max_workers)
   fetch_seconds=round(time.monotonic()-batch_t0,3)
   fetched_rows=sum(len(v or []) for v in rows_by_symbol.values())
   self._memory_probe("native5_after_bars_multi",batch_no=batch_no,chunk_symbols=len(chunk),fetched_rows=fetched_rows,fetch_seconds=fetch_seconds)
   process_t0=time.monotonic()
   for symbol in chunk:
    rows=[{**r,"_timeframe":"native_5Min"} for r in (rows_by_symbol.get(symbol) or []) if dt(r["t"])+timedelta(minutes=5)<=now]
    decision_now=self.utc_now()
    admission_by_end={}
    def eligible_end(end):
     if end not in admission_by_end:
      reason=self.signal_gate.reason(end,decision_now,5)
      if reason:self.signal_gate.record(reason,symbol,"E",end,decision_now)
      admission_by_end[end]=reason is None
     return admission_by_end[end]
    self.ec.eligible_end=eligible_end
    if rows:stats["symbols_with_rows"]+=1
    fused=False;crossing=None
    if rows:
     stats["evaluated_symbols"]+=1
     if hasattr(self.ec,"evaluate_with_telemetry"):
      tele,crossing=self.ec.evaluate_with_telemetry(symbol,rows,decision_now);fused=True
     else:
      tele=self.ec.telemetry(rows)
     stats["eligible_rows"]+=int(tele.get("eligible_rows") or 0)
     stats["invalid_close_bars"]+=int(tele.get("invalid_close_bars") or 0)
     stats["scoreable_bars"]+=int(tele.get("scoreable_bars") or 0)
     stats["unscoreable_bars"]+=int(tele.get("unscoreable_bars") or 0)
     stats["eligible_above_threshold"]+=int(tele.get("eligible_above_threshold") or 0)
     stats["max_observed_weight"]=max(float(stats["max_observed_weight"] or 0.0),float(tele.get("max_observed_weight") or 0.0))
     if tele.get("scoreable_bars"):stats["scoreable_symbols"]+=1
     if tele.get("near_threshold"):stats["near_threshold"]+=1
     stats["threshold"]=tele.get("threshold")
     stats["required_history_minutes"]=tele.get("required_history_minutes")
     sc=tele.get("max_score")
     if sc is not None and (stats["max_score"] is None or sc>stats["max_score"]):
      stats["max_score"]=sc;stats["max_score_symbol"]=symbol;stats["gap_to_threshold"]=tele.get("gap_to_threshold")
    if rows and not fused:crossing=self.ec.evaluate(symbol,rows,decision_now)
    if crossing and not self.signal_gate.reason(crossing.bar_end_ts,self.utc_now(),5):
     # STEP16Z observability only: distinguish detection from canonical E persistence.
     # No Early Core threshold, crossing rule, state semantics, or alert policy changes.
     stats["early_core_crossings_detected"]+=1
     try:
      _e_key,e_record=self.ew.persist_first_e(crossing)
     except Exception as exc:
      raise_if_leadership_lost(exc)
      stats["early_core_persist_errors"]+=1
      if stats["early_core_first_persist_error"] is None:
       stats["early_core_first_persist_error"]={"type":type(exc).__name__,"message":str(exc)[:300],"symbol":symbol}
      print({"stage":"SHADOW_E_PERSIST_ERROR","symbol":symbol,
             "error_type":type(exc).__name__,"error":str(exc)[:300]},flush=True)
     else:
      stats["crossings"]+=1
      stats["early_core_persisted"]+=1
      self._structure_watch_symbols.add(symbol)
      print({"stage":"SHADOW_FIRST_E","symbol":symbol,
             **self.liquidity_telemetry.first(symbol,"E",e_record.get("event_id")),
             "score":e_record.get("score"),
             "received_at":e_record.get("received_at"),"evaluation_utc":self.utc_now().isoformat(),
             "bar_end_ts":e_record.get("bar_end_ts"),
             "decision_available_ts":e_record.get("decision_available_ts")},flush=True)
     try:self._confluence(symbol,now)
     except ConfluenceEvaluationError as exc:
      self._confluence_retry.add(symbol)
      stats["confluence_errors"]+=1
      self.confluence_diagnostics.record(str(exc),"poll_native5")
   process_seconds=round(time.monotonic()-process_t0,3)
   self._memory_probe("native5_after_processing",batch_no=batch_no,chunk_symbols=len(chunk),fetched_rows=fetched_rows,process_seconds=process_seconds)
   del rows_by_symbol
   self._memory_probe("native5_after_del",batch_no=batch_no)
   # Full collection once per cycle, avoiding one stop-the-world sweep per chunk.
  gc.collect()
  self._memory_probe("native5_after_gc",batch_no=batch_no)
  stats["cycle_seconds"]=round(time.monotonic()-cycle_t0,3)
  self._memory_probe("native5_cycle_after",cycle_seconds=stats["cycle_seconds"],eligible_symbols=len(pending))
  return stats
 @synchronized
 def _confluence(self,symbol,now):
  self._confluence_retry.discard(symbol)
  now=self.utc_now()
  try:
   if self.signal_gate.window_reason(now):return
   if symbol in self._entry_opportunities:return
   existing=self._get(key_opportunity(self.session,symbol),"opportunity")
   if existing:
    if existing["state"]=="CONFLUENCE_VALID":self._entry_opportunities[symbol]=existing
    return
   e=self._get(key_early_core(self.session,symbol),"early_core");b=self._get(key_base_ready(self.session,symbol),"base_ready")
   rejected=self.signal_gate.pair_reason(e,b,now)
   if rejected:
    kind,reason,signal=rejected
    self.signal_gate.record(reason,symbol,kind,signal["bar_end_ts"],now);return
   # Writer/create contract remains (decision,key); cache only a validated record.
   _decision,key=self.cw.evaluate_and_persist(self.session,symbol,e,b,now)
   record=None if key is None else self.store.read(key,"opportunity")
   if record and record.get("state")=="OPPORTUNITY_EXPIRED":
    self.terminal_counts["OPPORTUNITY_EXPIRED"]+=1
    print({"stage":"OPPORTUNITY_STATE_FINAL","symbol":symbol,"state":"OPPORTUNITY_EXPIRED","utc":now.isoformat()},flush=True)
   if record and record.get("state")=="CONFLUENCE_VALID":
    self._entry_opportunities[symbol]=record
    print({"stage":"SHADOW_CONFLUENCE","symbol":symbol,
           "e_decision_available_ts":e.get("decision_available_ts") if e else None,
           "b_decision_available_ts":b.get("decision_available_ts") if b else None,
           "delta_seconds":record.get("delta_seconds"),
           "entry_trigger_ts":record.get("entry_trigger_ts")},flush=True)
  except Exception as exc:
   try:raise_if_leadership_lost(exc)
   except (LeaseLost,LeadershipError):
    self._confluence_lease_lost=True
    raise
   raise ConfluenceEvaluationError(type(exc).__name__) from None
 @synchronized
 def on_trade(self,symbol,msg,received_at,allow_decision):
  self.raw_trade_messages_received = getattr(self,"raw_trade_messages_received",0) + 1
  price=msg.get("p");ts=msg.get("t")
  if price is None or ts is None:return
  tr=SIPTrade(float(price),dt(ts),received_at,int(msg.get("_seq",0)),True)
  if allow_decision and not self.signal_gate.window_reason(received_at) and symbol in self._entry_opportunities:
   cutoff=received_at-timedelta(seconds=self.entry_trade_buffer_seconds)
   xs=self.trades.setdefault(symbol,[])
   trigger=dt(self._entry_opportunities[symbol]['entry_trigger_ts'])
   deadline=trigger+timedelta(seconds=ENTRY_PRICE_WAIT_SECONDS)
   eligible=[x for x in xs+[tr] if x.received_at>=cutoff and x.eligible and not x.price<=0 and trigger<=x.received_at<=min(received_at,deadline) and 0<=(x.received_at-x.trade_ts).total_seconds()<=MAX_ENTRY_TRADE_AGE_SECONDS]
   self.trades[symbol]=[max(eligible,key=lambda x:(x.received_at,x.sequence))] if eligible else []
   self._try_entry(symbol,received_at)
  else:
   self.trades.pop(symbol,None)
  self._monitor_trade(symbol,tr)
 @synchronized
 def on_status(self,msg):
  symbol=msg.get("S");code=str(msg.get("sc",""))
  if code in {"2","H","P"}:self.halted.add(symbol)
  elif code in {"3","Q","T"}:self.halted.discard(symbol)
 def _entry_exit(self,symbol,now,reason):
  self.entry_exit_counts[reason]+=1
  key=(symbol,reason);self.entry_exit_counts_by_symbol[key]+=1
  if self.entry_exit_counts_by_symbol[key]<=5:
   print({"stage":"ENTRY_DIAGNOSTIC","symbol":symbol,"reason":reason,"utc":now.isoformat(),"count":self.entry_exit_counts_by_symbol[key]},flush=True)
 def _terminal_opportunity(self,symbol,opp,now,state,reason):
  token=self.leadership.require_current()
  new=dict(opp);new.update(state=state,terminal_reason=reason,updated_at=now.isoformat(),leader_generation=token.leader_generation,worker_instance_id=token.worker_instance_id)
  validate_record(new,"opportunity")
  try:
   self.lua.atomic_opportunity_terminal(self.worker_id,token.leader_generation,key_opportunity(self.session,symbol),canonical_json(opp),canonical_json(new))
  except Exception:
   # Unknown commit outcome: no more entries until canonical refresh resolves it.
   self._entry_uncertain.add(symbol)
   self._entry_exit(symbol,now,"TERMINAL_UNCONFIRMED")
   raise
  self._entry_opportunities.pop(symbol,None);self.trades.pop(symbol,None)
  self.terminal_counts[state]+=1
  print({"stage":state,"symbol":symbol,"reason":reason,"utc":now.isoformat()},flush=True)
 @synchronized
 def _try_entry(self,symbol,now):
  try:return self._try_entry_checked(symbol,now)
  except Exception as exc:
   self._entry_exit(symbol,now,"EXCEPTION_"+type(exc).__name__)
   try:raise_if_leadership_lost(exc)
   except (LeaseLost,LeadershipError):
    self._confluence_lease_lost=True;raise
   return
 def _try_entry_checked(self,symbol,now):
  now=self.utc_now()
  opp=self._entry_opportunities.get(symbol)
  if not opp or opp["state"]!="CONFLUENCE_VALID":
   self._entry_exit(symbol,now,"NO_VALID_OPPORTUNITY");return
  if symbol in self._entry_uncertain:
   self._entry_exit(symbol,now,"TERMINAL_UNCONFIRMED");return
  if self.signal_gate.window_reason(now):
   self._entry_exit(symbol,now,"OUTSIDE_MARKET_HOURS");return
  trigger=dt(opp["entry_trigger_ts"])
  ed=evaluate_entry_price(trigger,now,self.trades.get(symbol,[]),symbol in self.halted)
  if ed.status!=EntryStatus.PRICE_READY:
   self._entry_exit(symbol,now,ed.status.value)
   if ed.status in {EntryStatus.EXPIRED,EntryStatus.HALT_FINAL}:
    self._terminal_opportunity(symbol,opp,now,"ENTRY_EXPIRED"if ed.status==EntryStatus.EXPIRED else"ENTRY_REJECTED",ed.status.value)
   return
  e=self._get(key_early_core(self.session,symbol),"early_core");b=self._get(key_base_ready(self.session,symbol),"base_ready")
  if not e or not b:
   self._entry_exit(symbol,now,"MISSING_SIGNAL_RECORD");return
  rejected=self.signal_gate.pair_reason(e,b,now)
  if rejected:
   kind,reason,signal=rejected
   self._entry_exit(symbol,now,reason)
   self._terminal_opportunity(symbol,opp,now,"ENTRY_EXPIRED",reason);return
  first=min(dt(e["decision_available_ts"]),dt(b["decision_available_ts"]))
  bars=[]
  for bar_ts,bar_low,recv in self.bars.get(symbol,[]):
   bs=dt(bar_ts);bars.append(StructureBar(bs,bs+timedelta(minutes=1),float(bar_low),recv))
  risk=evaluate_structural_risk(ed.entry_alert_price,first,trigger,bars)
  if risk.status!=RiskStatus.APPROVED:
   self._entry_exit(symbol,now,risk.status.value)
   self._terminal_opportunity(symbol,opp,now,"ENTRY_REJECTED",risk.status.value);return
  new,trade,out=EntryCommitBuilder(self.leadership).build(opp,risk,ed.entry_alert_price,now)
  new,trade,out=stamp(new),stamp(trade),stamp(out)
  self.lua.atomic_entry(self.worker_id,key_opportunity(self.session,symbol),canonical_json(opp),canonical_json(new),key_trade(trade["trade_id"]),canonical_json(trade),f"operational_priority_radar:v1:outbox:{out['event_id']}",canonical_json(out))
  self._entry_opportunities.pop(symbol,None);self.trades.pop(symbol,None);self.bars.pop(symbol,None)
  self._structure_watch_symbols.discard(symbol);self._active_by_symbol[symbol]=trade
  self.terminal_counts["ENTRY_COMMITTED"]+=1
  print({"stage":"ENTRY_COMMITTED","symbol":symbol,"utc":now.isoformat()},flush=True)
 @synchronized
 def expire_entry_opportunities(self,now):
  # Terminalization only; this timer never creates an entry, trade, or message.
  for symbol,opp in list(self._entry_opportunities.items()):
   if symbol in self._entry_uncertain:continue
   ed=evaluate_entry_price(dt(opp['entry_trigger_ts']),now,self.trades.get(symbol,[]),symbol in self.halted)
   if ed.status in {EntryStatus.EXPIRED,EntryStatus.HALT_FINAL}:
    self._entry_exit(symbol,now,ed.status.value)
    self._terminal_opportunity(symbol,opp,now,'ENTRY_EXPIRED'if ed.status==EntryStatus.EXPIRED else'ENTRY_REJECTED',ed.status.value)
 def _active(self,symbol):
  # Hot path: canonical active state is mirrored in memory after recovery/atomic commits.
  return self._active_by_symbol.get(symbol)
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
  if new["state"] in {"ACTIVE_PRE_T1","ACTIVE_POST_T1","HALTED_ACTIVE"}:self._active_by_symbol[t["symbol"]]=new
  else:self._active_by_symbol.pop(t["symbol"],None)
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
