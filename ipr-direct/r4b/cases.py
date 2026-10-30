"""Original frozen recovery routing; failures retain cases/pins, never invent outcomes."""
import json
from gap_recovery import reconcile_opportunity,active_trade_recovery_order
from production_recovery import ProductionGapRecovery,RedisCanonicalReader
from active_trade_recovery import TERMINAL,parse_ts
from state_store import key_trade,key_outbox,validate_record
from event_ids import entry_event_id,trade_event_id
from .original_recovery import reconcile_trade,RecoveryUnavailable
ACTIVE={'ACTIVE_PRE_T1','ACTIVE_POST_T1','HALTED_ACTIVE'}
class CaseBarrier:
 def __init__(self,bridge,proof):
  self.bridge=bridge;self.proof=proof;self.last=[];self.persisted_R=set();self.last_resolved_R=set();self.results=[];self.status_source=None
  self.epoch_guard=lambda: self.bridge.leadership.require_current()
 def records(self):
  return (self.bridge.records('operational_priority_radar:v1:trade:*','trade'),self.bridge.records('operational_priority_radar:v1:opportunity:*','opportunity'))
 def restore_pins(self):
  p=self.bridge.p;old_U=set(p._entry_uncertain);trades,opps=self.records()
  active=[r for r in trades if r['state'] in ACTIVE]
  # Reconcile every canonical trade before deciding whether the original one-per-
  # symbol hot cache can resume. Never silently discard a canonical trade.
  p._active_by_symbol={r['symbol']:r for r in active};p._entry_opportunities={r['symbol']:r for r in opps if r['state']=='CONFLUENCE_VALID'}
  p._entry_uncertain=old_U;p._structure_watch_symbols.update(set(p._active_by_symbol)|set(p._entry_opportunities)|old_U|self.persisted_R)
  return self.inventory()
 def inventory(self):
  p=self.bridge.p
  return {'O':set(p._entry_opportunities),'A':set(p._active_by_symbol),'U':set(p._entry_uncertain),'R':set(self.persisted_R)|set(getattr(self.bridge,'recovery_symbols',set()))}
 def _uncertain(self,s,opps,trades):
  os=[r for r in opps if r['symbol']==s and r['session']==self.bridge.p.session]
  if len(os)!=1:raise RecoveryUnavailable('U_CANONICAL_OPPORTUNITY_MISSING_OR_MULTIPLE')
  o=os[0]
  if o['state']=='ENTRY_COMMITTED':
   if not o.get('trade_id') or not o.get('entry_event_id'):raise RecoveryUnavailable('U_ATOMIC_IDENTITIES_MISSING')
   t=next((x for x in trades if x['trade_id']==o['trade_id']),None);raw=self.bridge.r.get(key_outbox(o['entry_event_id']))
   if t is None or raw is None:raise RecoveryUnavailable('U_ATOMIC_ENTRY_TRIPLE_INCOMPLETE')
   out=json.loads(raw);validate_record(out,'outbox')
   if out['event_type']!='ENTRY' or out['payload'].get('trade_id')!=t['trade_id'] or (t['session'],t['symbol'])!=(o['session'],o['symbol']):raise RecoveryUnavailable('U_ATOMIC_ENTRY_TRIPLE_CONFLICT')
   return 'ENTRY_ATOMIC_TRIPLE_CONFIRMED'
  if o['state'] not in {'CONFLUENCE_VALID','ENTRY_REJECTED','ENTRY_EXPIRED'}:raise RecoveryUnavailable('U_CANONICAL_STATE_UNSUPPORTED')
  identity=o.get('event_id') or f"{o['session']}:{s}:{o['entry_trigger_ts']}";tid=f"TRADE:{o['session']}:{s}:{identity}"
  if self.bridge.r.get(key_trade(tid)) is not None or self.bridge.r.get(key_outbox(entry_event_id(o['session'],s,identity))) is not None:raise RecoveryUnavailable('U_ATOMIC_ENTRY_TRIPLE_CONFLICT')
  return 'NO_ENTRY_COMMIT_IN_CANONICAL_OPPORTUNITY'
 def _expire_original(self,opps,now):
  # Route each canonical opportunity under its own session key. The ORIGINAL
  # timer and atomic terminal transition decide everything; no rule is copied.
  p=self.bridge.p
  with p._decision_lock:
   session,cache=p.session,p._entry_opportunities
   try:
    for o in opps:
     if o['state']!='CONFLUENCE_VALID':continue
     self.epoch_guard();p.session=o['session'];p._entry_opportunities={o['symbol']:o}
     p.expire_entry_opportunities(now)
   finally:p.session=session;p._entry_opportunities=cache
 def run(self,now,connection_started):
  self.epoch_guard();p=self.bridge.p;self.restore_pins();inv=self.inventory();issues=[];results=[];resolved_R=set();resolved_U=set()
  trades,opps=self.records();reader=RedisCanonicalReader(self.bridge.r)
  # Frozen active-trade priority, including active cases from an earlier session.
  active=[r for r in trades if r['state'] in ACTIVE]
  work=active_trade_recovery_order([{'active_trade':True,'record':r} for r in active])
  failed=set()
  for item in work:
   t=item['record'];s=t['symbol']
   try:
    r=reconcile_trade(self.bridge,t,now,self.epoch_guard,self.status_source)
    if r is False or r.get('ambiguous'):raise RecoveryUnavailable(r.get('reason','ACTIVE_RECOVERY_FAILED'))
    results.append({'symbol':s,'kind':'A','result':r['state'],'legal_ambiguity_final':r.get('legal_ambiguity_final',False)})
   except Exception as exc:failed.add(s);issues.append({'symbol':s,'reason':str(exc) if isinstance(exc,RecoveryUnavailable) or str(exc) in {'RECOVERY_EPOCH_CHANGED','RECOVERY_CANCELLED'} else type(exc).__name__})
  # Resolve U only from canonical atomic facts. A recovery failures still retain U.
  fresh_trades,fresh_opps=self.records()
  for s in sorted(inv['U']):
   try:
    self.epoch_guard();reason=self._uncertain(s,fresh_opps,fresh_trades)
    if s in failed:raise RecoveryUnavailable('U_ACTIVE_RECOVERY_NOT_FINISHED')
    resolved_U.add(s);results.append({'symbol':s,'kind':'U','result':reason})
   except Exception as exc:failed.add(s);issues.append({'symbol':s,'reason':str(exc) if isinstance(exc,RecoveryUnavailable) or str(exc) in {'RECOVERY_EPOCH_CHANGED','RECOVERY_CANCELLED'} else type(exc).__name__})
  # Original E/B gap policy is applied to opportunities; never creates retrospective Entry.
  for o in fresh_opps:
   if o['state']!='CONFLUENCE_VALID':continue
   s=o['symbol']
   try:
    self.epoch_guard();e=reader.get_e(o['session'],s);b=reader.get_b(o['session'],s)
    outcome=reconcile_opportunity(None if e is None else parse_ts(e['decision_available_ts']),None if b is None else parse_ts(b['decision_available_ts']),connection_started or now,now)
    results.append({'symbol':s,'kind':'O','result':outcome.status,'new_entry_allowed':outcome.new_entry_allowed,'opportunity_identity':[o['session'],s,o['entry_trigger_ts']]})
   except Exception as exc:failed.add(s);issues.append({'symbol':s,'reason':type(exc).__name__})
  # Expiry/halting of O is the original timer only. Do not clear an unresolved U to force it.
  p._entry_uncertain.difference_update(resolved_U-failed)
  if p._entry_opportunities:
   try:self._expire_original(fresh_opps,now)
   except Exception as exc:issues.append({'reason':type(exc).__name__});failed.update(p._entry_opportunities)
  for s in sorted(inv['R']|set(self.proof.invalid)):
   if s in failed:continue
   try:
    self.epoch_guard();r=ProductionGapRecovery(self.bridge.rest).recover(s,connection_started or now,now)
    if r['status']!='RECOVERED' or r['rows5'] is None:raise RecoveryUnavailable('R_NATIVE_RECOVERY_UNAVAILABLE')
    self.epoch_guard();resolved_R.add(s);results.append({'symbol':s,'kind':'R','result':'RECOVERED'})
   except Exception as exc:issues.append({'symbol':s,'reason':str(exc) if isinstance(exc,RecoveryUnavailable) else type(exc).__name__});failed.add(s)
  # Preserve data on failure; successful original transitions may already be committed.
  self.persisted_R.update(failed)
  p._entry_uncertain.difference_update(resolved_U-failed);self.persisted_R.difference_update(resolved_R-failed)
  if hasattr(self.bridge,'recovery_symbols'):self.bridge.recovery_symbols.difference_update(resolved_R-failed)
  active_after=[x for x in self.records()[0] if x['state'] in ACTIVE]
  for s in {x['symbol'] for x in active_after}:
   if sum(x['symbol']==s for x in active_after)>1:
    issues.append({'symbol':s,'reason':'MULTIPLE_ACTIVE_CASES_UNSUPPORTED_BY_ORIGINAL_HOT_CACHE'});self.persisted_R.add(s)
  self.restore_pins();self.last=issues;self.results=results;self.last_resolved_R=resolved_R-failed
  return {'gap_recovered':not issues,'reconciled':not issues,'issues':issues,'results':results,'resolved_R':sorted(self.last_resolved_R),'resolved_U':sorted(resolved_U-failed),'inventory':{k:sorted(v) for k,v in self.inventory().items()},'TRADE_completeness':False,'policy':'UNCHANGED_FROZEN_RECOVERY'}
