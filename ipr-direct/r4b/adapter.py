from dintegration.timing import timed
"""Positive transport transition only after observed per-symbol evidence + case barrier.
No retrospective ENTRY, no hand-supplied continuity booleans.
"""
import threading,time,json
from r9core.chain import EvidenceRecordTooLarge,ChainFailure
from datetime import timedelta
from dreview.core import _LIVE_ADAPTER_AUTHORITY
from dintegration.live import LiveAdapter
from dintegration.rest import instant
from .store import JournalState
from dintegration.clock import ClockUnavailable
from dreview.core import Blocked
from r4a.decision_metrics import operation
from ipr.observability import observed_phase
class EvidenceAdapter(LiveAdapter):
 INCREMENTAL_BATCH_CAP=100
 INCREMENTAL_REJECT_COOLDOWN=30.0
 def __init__(self,*a,proof,barrier,wall,**kw):
  super().__init__(*a,**kw);self.checkpoint=JournalState(self.checkpoint);self.proof=proof;self.barrier=barrier;self.wall=wall
  self.control_lock=threading.RLock();self.quarantine={};self.report={};self.store=None;self.trusted_since=None;self.admission_reasons={};self.cancel_event=threading.Event();self.cancel_event.set();self.entry_floor=None;self.needs_changed=None;self.gap_start=None;self.missed_opportunities=set();self.symbol_admitted_at={};self.rejection_until={};self.incremental_clock=time.monotonic
  self.bridge.recovery_control_lock=self.control_lock
  self.barrier.status_source=getattr(self.recovery,'original',None);self.status_changed=None
  self.original_filter=self.filter_check;self.filter_check=self._admissible_trade
 @property
 def transport_trusted(self):
  return super().transport_trusted and not self.cancel_event.is_set()
 def close_eligibility(self,reason):
  if hasattr(self,'wall') and (self.transport_trusted or self.gap_start is None):self.gap_start=self.wall()
  if hasattr(self,"cancel_event"):self.cancel_event.set()
  self.trusted_since=None
  if reason not in {'RECOVERY_PENDING','SUBSCRIPTION_CHANGE_REQUIRES_RECOVERY','NEEDS_DURING_PENDING_CONTROL'}:self.entry_floor=None
  return super().close_eligibility(reason)
 def _admissible_trade(self,s,msg,received):
  if not self.original_filter(s,msg,received):return False
  opportunity=self.bridge.p._entry_opportunities.get(s)
  if opportunity is not None and tuple([opportunity['session'],s,opportunity['entry_trigger_ts']]) in self.missed_opportunities:
   self.admission_reasons[s]='OPPORTUNITY_MISSED_DURING_DATA_GAP';return False
  floor=self.trusted_since
  if floor is not None:floor=max(floor,self.symbol_admitted_at.get(s,floor))
  if opportunity is not None and (floor is None or instant(opportunity["entry_trigger_ts"])<floor):
   self.admission_reasons[s]="OPPORTUNITY_MISSED_DURING_DATA_GAP";return False
  return True
 @timed('connection_started')
 def connection_started(self):
  if self.store is not None:
   self.store.load_archive();self.barrier.persisted_R.update(self.store.pending_R);self.bridge.p._entry_uncertain.update(self.store.pending_U)
   if self.proof.archived:
    self.barrier.persisted_R.update(self.proof.archived.get('invalid',{}))
  inv=self.barrier.restore_pins()
  pins=set().union(*inv.values())
  super().connection_started()
  if getattr(self,'prepare_needs',None) is not None:self.prepare_needs()
  if not pins<=self.c.desired_bar:raise ValueError('CANONICAL_PINS_REQUIRE_SELECTOR_REPLAN')
  self.proof.connected(started=self.wall());self.proof.set_scope(set());self.quarantine={}
 def on_message(self,msg,received):
  if msg.get('T') in {'b','t'}:
   with self.control_lock:
    s=msg.get('S');p=self.c.proofs.get(s,{})
    if not all(p.get(k) for k in ('history','continuity','reconciled')):return False
    if p.get('connection')!=self.c.connection:return False
    before=self._case_shape()
    result=super().on_message(msg,received)
    if self.needs_changed is not None and before!=self._case_shape():self.needs_changed()
    return result
  result=super().on_message(msg,received)
  if msg.get('T')=='s' and self.status_changed is not None:self.status_changed()
  return result
 def _case_shape(self):
  p=self.bridge.p
  return (frozenset(p._entry_opportunities),frozenset(p._active_by_symbol),frozenset(p._entry_uncertain),frozenset(self.barrier.persisted_R),frozenset(p._structure_watch_symbols))
 def _bar_evidence(self,s,one,five,now):
  if not one.get(s) or not five.get(s):return {'proven':False,'reason':'NATIVE_HISTORY_UNAVAILABLE'}
  if any(instant(b['t'])+timedelta(minutes=1)>now for b in one[s]):return {'proven':False,'reason':'INCOMPLETE_NATIVE_1MIN'}
  if any(b.get('_timeframe')!='native_5Min' or instant(b['t'])+timedelta(minutes=5)>now for b in five[s]):return {'proven':False,'reason':'INCOMPLETE_OR_NON_NATIVE_5MIN'}
  return self.proof.evidence(s,one[s],now)
 def recover(self,now):
  from ipr.review_flags import RECOVERY_IO_OUTSIDE_LOCK_ENABLED
  if RECOVERY_IO_OUTSIDE_LOCK_ENABLED:
   from .recovery_io import recover
   return recover(self,now)
  self.close_eligibility('RECOVERY_PENDING');start=time.monotonic()
  try:
   with self.control_lock:
    self.cancel_event=threading.Event();token=self.cancel_event
   self.checks()
   with self.control_lock:
    if not self.authenticated or self.c.failed or self.c.inflight or self.c.bar!=self.c.desired_bar or self.c.trade!=self.c.desired_trade:raise ValueError('ACK_AUTH_INCOMPLETE')
    connection,revision=self.c.connection,self.c.revision;symbols=set(self.c.bar)
    required_pins=set(self.c.needs.trades) if self.c.needs else set()
    if self.c.needs:self.barrier.persisted_R.update(self.c.needs.R)
    uncertain=set(self.c.needs.U) if self.c.needs else set()
    self.bridge.p._entry_uncertain.update(uncertain)
   warm_symbols=(symbols & self.proof.witnessed(now)) | (symbols & required_pins)
   warm_start=time.monotonic()
   try:
    with observed_phase('recover.fetch'):one,five=self.rest.warmup(warm_symbols,now,self.bridge.p.ec.required_history_minutes())
   finally:
    try:print(json.dumps({'stage':'RECOVERY_WARMUP','symbols':len(warm_symbols),'bar_scope':len(symbols),'native1_minutes':60,'native5_minutes':self.bridge.p.ec.required_history_minutes(),'seconds':time.monotonic()-warm_start}),flush=True)
    except Exception:pass
   # Canonical case reads and the original expiration timer are serialized with ingress.
   def epoch_guard():
    self.checks()
    if token is not self.cancel_event or token.is_set():raise ValueError('RECOVERY_CANCELLED')
    if connection!=self.c.connection or revision!=self.c.revision or self.c.inflight or self.c.failed:raise ValueError('RECOVERY_EPOCH_CHANGED')
   self.barrier.epoch_guard=epoch_guard
   with operation(self.bridge.p._decision_lock,'case_recovery'),self.bridge.p._decision_lock:
    cases=self.barrier.run(now,self.gap_start or self.proof.started)
   if not cases['reconciled']:
    self.report={'reason':'OPEN_CASE_CONTINUITY_UNPROVEN','cases':cases};return False
   self.missed_opportunities.update(tuple(x['opportunity_identity']) for x in cases.get('results',[]) if x.get('result')=='OPPORTUNITY_MISSED_DURING_DATA_GAP')
   for s in cases.get('resolved_R',[]):self.proof.rearm_symbol(s)
   if self.store is not None:self.store.resolve(cases.get('resolved_R',[]),cases.get('resolved_U',[]))
   required_pins=set().union(*(set(cases['inventory'][k]) for k in ('O','A','U','R')))
   good={};bad={}
   with observed_phase('recover.evidence'):
    for i,s in enumerate(sorted(symbols)):
     if i%16==0:time.sleep(0)
     if s not in warm_symbols:bad[s]='NO_COMPLETED_LIVE_BAR_WITNESS';continue
     e=self._bar_evidence(s,one,five,now)
     if e['proven']:good[s]=e
     else:bad[s]=e['reason']
   if required_pins-set(good):self.report={'reason':'PINNED_SYMBOL_UNPROVEN','symbols':sorted(required_pins-set(good))};return False
   if not good:self.report={'reason':'NO_SYMBOL_CONTINUITY_PROVEN','quarantine':bad};return False
   def current():
    return (token is self.cancel_event and not token.is_set() and connection==self.c.connection
      and revision==self.c.revision and symbols==self.c.bar and not self.c.inflight and not self.c.failed)
   if not current():raise ValueError('RECOVERY_EPOCH_CHANGED')
   self.checks()
   report={'reason':'BAR_TRANSPORT_EVIDENCE_READY','good':good,'quarantine':bad,'cases':cases,'TRADE_completeness':False}
   with observed_phase('recover.persist'):
    self.checkpoint.append_epoch({'kind':'B_EVIDENCE_READY','connection':connection,'revision':revision,'proof':self.proof.snapshot(),'report':report,'transport_trusted':False})
   self.checks()
   if not current():raise ValueError('RECOVERY_EPOCH_CHANGED')
   with observed_phase('recover.preload'),self.bridge.staged_preload() as staged_history:
    self.bridge.preload({s:one[s] for s in good})
   with observed_phase('recover.attest'),self.c.stage_evidence() as staged_proofs:
    for i,s in enumerate(sorted(symbols)):
     self.c.attest(s,history=s in good,continuity=s in good,reconciled=s in good,first_trade=False)
     if i%16==0:time.sleep(0) # yield GIL between bounded chunks; not a trust shortcut
   self.checks()
   # Only pointer/dict publication is serialized; no preload, attest or Redis here.
   with observed_phase('recover.commit_lock_wait'):
    self.bridge.p._decision_lock.acquire()
   try:
    with self.control_lock,observed_phase('recover.commit'):
     if not current():raise ValueError('RECOVERY_EPOCH_CHANGED')
     if not self.clock_check():raise ClockUnavailable('ROOT_OR_CLOCK_UNPROVEN')
     self.bridge.publish_preload(staged_history);self.c.proofs.update(staged_proofs)
     self.c._log('evidence_batch',symbols=len(staged_proofs))
     self.quarantine=bad;self.report=report
     self.entry_floor=self.entry_floor or self.wall();self.trusted_since=self.entry_floor;self.gap_start=None
     self._LiveAdapter__trusted=True;self.c._set_transport_from_adapter(True,_LIVE_ADAPTER_AUTHORITY)
     self.recovering=set(bad);self._invalidation_open=False;self.last=now;return True
   finally:self.bridge.p._decision_lock.release()

  except ChainFailure:
   self.close_eligibility('CHAIN2_FAILURE');raise
  except Exception as exc:
   self.report={'reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__};return False
  finally:
   self.bridge.discard_preload_stage()
   self.measure.observe('recovery',time.monotonic()-start)
   import sys
   if not self.transport_trusted and not isinstance(sys.exc_info()[1],ChainFailure):
    self.close_eligibility('RECOVERY_EVIDENCE_INCOMPLETE')
    # A persistence failure propagates to scheduler/supervisor. Never swallow Redis outage.
    self.checkpoint.append_epoch({'kind':'B_RECOVERY_FAILED','proof':self.proof.snapshot(),'report':self.report})

 def admit_incremental(self,candidates,now):
  start=time.monotonic();fetch_seconds=0.;selected=set(candidates);accepted={};bad={};fatal=None;clock_deferred=False;cooldown_symbols=0
  if len(selected)>self.INCREMENTAL_BATCH_CAP:raise ValueError('INCREMENTAL_BATCH_CAP')
  def current():
   return (self.transport_trusted and token is self.cancel_event and not token.is_set()
     and connection==self.c.connection and revision==self.c.revision and generation==self.proof.generation
     and self.authenticated and not self.c.failed and not self.c.inflight
     and self.c.bar==self.c.desired_bar and self.c.trade==self.c.desired_trade)
  try:
   with self.control_lock:
    token=self.cancel_event;connection=self.c.connection;revision=self.c.revision;generation=self.proof.generation
    if not current():bad={s:'INCREMENTAL_CONTEXT_CHANGED' for s in selected};return False
   self.checks()
   with self.control_lock:
    selected &= self.recovering & self.c.bar
    cooling={s for s in selected if self.rejection_until.get(s,0)>self.incremental_clock()}
    cooldown_symbols=sum(self.rejection_until.get(s,0)>self.incremental_clock() for s in self.recovering)
    selected-=cooling
   if not selected:return True
   fetch_start=time.monotonic()
   try:
    with observed_phase('incremental.fetch'):one,five=self.rest.warmup(selected,now,self.bridge.p.ec.required_history_minutes())
   except Exception as exc:
    bad={s:'WARMUP_'+type(exc).__name__ for s in selected};return False
   finally:fetch_seconds=time.monotonic()-fetch_start
   if not current():bad={s:'INCREMENTAL_CONTEXT_CHANGED' for s in selected};return False
   self.checks()
   with self.control_lock:inv=self.barrier.inventory();pins=set().union(*inv.values())
   good={}
   with observed_phase('incremental.evidence'):
    for s in sorted(selected):
     if s in pins:bad[s]='REQUIRES_FULL_CASE_RECOVERY';continue
     e=self._bar_evidence(s,one,five,now)
     if e['proven']:good[s]=e
     else:bad[s]=e['reason']
   if good:
    if not current():bad={s:'INCREMENTAL_CONTEXT_CHANGED' for s in selected};return False
    self.checks()
    with observed_phase('incremental.persist'):
     self.checkpoint.append_epoch({'kind':'B_INCREMENTAL_ADMISSION','connection':connection,'revision':revision,
       'accepted_symbols':sorted(good),'evidence':good,'proof':self.proof.snapshot(),'transport_trusted':False})
    if not current():bad={s:'INCREMENTAL_CONTEXT_CHANGED' for s in selected};return False
    self.bridge.leadership.require_current()
    if not self.root_check():raise Blocked('ROOT_OR_CLOCK_UNPROVEN')
    with observed_phase('incremental.preload'),self.bridge.staged_preload() as staged_history:
     self.bridge.preload({s:one[s] for s in good})
    floor=self.wall()
    with observed_phase('incremental.attest'),self.c.stage_evidence() as staged_proofs:
     for i,s in enumerate(good):
      self.c.attest(s,history=True,continuity=True,reconciled=True,first_trade=False)
      if i%16==0:time.sleep(0)
    self.checks()
    with observed_phase('incremental.commit_lock_wait'):
     self.bridge.p._decision_lock.acquire()
    try:
     with self.control_lock,observed_phase('incremental.commit'):
      if not current():bad={s:'INCREMENTAL_CONTEXT_CHANGED' for s in selected};return False
      if not self.clock_check():raise ClockUnavailable('ROOT_OR_CLOCK_UNPROVEN')
      self.bridge.publish_preload(staged_history);self.c.proofs.update(staged_proofs)
      self.c._log('evidence_batch',symbols=len(staged_proofs))
      self.symbol_admitted_at.update({s:floor for s in good})
      self.recovering.difference_update(good);accepted=good
      for s in good:self.rejection_until.pop(s,None)
    finally:self.bridge.p._decision_lock.release()
   with self.control_lock:
    self.quarantine.update(bad)
    for s in accepted:self.quarantine.pop(s,None)
    report_good=dict(self.report.get('good',{}));report_good.update(accepted)
    self.report={**self.report,'good':report_good,'quarantine':dict(self.quarantine)}
   return bool(accepted)

  except ClockUnavailable:
   clock_deferred=True
   print(json.dumps({'stage':'INCREMENTAL_ADMISSION_DEFERRED','reason':'CLOCK_UNAVAILABLE','symbols':len(selected)}),flush=True)
   return False
  except Exception as exc:
   # Leadership/root/persistence errors retain the original fatal/fail-closed boundary.
   self.close_eligibility('INCREMENTAL_ADMISSION_FATAL');fatal=type(exc).__name__;raise
  finally:
   self.bridge.discard_preload_stage()
   if fatal:bad.update({s:'FATAL_'+fatal for s in selected if s not in accepted})
   if not clock_deferred:
    for s in bad:self.rejection_until[s]=self.incremental_clock()+self.INCREMENTAL_REJECT_COOLDOWN
   reasons={}
   for why in bad.values():reasons[why]=reasons.get(why,0)+1
   reasons['REJECTION_COOLDOWN']=cooldown_symbols
   try:print(json.dumps({'stage':'INCREMENTAL_ADMISSION','at':self.wall().isoformat(),'requested':len(selected),
     'accepted':len(accepted),'rejected':len(bad),'rejection_reasons':reasons,'fetch_seconds':fetch_seconds,
     'seconds':time.monotonic()-start,'transport_trusted':self.transport_trusted,'TRADE_completeness':'UNKNOWN'}),flush=True)
   except Exception:pass
