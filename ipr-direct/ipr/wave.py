"""Bounded local diagnostics and UTC minute admission gate; no I/O in snapshots."""
import math,time,threading
from collections import OrderedDict
from contextlib import contextmanager
class HeavyWindow:
 GATED_OPERATIONS=frozenset({'incremental'})
 def __init__(self,wall,stop=lambda:False,sleep=time.sleep,clock=time.monotonic):
  self.wall=wall;self.stop=stop;self.sleep=sleep;self.clock=clock;self.local=threading.local();self.lock=threading.Lock();self.active={}
  self.wait_seconds={name:0. for name in self.GATED_OPERATIONS}
 def blocked(self):
  now=self.wall();s=now.second+now.microsecond/1e6
  return s>=55 or s<10
 def wait(self,name=None):
  name=name or getattr(self.local,'name',None)
  started=None
  try:
   while self.blocked():
    if started is None:started=self.clock()
    if self.stop():raise RuntimeError('HEAVY_WORK_STOPPED')
    self.sleep(.1)
   if self.stop():raise RuntimeError('HEAVY_WORK_STOPPED')
  finally:
   if started is not None:
    elapsed=max(0.,self.clock()-started)
    with self.lock:self.wait_seconds[name]=self.wait_seconds.get(name,0.)+elapsed
 @contextmanager
 def operation(self,name):
  if name in self.GATED_OPERATIONS:self.wait(name)
  elif self.stop():raise RuntimeError('HEAVY_WORK_STOPPED')
  old=getattr(self.local,'name',None);self.local.name=name
  with self.lock:
   self.active[name]=self.active.get(name,0)+1;self.wait_seconds.setdefault(name,0.)
  try:yield
  finally:
   with self.lock:self.active[name]-=1
   self.local.name=old
 def request_allowed(self):
  return getattr(self.local,'name',None) not in self.GATED_OPERATIONS or not self.blocked()
 def before_request(self):
  if getattr(self.local,'name',None) in self.GATED_OPERATIONS:self.wait()
  elif getattr(self.local,'name',None)=='scan_rest' and self.stop():raise RuntimeError('HEAVY_WORK_STOPPED')
 def metrics(self):
  with self.lock:return {'wait_seconds_by_operation':dict(self.wait_seconds),'scope':'cumulative since child start; monotonic elapsed inside gate wait only'}
 def snapshot(self):
  with self.lock:return {k:bool(self.active.get(k,0)) for k in ('scan_rest','incremental')}

class ScanDiagnostics:
 def __init__(self):
  self.lock=threading.Lock();self.last_success_seconds=None;self.last_failure_seconds=None;self.last_watermark_age_seconds=None
 def watermark(self,end,watermark):
  age=(end-watermark).total_seconds()
  with self.lock:self.last_watermark_age_seconds=age
  return age
 def finish(self,success,seconds):
  with self.lock:
   if success:self.last_success_seconds=seconds
   else:self.last_failure_seconds=seconds
 def snapshot(self):
  with self.lock:return {'last_success_duration_seconds':self.last_success_seconds,'last_failure_duration_seconds':self.last_failure_seconds,'last_watermark_age_seconds':self.last_watermark_age_seconds}


class WaveDiagnostics:
 MINUTES=6
 SAMPLES=8192
 def __init__(self,adapter,measure,heavy,clock=time.monotonic):
  self.adapter=adapter;self.measure=measure;self.heavy=heavy;self.clock=clock;self.lock=threading.Lock();self.minutes=OrderedDict()
 def admitted(self):return len(self.adapter.c.bar-self.adapter.recovering)
 def _row(self,at):
  key=at.replace(second=0,microsecond=0).isoformat()
  if key not in self.minutes:
   self.minutes[key]={'received_BAR':0,'processed_BAR':0,'failed_BAR':0,'times':[], 'timing_overflow':0,'duration_count':0,'duration_sum':0.,'duration_max':0.,
                      'accepted_symbols_at_first_receipt':self.admitted(),'active_at_first_receipt':self.heavy.snapshot()}
   while len(self.minutes)>self.MINUTES:self.minutes.popitem(last=False)
  return self.minutes[key]
 def received(self,item,at):
  if item!='BAR':return
  with self.lock:self._row(at)['received_BAR']+=1
 def finished(self,at,seconds,ok):
  with self.lock:
   r=self._row(at);r['processed_BAR' if ok else 'failed_BAR']+=1
   r['duration_count']+=1;r['duration_sum']+=seconds;r['duration_max']=max(r['duration_max'],seconds)
   if len(r['times'])<self.SAMPLES:r['times'].append(seconds)
   else:r['timing_overflow']+=1
 def snapshot(self,ingress):
  with self.lock:
   rows={}
   for at,r in self.minutes.items():
    values=sorted(r['times']);n=len(values)
    rows[at]={k:v for k,v in r.items() if k not in {'times','duration_sum','duration_max','duration_count'}}
    rows[at]['BAR_processing_seconds']={'count':r['duration_count'],'mean':r['duration_sum']/r['duration_count'] if r['duration_count'] else None,
      'p95_nearest_rank':values[math.ceil(.95*n)-1] if n and not r['timing_overflow'] else None,
      'max':r['duration_max'] if r['duration_count'] else None,'includes_decision_lock_wait':True}
  oldest=ingress.inflight or (ingress.q._queue[0] if ingress.q is not None and ingress.q.qsize() else None)
  probe=getattr(self.adapter.bridge.p,'contention_probe',None)
  stats=probe.snapshot() if probe else None
  with self.measure.guard:spans={k:dict(v) for k,v in self.measure.spans.items()}
  current=self.measure.current_loop_lag()
  return {'receipt_minutes':rows,'accepted_symbols_now':self.admitted(),
    'active_now':self.heavy.snapshot(),'queue_length':(ingress.q.qsize() if ingress.q is not None else 0)+ingress._batch_waiting,
    'oldest_age_seconds':max(0,self.clock()-oldest[3]) if oldest else 0.,
    'inflight_BAR_elapsed_seconds':max(0,self.clock()-ingress._element_started) if ingress._element_started is not None and ingress.inflight and ingress.inflight[0]=='BAR' else None,
    'decision_lock':stats,'decision_lock_hold_by_operation':getattr(self.adapter.bridge.p._decision_lock,'snapshot',lambda:None)(),'loop_lag_current_seconds':current,
    'loop_lag_max_observed_seconds':max(spans.get('loop_lag',{}).get('max',0.),current or 0.),
    'scope':'six receipt-minute buckets, <=8192 BAR timing samples/bucket; elapsed includes lock wait; NOT CPU or wire time'}
