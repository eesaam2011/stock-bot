import json,threading
from datetime import datetime,timedelta,timezone
from state_store import key_early_core,key_base_ready,key_opportunity,validate_record,SchemaError
from recovery_chronology import plan_native_batch
from recovery_signal_replay import reconstruct_window_signals
from recovery_session_replay import reconstruct_session_signals,EARLY_WARMUP_MINUTES
from recovery_session_preview import preview_session_overlap
from recovery_canonical_audit import audit_session_canonical
from recovery_chronology import _utc
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
 def __init__(self,reader,rest,trade_reconciler,session,symbols,now_fn=None,overlap_seconds=120,status_tracker=None,audit_window_signals=False,
              audit_session_signals=False,session_start=None,
              audit_session_overlap=False,sip_capture=None,sip_epoch=None,
              audit_session_canonical_records=False):
  self.reader=reader;self.rest=rest;self.trade_reconciler=trade_reconciler
  self.session=session;self.symbols=list(symbols);self.now_fn=now_fn or (lambda:datetime.now(UTC));self.overlap_seconds=overlap_seconds
  self.status_tracker=status_tracker;self.recovered_1m={};self.recovered_5m={};self.pending_halted={}
  self.audit_window_signals=bool(audit_window_signals)
  self.audit_session_signals=bool(audit_session_signals)
  self.session_start=session_start
  self.audit_session_overlap=bool(audit_session_overlap)
  self.sip_capture=sip_capture
  self.sip_epoch=sip_epoch
  self.audit_session_canonical_records=bool(audit_session_canonical_records)
  self._base_reconciled=False
  self._fetch_audit=None
  self.cancel_event=threading.Event()
  if self.trade_reconciler is not None:
   self.trade_reconciler.cancel_event=self.cancel_event
 def cancel(self):
  self.cancel_event.set()
 def _require_not_cancelled(self):
  if self.cancel_event.is_set():raise RecoveryFailure("RECOVERY_CANCELLED")
 def run(self):
  self._require_not_cancelled()
  self._base_reconciled=False
  self._fetch_audit=None
  self.pending_halted.clear()
  try:
   trades=self.reader.active_trades()
   # P0 first. HALTED_ACTIVE waits for authoritative SIP status after subscription.
   for t in trades:
    self._require_not_cancelled()
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
   if self.audit_session_signals:
    if self.session_start is None:
     raise RecoveryFailure("SESSION_AUDIT_REQUIRES_EXPLICIT_SESSION_START")
    session_start=_utc(self.session_start)
    if not session_start<now:
     raise RecoveryFailure("SESSION_AUDIT_INVALID_SESSION_START")
    # Native5 pre-session warm-up is part of the request, not a claim
    # that REST observed every market event during that interval.
    start=min(start,session_start-timedelta(minutes=EARLY_WARMUP_MINUTES))
   if self.audit_session_canonical_records and (not self.audit_session_overlap
       or not hasattr(self.reader,"get_e") or not hasattr(self.reader,"get_b")):
    raise RecoveryFailure("CANONICAL_AUDIT_REQUIRES_SESSION_OVERLAP_AND_READER")
   if self.audit_session_overlap:
    if (not self.audit_session_signals or self.sip_capture is None
        or not isinstance(self.sip_epoch,int) or self.sip_epoch<1
        or len(self.symbols)>80 or not self.symbols
        or not hasattr(self.rest,"native_recovery_batch_audited")):
     raise RecoveryFailure("SESSION_OVERLAP_REQUIRES_ONE_AUDITED_BATCH")
    snap=self.sip_capture.snapshot()
    if (snap["phase"]!=self.sip_capture.DRAINING
        or snap["epoch"]!=self.sip_epoch):
     raise RecoveryFailure("SESSION_OVERLAP_REQUIRES_EXTERNAL_DRAIN")
   batch_audits=[]
   overlap_audits=[]
   canonical_audits=[]
   session_audits=[]
   signal_audits=[]
   pagination_audits=[]
   if hasattr(self.rest,"native_recovery_batch"):
    # Plan/audit one bounded batch at a time; never retain a universe-wide
    # history. A plan is not canonical replay or proof of SIP continuity.
    # Full-session 1m+native5 history can exceed the 100k-event plan
    # at 200 symbols. Keep the opt-in audit batches bounded.
    batch_size=80 if self.audit_session_signals else 200
    total=len(self.symbols)
    recovered_1m_count=0
    recovered_5m_count=0
    for offset in range(0,total,batch_size):
     self._require_not_cancelled()
     batch=self.symbols[offset:offset+batch_size]
     if hasattr(self.rest,"native_recovery_batch_audited"):
      r1,r5,page_audit=self.rest.native_recovery_batch_audited(
          batch,start,now,batch_size=batch_size,max_workers=2)
      if not page_audit.get("both_api_page_chains_exhausted"):
       raise RecoveryFailure("REST_PAGINATION_UNPROVEN")
      pagination_audits.append(page_audit)
     else:
      r1,r5=self.rest.native_recovery_batch(batch,start,now,batch_size=batch_size,max_workers=2)
     self._require_not_cancelled()
     plan,audit=plan_native_batch(r1,r5,batch,window_start=start,
                                  window_end=now,recovered_at=now if self.audit_session_overlap else self.now_fn())
     self._require_not_cancelled()
     if self.audit_window_signals:
      _,signals_audit=reconstruct_window_signals(plan,self.session,recovered_at=plan[0].recovered_at if plan else self.now_fn())
      signal_audits.append(signals_audit)
     if self.audit_session_signals:
      if self.audit_session_overlap:
       overlap_signals,overlap_audit=preview_session_overlap(
           plan,self.sip_capture,session=self.session,epoch=self.sip_epoch,
           symbols=batch,session_start=session_start,session_end=now,
           requested_start=start,as_of=now)
       overlap_audits.append(overlap_audit)
       session_audit=overlap_audit["session_signals"]
       if self.audit_session_canonical_records:
        canonical_audit=audit_session_canonical(
            overlap_signals,self.reader,session=self.session,
            symbols=batch,as_of=now)
        canonical_audits.append(canonical_audit)
        if canonical_audit["divergent"]:
         raise RecoveryFailure("CANONICAL_SESSION_SIGNAL_DIVERGENCE")
      else:
       _,session_audit=reconstruct_session_signals(
           plan,self.session,session_start=session_start,session_end=now,
           requested_start=start,recovered_at=plan[0].recovered_at if plan else self.now_fn())
      session_audit["rest_pagination_proven"]=bool(pagination_audits and page_audit.get("both_api_page_chains_exhausted")) if hasattr(self.rest,"native_recovery_batch_audited") else False
      session_audits.append(session_audit)
     batch_audits.append(audit)
     batch_1m_symbols=sum(1 for sym in batch if r1.get(sym))
     batch_5m_symbols=sum(1 for sym in batch if r5.get(sym))
     recovered_1m_count += batch_1m_symbols
     recovered_5m_count += batch_5m_symbols
     del plan,r1,r5
     print({"stage":"STARTUP_RECOVERY_BATCH",
            "processed":min(offset+len(batch),total),"total":total,
            "batch_size":len(batch),
            "batch_1m_symbols":batch_1m_symbols,
            "batch_5m_symbols":batch_5m_symbols,
            "recovered_1m_symbols":recovered_1m_count,
            "recovered_5m_symbols":recovered_5m_count,
            "retained_recovery_bars":0},flush=True)
   else:
    # Deterministic adapter/test compatibility; keep rows ephemeral here too.
    for sym in self.symbols:
     self._require_not_cancelled()
     rows1=self.rest.native_1m_gap(sym,start,now)
     rows5=self.rest.native_5m(sym,start,now)
     plan,audit=plan_native_batch({sym:rows1},{sym:rows5},[sym],
                                  window_start=start,window_end=now,
                                  recovered_at=self.now_fn())
     self._require_not_cancelled()
     if self.audit_window_signals:
      _,signals_audit=reconstruct_window_signals(plan,self.session,recovered_at=plan[0].recovered_at if plan else self.now_fn())
      signal_audits.append(signals_audit)
     if self.audit_session_signals:
      _,session_audit=reconstruct_session_signals(
          plan,self.session,session_start=session_start,session_end=now,
          requested_start=start,recovered_at=plan[0].recovered_at if plan else self.now_fn())
      session_audits.append(session_audit)
     batch_audits.append(audit)
     del plan,rows1,rows5
   self._require_not_cancelled()
   # Completed native bars were normalized, deduplicated, ordered and
   # audited per batch, then discarded. E/B canonical replay, SIP merge and
   # independent end-to-end coverage proof are still missing. Fail closed.
   self._base_reconciled=False
   self._fetch_audit={"kind":"REST_CHRONOLOGY_AUDIT_ONLY",
                      "batch_count":len(batch_audits),
                      "native_1m":sum(a["native_1m"] for a in batch_audits),
                      "native_5m":sum(a["native_5m"] for a in batch_audits),
                      "observed_interbar_gaps":sum(a["observed_interbar_gaps"] for a in batch_audits),
                      "empty_symbol_timeframe_lanes":sum(a["empty_symbol_timeframe_lanes"] for a in batch_audits),
                      "batch_digests":[a["chronological_sha256"] for a in batch_audits],
                      "signal_audit_enabled":self.audit_window_signals,
                      "session_signal_audit_enabled":self.audit_session_signals,
                      "session_sip_overlap_audit_enabled":self.audit_session_overlap,
                      "session_canonical_comparison_enabled":self.audit_session_canonical_records,
                      "session_canonical_audited_batches":len(canonical_audits),
                      "session_canonical_matches":sum(len(a["matched"]) for a in canonical_audits),
                      "session_observed_without_canonical":sum(len(a["observed_without_canonical"]) for a in canonical_audits),
                      "session_canonical_without_observed":sum(len(a["canonical_without_observed"]) for a in canonical_audits),
                      "session_canonical_multi_key_snapshot_atomic":False,
                      "session_canonical_backfill_authorized":False,
                      "session_sip_overlap_audited_batches":len(overlap_audits),
                      "session_sip_overlap_equal_bars":sum(a["overlap"]["overlap_equal_1m_bars"] for a in overlap_audits),
                      "session_sip_overlap_new_bars":sum(a["overlap"]["unmatched_sip_1m_bars"] for a in overlap_audits),
                      "session_sip_trades_not_reconciled":sum(a["overlap"]["sip_trades"] for a in overlap_audits),
                      "session_sip_statuses_not_reconciled":sum(a["overlap"]["sip_statuses"] for a in overlap_audits),
                      "session_observed_E":sum(a["session_observed_E"] for a in session_audits),
                      "session_observed_B":sum(a["session_observed_B"] for a in session_audits),
                      "session_warmup_window_requested":bool(session_audits) and all(a["warmup_window_requested"] for a in session_audits),
                      "session_audited_batches":len(session_audits),
                      "window_local_E":sum(a["window_local_E"] for a in signal_audits),
                      "window_local_B":sum(a["window_local_B"] for a in signal_audits),
                      "rest_pagination_audited_batches":len(pagination_audits),
                      "rest_pagination_pages_1m":sum(a["native_1m"]["pages"] for a in pagination_audits),
                      "rest_pagination_pages_5m":sum(a["native_5m"]["pages"] for a in pagination_audits),
                      "api_page_chains_exhausted":bool(pagination_audits) and len(pagination_audits)==len(batch_audits),
                      "full_session_coverage_proven":False,
                      "first_of_session_proven":False,
                      "replay_completed":False,"continuity_proven":False}
   result={"gap_recovered":False,"reconciled":False,
           "reason":"CANONICAL_REPLAY_NOT_IMPLEMENTED","fetch_audit":self._fetch_audit}
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
 def continuity_verified(self,epoch):
  # Fetch-only REST recovery is never a SIP continuity proof.
  return False
 def on_disconnect(self):
  self._base_reconciled=False
  self._fetch_audit=None
  self.pending_halted.clear()
  if self.status_tracker is not None and hasattr(self.status_tracker,"reset"):
   self.status_tracker.reset()
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
