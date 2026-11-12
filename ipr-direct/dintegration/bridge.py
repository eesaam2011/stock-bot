"""Caller-side integration with byte-identical original decision modules."""
from pathlib import Path
from datetime import datetime,timezone
import sys,json,threading
from contextlib import contextmanager
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'reference_app'))
from production_pipeline import ProductionDecisionPipeline
from production_adapters import OperationalEarlyCore,OperationalBaseReady
from signal_admission import SignalAdmission
from state_store import validate_record
from dreview.core import Needs
class OriginalBridge:
 def __init__(self,r,lua,leadership,session,root_symbols,rest,worker,session_policy):
  self.r=r;self.leadership=leadership;self.rest=rest;self.needs_revision=0;self._preload_local=threading.local()
  self.p=ProductionDecisionPipeline(r,lua,leadership,session,sorted(root_symbols),OperationalEarlyCore(rest),OperationalBaseReady(),rest,worker,shadow=False)
  self.p.signal_gate=SignalAdmission(session,session_policy)
  from .native_timing import attach_native_timing
  attach_native_timing(self,rest,session_policy)
 def records(self,pattern,typ):return self.p._scan_records(pattern,typ)
 def recover_caches(self):
  self.p.refresh_runtime_caches()
  # Representation-only carry of canonical prior-session active cases; no new transition.
  for r in self.records('operational_priority_radar:v1:trade:*','trade'):
   if r['state'] in {'ACTIVE_PRE_T1','ACTIVE_POST_T1','HALTED_ACTIVE'}:
    self.p._active_by_symbol[r['symbol']]=r;self.p._structure_watch_symbols.add(r['symbol'])
 def needs(self,recovery_symbols=()):
  self.leadership.require_current();self.needs_revision+=1
  return Needs(self.needs_revision,bar=frozenset(),O=frozenset(self.p._entry_opportunities),A=frozenset(self.p._active_by_symbol),U=frozenset(self.p._entry_uncertain),R=frozenset(recovery_symbols))
 @contextmanager
 def staged_preload(self):
  from production_adapters import OperationalBaseReady
  from signal_admission import PackedHistory
  target=OperationalBaseReady();self._preload_local.target=target;self._preload_local.baselines={}
  try:yield target.history
  finally:self._preload_local.target=None
 def preload(self,one):
  target=getattr(self._preload_local,'target',None)
  if target is None:target=self.p.br
  else:
   from signal_admission import PackedHistory
   for s in one:
    old=self.p.br.history.get(s)
    self._preload_local.baselines[s]=(old,tuple(old.rows) if old is not None else None)
    if old is not None:
     clone=PackedHistory(maxlen=60)
     import pickle
     for raw in self._preload_local.baselines[s][1]:clone.append(pickle.loads(raw))
     target.history[s]=clone
  for s,rows in one.items():
   for b in rows:target.accumulate_only(s,b)
 def publish_preload(self,staged):
  for s,(old,raw_rows) in self._preload_local.baselines.items():
   current=self.p.br.history.get(s)
   if current is not old or (current is not None and tuple(current.rows)!=raw_rows):raise ValueError('HISTORY_CHANGED_DURING_PRELOAD')
  self.p.br.history.update(staged)
 def discard_preload_stage(self):self._preload_local.baselines={}
 def release_unused_history(self,controller,recovering,recovery_symbols=()):
  # Only the BASE_READY buffer is discarded; canonical records/outbox are untouched.
  # Serialize with original BAR/native readers. Preserve every open/uncertain case.
  with self.p._decision_lock:
   keep=set(controller.bar)|set(controller.desired_bar)|set(recovering)|set(recovery_symbols)
   keep.update(self.p._entry_opportunities);keep.update(self.p._active_by_symbol);keep.update(self.p._entry_uncertain)
   if controller.needs:keep.update(controller.needs.pins)
   if controller.inflight:keep.update(controller.inflight['after_bar'])
   released=set(self.p.br.history)-keep
   for s in released:self.p.br.release_symbol(s)
   return released
 def native_cycle(self,symbols,now,allowed):
  self.leadership.require_current();old=self.p.symbols;self.p.symbols=sorted(symbols)
  try:return self.p.poll_native5(now,allowed)
  finally:self.p.symbols=old
 def event(self,kind,msg,received,allow):
  self.leadership.require_current();self.p.utc_now=lambda:received;s=msg.get('S')
  if kind=='BAR':return self.p.on_bar(s,msg,received,allow)
  if kind=='TRADE':return self.p.on_trade(s,msg,received,allow)
  if kind=='STATUS':return self.p.on_status(msg)
