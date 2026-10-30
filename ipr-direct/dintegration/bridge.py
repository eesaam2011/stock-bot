"""Caller-side integration with byte-identical original decision modules."""
from pathlib import Path
from datetime import datetime,timezone
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'reference_app'))
from production_pipeline import ProductionDecisionPipeline
from production_adapters import OperationalEarlyCore,OperationalBaseReady
from signal_admission import SignalAdmission
from state_store import validate_record
from dreview.core import Needs
class OriginalBridge:
 def __init__(self,r,lua,leadership,session,root_symbols,rest,worker,session_policy):
  self.r=r;self.leadership=leadership;self.rest=rest;self.needs_revision=0
  self.p=ProductionDecisionPipeline(r,lua,leadership,session,sorted(root_symbols),OperationalEarlyCore(rest),OperationalBaseReady(),rest,worker,shadow=False)
  self.p.signal_gate=SignalAdmission(session,session_policy)
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
 def preload(self,one):
  for s,rows in one.items():
   for b in rows:self.p.br.accumulate_only(s,b)
 def native_cycle(self,symbols,now,allowed):
  self.leadership.require_current();old=self.p.symbols;self.p.symbols=sorted(symbols)
  try:return self.p.poll_native5(now,allowed)
  finally:self.p.symbols=old
 def event(self,kind,msg,received,allow):
  self.leadership.require_current();self.p.utc_now=lambda:received;s=msg.get('S')
  if kind=='BAR':return self.p.on_bar(s,msg,received,allow)
  if kind=='TRADE':return self.p.on_trade(s,msg,received,allow)
  if kind=='STATUS':return self.p.on_status(msg)
