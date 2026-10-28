"""Bounded, non-awaiting WS admission. No Redis or decision lock on reader.
CPython threads still share the GIL: this does NOT promise a real-time bound.
Queue age is measured from decoded callback receipt, not wire/server send time.
"""
import asyncio,time
from contextlib import nullcontext
from contention_probe import TLS
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from ingress_delay_stats import MinuteDelayStats

class IngressBacklogError(RuntimeError):pass

class BoundedWSIngress:
 def __init__(self,pipeline,recovery,allowed,*,capacity=8192,max_age_seconds=5.0,on_gap=None,logger=print):
  if capacity<1 or max_age_seconds<=0:raise ValueError('INVALID_INGRESS_BOUNDS')
  self.p=pipeline;self.rec=recovery;self.allowed=allowed
  self.capacity=capacity;self.max_age=max_age_seconds;self.logger=logger;self.on_gap=on_gap
  self.received=Counter();self.processed=Counter();self.ignored=Counter();self.dropped=Counter()
  self.high_water=0;self.max_delay=0.;self.max_complete_delay=0.;self.max_reader_callback=0.;self.failure_count=0
  self.q=None;self.task=None;self.watch=None;self.failure=None;self.accepting=False;self.inflight=None
  self.executor=None;self._logged=False
  self.delay_stats=MinuteDelayStats();self._start_delay=0.;self._batch_waiting=0;self.batch_size=32;self._element_started=None
 async def begin(self):
  if self.task is not None:raise RuntimeError('INGRESS_NOT_DRAINED')
  self.q=asyncio.Queue(maxsize=self.capacity);self.failure=asyncio.get_running_loop().create_future()
  self.accepting=True;self._logged=False;self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ws-decision')
  self.task=asyncio.create_task(self._consume());self.watch=asyncio.create_task(self._watch_age())
 def submit(self,kind,msg,received_at):
  begin=time.monotonic();self.received[kind]+=1
  if not self.accepting:
   self.dropped[kind]+=1
   if self.failure and self.failure.done():raise self.failure.result()
   raise IngressBacklogError('INGRESS_NOT_ACCEPTING')
  # Membership only, no iteration, Redis, conversion, decision lock or per-tick thread.
  # Cache insert/remove is the linearization point under supported CPython GIL.
  symbol=msg.get('S')
  if kind=='TRADE' and symbol not in self.p._entry_opportunities and symbol not in self.p._active_by_symbol:
   self.ignored[kind]+=1
  elif kind not in {'BAR','TRADE','STATUS'} or (kind in {'BAR','TRADE'} and not symbol):
   self.ignored[kind]+=1
  else:
   try:
    if self.q.qsize()+self._batch_waiting>=self.capacity:raise asyncio.QueueFull
    self.q.put_nowait((kind,msg,received_at,begin))
   except asyncio.QueueFull:
    self.dropped[kind]+=1;self._fail(IngressBacklogError('INGRESS_QUEUE_FULL'))
    raise self.failure.result()
   self.high_water=max(self.high_water,self.q.qsize())
  self.max_reader_callback=max(self.max_reader_callback,time.monotonic()-begin)
 def _fail(self,exc):
  self.accepting=False
  if self.failure and not self.failure.done():
   self.failure_count+=1
   # Pure in-memory DISCONNECT transition now, before socket close handshake.
   if self.on_gap is not None:self.on_gap()
   self.failure.set_result(exc)
   if not self._logged:
    self._logged=True
    try:self.logger({'stage':'WS_INGRESS_FAIL_CLOSED','reason':str(exc) if isinstance(exc,IngressBacklogError) else type(exc).__name__,'error_type':type(exc).__name__,'utc':datetime.now(timezone.utc).isoformat()},flush=True)
    except Exception:pass
 def _work(self,item):
  kind,msg,received_at,at=item
  # The worker may wait here; the WS reader never does. Recheck permission AFTER lock.
  with (self.p._decision_lock.operation("ingress_"+kind) if hasattr(self.p._decision_lock,"operation") else nullcontext()), self.p._decision_lock:
   delay=time.monotonic()-at;self._start_delay=delay;self.max_delay=max(self.max_delay,delay)
   if not self.accepting:return False
   if delay>self.max_age:raise IngressBacklogError('INGRESS_QUEUE_TOO_OLD')
   allow=self.allowed();sym=msg.get('S')
   if kind=='BAR':self.p.on_bar(sym,msg,received_at,allow)
   elif kind=='TRADE':self.p.on_trade(sym,msg,received_at,allow)
   elif kind=='STATUS':self.rec.on_status(msg);self.p.on_status(msg)
   self.rec.on_stream_message(kind,msg)
   return True
 def _record_bar_delay(self,item,status,start_delay,complete_delay):
  if item[0]!='BAR':return
  try:self.delay_stats.record(item[3],start_delay,complete_delay,self.max_age,status)
  except Exception:self.delay_stats.record_errors+=1
 def _work_batch(self,batch):
  records=[]
  for i,item in enumerate(batch):
   self.inflight=item;self._batch_waiting=len(batch)-i-1;self._start_delay=0.
   if not self.accepting:break
   t0=time.monotonic();self._element_started=t0;TLS.ingress=True
   try:ok=self._work(item);error=None
   except Exception as exc:ok=False;error=exc
   finally:
    TLS.ingress=False
    probe=getattr(self.p,"contention_probe",None)
    if probe:
     probe.record("element",item[0],time.monotonic()-t0)
     if time.monotonic()-t0>1.:probe.request("ELEMENT_OVER_1_SECOND",item[3])
    self._element_started=None
   records.append((item,ok,error,self._start_delay,time.monotonic()-item[3]))
   if error is not None:break
  return records
 async def _consume(self):
  try:
   while self.accepting:
    first=await self.q.get();batch=[first]
    while len(batch)<self.batch_size and not self.q.empty():batch.append(self.q.get_nowait())
    self.inflight=first;self._batch_waiting=len(batch)-1
    try:
     records=await asyncio.get_running_loop().run_in_executor(self.executor,self._work_batch,batch)
     for item,ok,error,start_delay,complete_delay in records:
      if ok:
       self.processed[item[0]]+=1;self.max_complete_delay=max(self.max_complete_delay,complete_delay)
      else:self.dropped[item[0]]+=1
      self._record_bar_delay(item,'OK'if ok else'FAILED_OR_DROPPED',start_delay,complete_delay)
      if error is not None:
       from confluence_diagnostics import raise_if_leadership_lost
       try:raise_if_leadership_lost(error)
       except Exception:self.p._confluence_lease_lost=True
       self._fail(error)
     for item in batch[len(records):]:self.dropped[item[0]]+=1
    finally:
     self._batch_waiting=0;self.inflight=None
     for item in batch:self.q.task_done()
  except asyncio.CancelledError:raise
 async def _watch_age(self):
  while self.accepting:
   await asyncio.sleep(.25)
   # Private asyncio queue belongs to this loop; bounded peek, no copy/walk.
   probe=getattr(self.p,"contention_probe",None)
   if probe and self._element_started and time.monotonic()-self._element_started>1.0:
    probe.request("ELEMENT_OVER_1_SECOND",self.inflight[3] if self.inflight else self._element_started)
   oldest=self.inflight or (self.q._queue[0] if self.q.qsize() else None)
   if oldest and time.monotonic()-oldest[3]>2.0:
    probe=getattr(self.p,"contention_probe",None)
    if probe:probe.request("QUEUE_AGE_OVER_2_SECONDS",oldest[3])
   if oldest and time.monotonic()-oldest[3]>self.max_age:
    self._fail(IngressBacklogError('INGRESS_BACKLOG_TIMEOUT'))
 async def wait_failure(self):
  return await asyncio.shield(self.failure)
 async def end(self):
  self.accepting=False
  if self.watch:self.watch.cancel();await asyncio.gather(self.watch,return_exceptions=True)
  # No new job can start. An in-flight thread isn't forcibly killed; await it before reconnect.
  if self.task:
   if self.inflight is None:self.task.cancel()
   await asyncio.gather(self.task,return_exceptions=True);self.task=None
  if self.q:
   while not self.q.empty():
    item=self.q.get_nowait();self.dropped[item[0]]+=1;self.q.task_done()
  if self.executor:self.executor.shutdown(wait=False,cancel_futures=True);self.executor=None
 def snapshot(self):
  return {'bar_delay_distribution':self.delay_stats.snapshot(),'max_age_seconds':self.max_age,'received':dict(self.received),'processed':dict(self.processed),'ignored_no_interest_or_control':dict(self.ignored),'dropped':dict(self.dropped),'queue_length':(self.q.qsize() if self.q else 0)+self._batch_waiting,'worker_batch_size':self.batch_size,'queue_capacity':self.capacity,'queue_high_water':self.high_water,'inflight':1 if self.inflight else 0,'max_receive_to_processing_seconds':self.max_delay,'max_receive_to_processing_start_seconds':self.max_delay,'max_receive_to_processing_complete_seconds':self.max_complete_delay,'max_reader_callback_seconds':self.max_reader_callback,'fail_closed_count':self.failure_count,'scope':'decoded callback receive, cumulative per process; per-field observational snapshot; NOT wire latency or a hard realtime guarantee'}
