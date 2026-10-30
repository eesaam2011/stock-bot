"""Positive transport transition only after observed per-symbol evidence + case barrier.
No retrospective ENTRY, no hand-supplied continuity booleans.
"""
import threading,time
from datetime import timedelta
from dreview.core import _LIVE_ADAPTER_AUTHORITY
from dintegration.live import LiveAdapter
from dintegration.rest import instant
from .store import JournalState
class EvidenceAdapter(LiveAdapter):
 def __init__(self,*a,proof,barrier,wall,**kw):
  super().__init__(*a,**kw);self.checkpoint=JournalState(self.checkpoint);self.proof=proof;self.barrier=barrier;self.wall=wall
  self.control_lock=threading.RLock();self.quarantine={};self.report={};self.store=None;self.trusted_since=None;self.admission_reasons={};self.cancel_event=threading.Event();self.cancel_event.set();self.entry_floor=None;self.needs_changed=None;self.gap_start=None;self.missed_opportunities=set()
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
  if opportunity is not None and (self.trusted_since is None or instant(opportunity["entry_trigger_ts"])<self.trusted_since):
   self.admission_reasons[s]="OPPORTUNITY_MISSED_DURING_DATA_GAP";return False
  return True
 def connection_started(self):
  if self.store is not None:
   self.store.load_archive();self.barrier.persisted_R.update(self.store.pending_R);self.bridge.p._entry_uncertain.update(self.store.pending_U)
   if self.proof.archived:
    self.barrier.persisted_R.update(self.proof.archived.get('invalid',{}))
  with self.control_lock:
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
 def recover(self,now):
  self.close_eligibility('RECOVERY_PENDING');start=time.monotonic()
  try:
   with self.control_lock:
    self.cancel_event=threading.Event();token=self.cancel_event
    self.checks()
    if not self.authenticated or self.c.failed or self.c.inflight or self.c.bar!=self.c.desired_bar or self.c.trade!=self.c.desired_trade:raise ValueError('ACK_AUTH_INCOMPLETE')
    connection,revision=self.c.connection,self.c.revision;symbols=set(self.c.bar)
    required_pins=set(self.c.needs.trades) if self.c.needs else set()
    if self.c.needs:self.barrier.persisted_R.update(self.c.needs.R)
    uncertain=set(self.c.needs.U) if self.c.needs else set()
    self.bridge.p._entry_uncertain.update(uncertain)
   one,five=self.rest.warmup(symbols,now,self.bridge.p.ec.required_history_minutes())
   # Canonical case reads and the original expiration timer are serialized with ingress.
   def epoch_guard():
    self.checks()
    if token is not self.cancel_event or token.is_set():raise ValueError('RECOVERY_CANCELLED')
    if connection!=self.c.connection or revision!=self.c.revision or self.c.inflight or self.c.failed:raise ValueError('RECOVERY_EPOCH_CHANGED')
   self.barrier.epoch_guard=epoch_guard
   with self.bridge.p._decision_lock:
    cases=self.barrier.run(now,self.gap_start or self.proof.started)
   if not cases['reconciled']:
    self.report={'reason':'OPEN_CASE_CONTINUITY_UNPROVEN','cases':cases};return False
   self.missed_opportunities.update(tuple(x['opportunity_identity']) for x in cases.get('results',[]) if x.get('result')=='OPPORTUNITY_MISSED_DURING_DATA_GAP')
   for s in cases.get('resolved_R',[]):self.proof.rearm_symbol(s)
   if self.store is not None:self.store.resolve(cases.get('resolved_R',[]),cases.get('resolved_U',[]))
   required_pins=set().union(*(set(cases['inventory'][k]) for k in ('O','A','U','R')))
   good={};bad={}
   for s in sorted(symbols):
    if not one.get(s) or not five.get(s):bad[s]='NATIVE_HISTORY_UNAVAILABLE';continue
    if any(instant(b['t'])+timedelta(minutes=1)>now for b in one[s]):bad[s]='INCOMPLETE_NATIVE_1MIN';continue
    if any(b.get('_timeframe')!='native_5Min' or instant(b['t'])+timedelta(minutes=5)>now for b in five[s]):bad[s]='INCOMPLETE_OR_NON_NATIVE_5MIN';continue
    e=self.proof.evidence(s,one[s],now)
    if e['proven']:good[s]=e
    else:bad[s]=e['reason']
   if required_pins-set(good):self.report={'reason':'PINNED_SYMBOL_UNPROVEN','symbols':sorted(required_pins-set(good))};return False
   if not good:self.report={'reason':'NO_SYMBOL_CONTINUITY_PROVEN','quarantine':bad};return False
   with self.bridge.p._decision_lock,self.control_lock:
    self.checks()
    if token is not self.cancel_event or token.is_set():raise ValueError('RECOVERY_CANCELLED')
    if connection!=self.c.connection or revision!=self.c.revision or symbols!=self.c.bar or self.c.inflight or self.c.failed:raise ValueError('RECOVERY_EPOCH_CHANGED')
    # No historical event is fed into on_trade/on_bar as a fresh decision event.
    self.bridge.preload({s:one[s] for s in good})
    for s in symbols:
     self.c.attest(s,history=s in good,continuity=s in good,reconciled=s in good,first_trade=False)
    self.quarantine=bad;self.report={'reason':'BAR_TRANSPORT_EVIDENCE_READY','good':good,'quarantine':bad,'cases':cases,'TRADE_completeness':False}
    # Durable evidence first, then in-memory eligibility. Redis restore always starts False.
    self.checkpoint.append_epoch({'kind':'B_EVIDENCE_READY','connection':connection,'revision':revision,'proof':self.proof.snapshot(),'report':self.report,'transport_trusted':False})
    if token is not self.cancel_event or token.is_set():raise ValueError('RECOVERY_CANCELLED')
    self.entry_floor=self.entry_floor or self.wall();self.trusted_since=self.entry_floor;self.gap_start=None;self._LiveAdapter__trusted=True;self.c._set_transport_from_adapter(True,_LIVE_ADAPTER_AUTHORITY)
    self.recovering=set(bad);self.last=now;return True
  except Exception as exc:
   self.report={'reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__};return False
  finally:
   self.measure.observe('recovery',time.monotonic()-start)
   if not self.transport_trusted:
    self.close_eligibility('RECOVERY_EVIDENCE_INCOMPLETE')
    # A persistence failure propagates to scheduler/supervisor. Never swallow Redis outage.
    self.checkpoint.append_epoch({'kind':'B_RECOVERY_FAILED','proof':self.proof.snapshot(),'report':self.report})
