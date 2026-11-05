"""Local bounded scan gate and diagnostics. Never calls the engine or Redis."""
import json, math, threading, time
from collections import OrderedDict
from datetime import datetime, timezone

SCAN_VALIDITY_SECONDS = 150

def emit(row):
 try:print(json.dumps(row),flush=True)
 except Exception:pass  # Observation never changes an existing admission decision.

class RTHVolumeStale(ValueError):
 pass

class ScanGate:
 def __init__(self,state,clock=time.monotonic,wall=lambda:datetime.now(timezone.utc),started_at=None,log=emit):
  self.state=state;self.clock=clock;self.wall=wall;self.log=log;self.lock=threading.RLock()
  self.started=clock() if started_at is None else started_at;self.accounted=self.started
  self.total={False:0.,True:0.};self.last_success=None;self.generation=0
 def _account(self,now):
  self.total[bool(self.state['scan_ok'])]+=max(0.,now-self.accounted);self.accounted=now
 def _change(self,value,reason,now):
  self._account(now);old=bool(self.state['scan_ok']);self.state['scan_ok']=value
  if old!=value:
   self.log({'stage':'SCAN_GATE_TRANSITION','at':self.wall().isoformat(),'from':old,'to':value,'reason':reason,
             'true_seconds':self.total[True],'false_seconds':self.total[False]})
 def success(self,generation=None,allowed=True):
  with self.lock:
   if not allowed or (generation is not None and generation!=self.generation):return False
   self.last_success=self.clock();self._change(True,'SCAN_SUCCESS',self.last_success);return True
 def invalidate(self,reason):
  with self.lock:
   self.generation+=1;self.last_success=None;self._change(False,reason,self.clock())
 def expire(self):
  with self.lock:
   if self.state['scan_ok'] and (self.last_success is None or self.clock()-self.last_success>SCAN_VALIDITY_SECONDS):
    self._change(False,'SCAN_EXPIRED',self.clock())
   return bool(self.state['scan_ok'])
 def failure(self,exc):
  if type(exc) is RTHVolumeStale:
   return self.expire()  # Preserve ONLY a previous uninvalidated, unexpired success.
  self.invalidate('SCAN_FAILED');return False
 def snapshot(self):
  with self.lock:
   self._account(self.clock());total=sum(self.total.values())
   return {'validity_seconds':SCAN_VALIDITY_SECONDS,'scan_ok':bool(self.state['scan_ok']),
           'true_seconds':self.total[True],'false_seconds':self.total[False],
           'elapsed_seconds':total,'availability_ratio':self.total[True]/total if total else None,
           'last_success_age_seconds':None if self.last_success is None else max(0.,self.clock()-self.last_success),
           'scope':'monotonic time since child main instrumentation; not process exec timestamp'}

def gate_for(state,clock=time.monotonic,wall=lambda:datetime.now(timezone.utc),started_at=None):
 if '_scan_gate' not in state:state['_scan_gate']=ScanGate(state,clock,wall,started_at)
 return state['_scan_gate']

class ScanExclusions:
 REASONS=('PRICE','LAST_TRADE_STALE','HISTORY_OR_VOLUME_MISSING','SNAPSHOT_MISSING','SNAPSHOT_AGE_GT60','ADDITIONS_BLOCKED_RECOVERY','ADDITIONS_NOT_DUE')
 def __init__(self):
  self.counts={k:0 for k in self.REASONS};self.samples={k:[] for k in self.REASONS}
  self.scoring_complete=False;self.snapshot_check_complete=False;self.selection_check_complete=False
 def add(self,reason,symbol):
  self.counts[reason]=self.counts.get(reason,0)+1
  if len(self.samples.setdefault(reason,[]))<3:self.samples[reason].append(symbol)
 def selection(self,previous,scores,pins,target,blocked,due):
  eligible=set(scores)-set(pins);target=set(target)
  unfilled=max(0,500-len(target));available_not_added=len(eligible-target)
  why='ADDITIONS_BLOCKED_RECOVERY' if blocked else 'ADDITIONS_NOT_DUE'
  if blocked or not due:
   self.counts[why]=min(unfilled,available_not_added)
   self.samples[why]=sorted(eligible-target)[:3] if self.counts[why] else []
  self.retained=len(set(previous)&target);self.unfilled=unfilled
  self.selection_check_complete=True
 def report(self,at,outcome):
  counts=dict(self.counts)
  if not self.snapshot_check_complete:counts['SNAPSHOT_AGE_GT60']=None
  if not self.selection_check_complete:
   counts['ADDITIONS_BLOCKED_RECOVERY']=None;counts['ADDITIONS_NOT_DUE']=None
  emit({'stage':'SCAN_EXCLUSIONS','at':at.isoformat(),'outcome':outcome,'counts':counts,'samples':self.samples,
        'scoring_complete':self.scoring_complete,'snapshot_check_complete':self.snapshot_check_complete,'selection_check_complete':self.selection_check_complete,
        'retained_previous':getattr(self,'retained',None),'unfilled_slots':getattr(self,'unfilled',None),
        'scope':'first rejection reason per symbol; addition counts are unfilled slots, not ENTRY opportunities'})

class BlockedTrades:
 def __init__(self,allowed_symbols,log=emit):
  self.allowed=frozenset(allowed_symbols);self.log=log;self.lock=threading.Lock();self.total=0;self.minutes=OrderedDict();self.errors=0
 def record(self,s,msg,now):
  # Telemetry must not throw into or change final_filter's existing False decision.
  try:
   if s not in self.allowed:return
   minute=int(now.timestamp()//60);row=None
   with self.lock:
    self.total+=1
    if self.minutes and minute<max(self.minutes)-5:return
    bucket=self.minutes.setdefault(minute,{'count':0,'symbols':set(),'logged':0})
    bucket['count']+=1;first=s not in bucket['symbols'];bucket['symbols'].add(s)
    for old in list(self.minutes):
     if old<max(self.minutes)-5:del self.minutes[old]
    if first and bucket['logged']<50:
     bucket['logged']+=1
     flags=msg.get('c');flags=[x for x in flags[:16] if isinstance(x,str) and len(x)==1] if isinstance(flags,list) else None
     size=msg.get('s');size=size if type(size) in (int,float) and math.isfinite(size) else None
     row={'stage':'SCAN_GATE_BLOCKED_TRADE','symbol':s,'at':now.isoformat(),'price':msg.get('p'),'size':size,'conditions':flags}
   if row is not None:self.log(row)
  except Exception:
   with self.lock:self.errors+=1
 def snapshot(self):
  with self.lock:
   return {'total_rejected_trade_messages':self.total,'minutes':[{'minute_utc':datetime.fromtimestamp(k*60,timezone.utc).isoformat(),
           'rejected_trade_messages':v['count'],'distinct_symbols':len(v['symbols']),'logged_symbols':v['logged']} for k,v in sorted(self.minutes.items())],
           'telemetry_errors':self.errors,'scope':'blocked TRADE messages only; not counterfactual ENTRY or recall; max 50 detail logs/minute, last 6 UTC minutes'}

def blocked_context_valid(runtime,s):
 if runtime is None or not runtime.a.transport_trusted or runtime.c.failed or s not in runtime.c.bar or s in runtime.a.recovering:return False
 proof=runtime.c.proofs.get(s,{})
 if proof.get('connection')!=runtime.c.connection or not all(proof.get(k) for k in ('history','continuity','reconciled')):return False
 # Conservative telemetry-only mirror of the remaining adapter opportunity floor checks.
 opp=runtime.a.bridge.p._entry_opportunities.get(s)
 if opp:
  if (opp['session'],s,opp['entry_trigger_ts']) in runtime.a.missed_opportunities:return False
  floor=runtime.a.trusted_since
  if floor is None:return False
  floor=max(floor,runtime.a.symbol_admitted_at.get(s,floor))
  if datetime.fromisoformat(opp['entry_trigger_ts'].replace('Z','+00:00'))<floor:return False
 return True
