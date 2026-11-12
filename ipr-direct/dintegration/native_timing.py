"""R10.8 observations only; original clock, HTTP calls, rows and exceptions stay intact.

R106_FIRST_E_TIMING remains a post-print upper bound. No R10.6 fast transport,
request pool, cache or R10.5 clock dispatcher is present.
"""
import asyncio,functools,json,queue,threading,time
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from ipr import review_flags
from ipr.telemetry_safe import observing

UTC=timezone.utc

def instant(s):return datetime.fromisoformat(s.replace('Z','+00:00')) if isinstance(s,str) else s

def native_phase(runtime,phase):
 try:
  if review_flags.NATIVE_TIMING_ENABLED:
   m=getattr(runtime.a.bridge,'native_timing',None)
   if m is not None:m.safe(m.phase,phase)
 except BaseException:pass

def native_guard(runtime,name,value,at):
 try:
  if review_flags.NATIVE_TIMING_ENABLED:
   m=getattr(runtime.a.bridge,'native_timing',None)
   if m is not None:m.safe(m.guard,name,value,at)
 except BaseException:pass
 return value # The original value and short-circuit order are retained.

async def observe_boundaries(monitor,r,pause):
 # A failed observation task must not become an application task failure.
 while not r.stop.is_set():
  try:await monitor.boundary_loop(r,pause)
  except asyncio.CancelledError:raise
  except BaseException:
   try:monitor.errors+=1
   except BaseException:pass
  if not r.stop.is_set():await pause(r.stop,1.)

class _Response:
 def __init__(self,response,monitor):self.response=response;self.entered=response;self.monitor=monitor
 def __enter__(self):self.entered=self.response.__enter__();return self
 def __exit__(self,*a):return self.response.__exit__(*a)
 def __getattr__(self,name):return getattr(self.entered,name)
 def read(self,*a,**kw):
  result=self.entered.read(*a,**kw)
  self.monitor.safe(self.monitor.body_bytes,len(result))
  return result

class NativeTiming:
 MAX_LINES=4096
 MAX_LINE_BYTES=4096
 MAX_DAY_BYTES=8*1024*1024
 MAX_BOUNDARIES=96
 def __init__(self,p,rest,session_policy=None,wall=lambda:datetime.now(UTC),clock=time.monotonic,emit=None):
  self.p=p;self.rest=rest;self.api=getattr(rest.get,'__self__',None);self.wall=wall;self.clock=clock;self.emit=emit or (lambda line:print(line,flush=True))
  self.local=threading.local();self.lock=threading.Lock();self.q=queue.Queue(32);self.writer=None;self.stop=threading.Event()
  self.errors=0;self.busy=0;self.dropped=0;self.writer_errors=0;self.cycles=0;self.last=None
  self.day=None;self.lines=0;self.bytes=0;self.phase_name='UNKNOWN';self.phase_at=None;self.active_start=None
  self.observation_start=self.wall();self.last_tick=None;self.boundaries=[];self.reason_counts={};self.boundary_counts={'started_within_15s':0,'not_started_observed':0,'UNKNOWN':0}
  if session_policy:
   self.market_open=instant(session_policy['open_utc']);self.market_close=instant(session_policy['close_utc'])
   end=self.market_open+timedelta(minutes=5)
   while end<=self.market_close and len(self.boundaries)<self.MAX_BOUNDARIES:
    self.boundaries.append({'end':end,'started':None,'reasons':set(),'samples':0,'gaps':0,'done':False});end+=timedelta(minutes=5)
  else:self.market_open=None;self.market_close=None
  # Do not flood a late attachment with historic UNKNOWN lines. Their exact
  # schedule and count stay explicit in metrics; no past absence is invented.
  self.pre_observation_boundaries=0
  for b in self.boundaries:
   if b['end']+timedelta(seconds=15)<self.observation_start:
    b['done']=True;self.pre_observation_boundaries+=1;self.boundary_counts['UNKNOWN']+=1
 def safe(self,fn,*a):
  try:return fn(*a)
  except BaseException:self.errors+=1
 def current(self):return getattr(self.local,'cycle',None)
 def phase(self,name):
  self.phase_name=name;self.phase_at=self.clock()
  if name=='NO_PROVEN_SYMBOLS':self._note(self.wall(),name)
 def _note(self,at,reason):
  for b in self.boundaries:
   if not b['done'] and b['end']<=at<=b['end']+timedelta(seconds=15):b['reasons'].add(reason)
 def guard(self,name,value,at):
  if not value:self._note(self.wall(),{'ready':'READY_FALSE','scan_ok':'SCAN_OK_FALSE','rth':'OUTSIDE_RTH','trusted':'TRUSTED_FALSE'}.get(name,'UNKNOWN'))
 def boundary_tick(self,at):
  # All reasons denote actual guard checks or observed execution phases. They
  # are NOT an inferred exclusive explanation for an unsampled interval.
  gap_start=self.last_tick;self.last_tick=at
  for b in self.boundaries:
   if b['done'] or at<b['end']:continue
   deadline=b['end']+timedelta(seconds=15)
   if gap_start is not None and (at-gap_start).total_seconds()>2.5 and gap_start<deadline and at>b['end']:b['gaps']+=1
   if at<=deadline:
    b['samples']+=1
    if self.active_start is not None and self.active_start<b['end']:b['reasons'].add('PREVIOUS_CYCLE_RUNNING')
    elif self.phase_name=='PAUSE' and self.phase_at is not None:
     b['reasons'].add('PAUSE_5S' if self.clock()-self.phase_at<=5. else 'UNKNOWN_SCHEDULER_DELAY')
    elif self.phase_name=='QUEUED':b['reasons'].add('UNKNOWN_WORKER_QUEUE')
    continue
   b['done']=True
   prior_covers=b.get('prior_cycle_covered_window',False) or (self.active_start is not None and self.active_start<=b['end'] and at>deadline)
   if prior_covers:b['reasons'].add('PREVIOUS_CYCLE_RUNNING')
   complete=self.errors==0 and (prior_covers or (self.observation_start<=b['end'] and b['samples']>0 and not b['gaps']))
   outcome='STARTED_WITHIN_15S' if b['started'] is not None else 'NO_START_OBSERVED' if complete else 'UNKNOWN'
   key={'STARTED_WITHIN_15S':'started_within_15s','NO_START_OBSERVED':'not_started_observed','UNKNOWN':'UNKNOWN'}[outcome];self.boundary_counts[key]+=1
   if outcome=='NO_START_OBSERVED':
    if not b['reasons']:b['reasons'].add('UNKNOWN')
    for reason in b['reasons']:self.reason_counts[reason]=self.reason_counts.get(reason,0)+1
   self.enqueue({'stage':'R108_NATIVE_BOUNDARY','session':self.p.session,'bar_end':b['end'].isoformat(),'first_cycle_start_utc':b['started'].isoformat() if b['started'] else None,
                 'outcome':outcome,'observed_blockers':sorted(b['reasons']),'observation_samples':b['samples'],'sampling_gaps':b['gaps'],
                 'coverage':'continuous sampler presence' if complete else 'partial/late/gapped; absence UNKNOWN',
                 'reason_scope':'observed guards/phases; multiple reasons may coexist; unsampled causes UNKNOWN'})
 async def boundary_loop(self,r,pause):
  while not r.stop.is_set():
   self.safe(lambda:self.boundary_tick(r.wall()))
   await pause(r.stop,1.) # Observer only; original native pause remains 5 seconds.
 def http_start(self):
  c=self.current()
  if c is not None:c['request_count']+=1
 def body_bytes(self,n):
  c=self.current()
  if c is not None:c['response_bytes']+=n
 def request(self,operation,wait,hold):
  c=self.current()
  if c is None:return
  c['rest_acquisitions']+=1;c['rest_wait_seconds']+=wait;c['rest_hold_seconds']+=hold
  if len(c['request_samples'])<8:c['request_samples'].append({'operation':operation,'wait_seconds':wait,'hold_seconds':hold})
 def sip_response(self,data,params,at):
  c=self.current()
  if c is None or params.get('timeframe')!='5Min' or not isinstance(data,dict):return
  bars=data.get('bars')
  if not isinstance(bars,dict):return
  target=instant(c['latest_closed_bar_end']);target_start=target-timedelta(minutes=5)
  for symbol,rows in bars.items():
   if not isinstance(rows,list):continue
   # Native history is 305 minutes; the observer inspects <=128 rows/symbol
   # and does not sort, modify, retain or fetch any row.
   if len(rows)>128:c['sip_inspection_clipped']+=1
   for row in rows[:128]:
    if isinstance(row,dict) and instant(row.get('t'))==target_start:
     lag=(at-target).total_seconds();c['sip_target_matches']+=1
     if c['sip_first_seen_utc'] is None:c['sip_first_seen_utc']=at.isoformat();c['sip_first_receipt_lag_seconds']=lag
     c['sip_last_receipt_lag_seconds']=lag
     break
 def probe(self,stage,fields):
  c=self.current()
  if c is None:return
  now=self.clock()
  if stage=='native5_cycle_before':c['pending_symbols']=fields.get('eligible_symbols');c['before_fetch_work_seconds']=now-c['started_mono']
  elif stage=='native5_before_bars_multi':c['fetch_start']=now
  elif stage=='native5_after_bars_multi':c['fetch_seconds']+=now-c.pop('fetch_start',now)
 def evaluated(self,seconds):
  c=self.current()
  if c is not None:c['evaluation_seconds']+=seconds
 def elapsed(self,name,seconds):
  c=self.current()
  if c is not None:c[name]=c.get(name,0.)+seconds
 def poll_result(self,result):
  c=self.current()
  if c is not None and isinstance(result,dict):
   c['pending_symbols']=result.get('eligible_symbols',c['pending_symbols']);c['required_history_minutes']=result.get('required_history_minutes')
   for name in ('early_core_persisted','early_core_persist_errors','confluence_errors'):c[name]=result.get(name)
   failure=result.get('early_core_first_persist_error')
   c['persistence_error_type']=failure.get('type') if isinstance(failure,dict) else None
 def persisted(self,result,seconds):
  c=self.current()
  if c is not None:
   c['persistence_seconds']+=seconds
   if isinstance(result,tuple) and len(result)>1 and isinstance(result[1],dict):c['pending_E']=result[1]
 def post_print(self,symbol):
  c=self.current()
  if c is None:return
  record=c.pop('pending_E',None)
  if not record or record.get('symbol')!=symbol:return
  now=self.wall();bar=record.get('bar_end_ts');end=instant(bar)
  self.enqueue({'stage':'R106_FIRST_E_TIMING','symbol':symbol,'session':self.p.session,'bar_end':bar,'cycle_start_utc':c['cycle_start_utc'],'post_FIRST_E_utc':now.isoformat(),
   'bar_close_to_post_FIRST_E_seconds':(now-end).total_seconds(),'cycle_start_from_bar_close_seconds':(instant(c['cycle_start_utc'])-end).total_seconds(),
   'cycle_elapsed_to_post_FIRST_E_seconds':self.clock()-c['started_mono'],**{k:c[k] for k in ('pending_symbols','fetch_seconds','rest_wait_seconds','rest_hold_seconds','evaluation_seconds','persistence_seconds','request_count','response_bytes')},
   'scope':'captured after original FIRST_E print, before confluence; sums overlap fetch; no decision/clock change'})
 def enqueue(self,row):
  # No stdout/file/network I/O or wait-for-capacity in the decision thread.
  try:self.q.put_nowait(row)
  except queue.Full:self.dropped+=1;return
  if self.writer is None and self.lock.acquire(False):
   try:
    if self.writer is None:self.writer=threading.Thread(target=self._write,name='ipr-native-timing',daemon=True);self.writer.start()
   finally:self.lock.release()
 def _write(self):
  while not self.stop.is_set() or not self.q.empty():
   try:row=self.q.get(timeout=.1)
   except queue.Empty:continue
   try:
    line=json.dumps(row,separators=(',',':'),ensure_ascii=True);size=len(line.encode())+1;day=self.wall().date().isoformat()
    if day!=self.day:self.day=day;self.lines=0;self.bytes=0
    if size>self.MAX_LINE_BYTES or self.lines>=self.MAX_LINES or self.bytes+size>self.MAX_DAY_BYTES:self.dropped+=1;continue
    self.lines+=1;self.bytes+=size;self.emit(line)
   except BaseException:self.writer_errors+=1
   finally:self.q.task_done()
 def close(self):
  self.stop.set()
  if self.writer:self.writer.join(timeout=1.)
 @contextmanager
 def cycle(self,now,symbols):
  prior=self.current();at=self.wall();mono=self.clock();latest=at.replace(minute=at.minute//5*5,second=0,microsecond=0)
  c={'stage':'R108_NATIVE_CYCLE','session':self.p.session,'cycle_start_utc':at.isoformat(),'poll_cutoff_utc':now.isoformat(),'latest_closed_bar_end':latest.isoformat(),
   'poll_enter_utc':None,'queue_wait_seconds':max(0.,mono-self.phase_at) if self.phase_name=='QUEUED' and self.phase_at is not None else None,
   'start_after_latest_close_seconds':(at-latest).total_seconds(),'input_symbols':len(symbols),'pending_symbols':None,'before_fetch_work_seconds':0.,'fetch_seconds':0.,
   'rest_acquisitions':0,'rest_wait_seconds':0.,'rest_hold_seconds':0.,'rest_measurement_available':None,'evaluation_seconds':0.,'persistence_seconds':0.,'request_count':0,'response_bytes':0,
   'sip_first_seen_utc':None,'sip_first_receipt_lag_seconds':None,'sip_last_receipt_lag_seconds':None,'sip_target_matches':0,'sip_inspection_clipped':0,'request_samples':[],
   'started_mono':mono,'completion':'UNKNOWN','failure_type':None,'sip_target_eligible_for_poll':latest<=now}
  self.local.cycle=c;self.active_start=at;self.phase_name='RUNNING';self.phase_at=mono
  for b in self.boundaries:
   if b['end']<=at<=b['end']+timedelta(seconds=15) and b['started'] is None:b['started']=at
  try:yield c
  except BaseException as exc:
   c['completion']='FAILED';c['failure_type']=type(exc).__name__[:80];raise
  else:c['completion']='COMPLETED'
  finally:
   try:
    ended=self.wall()
    for b in self.boundaries:
     if not b['done'] and at<=b['end'] and ended>=b['end']+timedelta(seconds=15):b['prior_cycle_covered_window']=True;b['reasons'].add('PREVIOUS_CYCLE_RUNNING')
    c['cycle_seconds']=self.clock()-mono;c.pop('started_mono',None);c.pop('pending_E',None);c.pop('fetch_start',None)
    self.cycles+=1;self.last=dict(c);self.enqueue(dict(c))
   except BaseException:self.errors+=1
   finally:self.local.cycle=prior;self.active_start=None;self.phase_name='IDLE'
 def install(self,bridge):
  old_native=bridge.native_cycle
  @functools.wraps(old_native)
  def native(symbols,now,allowed):
   if not review_flags.NATIVE_TIMING_ENABLED:return old_native(symbols,now,allowed)
   with observing(lambda:self.cycle(now,symbols)) as available:
    if not available:
     try:self.errors+=1
     except BaseException:pass
    result=old_native(symbols,now,allowed);self.safe(self.poll_result,result);return result
  bridge.native_cycle=native
  old_poll=self.p.poll_native5
  @functools.wraps(old_poll)
  def poll(*a,**kw):
   def note():
    c=self.current()
    if c is not None:c['poll_enter_utc']=self.wall().isoformat()
   self.safe(note);return old_poll(*a,**kw)
  self.p.poll_native5=poll
  old_probe=self.p._memory_probe
  @functools.wraps(old_probe)
  def probe(stage,**fields):self.safe(self.probe,stage,fields);return old_probe(stage,**fields)
  self.p._memory_probe=probe
  old_eval=self.p.ec.evaluate_with_telemetry
  @functools.wraps(old_eval)
  def evaluate(*a,**kw):
   t=self.safe(self.clock)
   try:return old_eval(*a,**kw)
   finally:self.safe(lambda:self.evaluated(self.clock()-t))
  self.p.ec.evaluate_with_telemetry=evaluate
  old_persist=self.p.ew.persist_first_e
  @functools.wraps(old_persist)
  def persist(*a,**kw):
   t=self.safe(self.clock);result=old_persist(*a,**kw);self.safe(lambda:self.persisted(result,self.clock()-t));return result
  self.p.ew.persist_first_e=persist
  cw=getattr(self.p,'cw',None);old_canonical=getattr(cw,'evaluate_and_persist',None)
  if old_canonical is not None:
   @functools.wraps(old_canonical)
   def canonical(*a,**kw):
    t=self.safe(self.clock)
    try:return old_canonical(*a,**kw)
    finally:self.safe(lambda:self.elapsed('persistence_seconds',self.clock()-t))
   cw.evaluate_and_persist=canonical
  old_confluence=self.p._confluence
  @functools.wraps(old_confluence)
  def confluence(symbol,*a,**kw):
   self.safe(self.post_print,symbol);t=self.safe(self.clock)
   try:return old_confluence(symbol,*a,**kw)
   finally:self.safe(lambda:self.elapsed('confluence_seconds',self.clock()-t))
  self.p._confluence=confluence
  old_get=self.rest.get;api=self.api
  @functools.wraps(old_get)
  def get(path,params):
   if self.current() is None:return old_get(path,params)
   previous=getattr(api.local,'request_observer',None) if api else None
   def observer(*v):
    self.safe(self.request,*v)
    if previous is not None:
     try:previous(*v)
     except BaseException:pass
   with observing(lambda:api.observe_requests(observer)) as available:
    c=self.current()
    if c is not None:
     c['rest_measurement_available']=available and c['rest_measurement_available'] is not False
     if not available:self.errors+=1
    data=old_get(path,params)
   self.safe(lambda:self.sip_response(data,params,self.wall()));return data
  self.rest.get=get
  if api is not None and hasattr(api,'opener'):
   original=api.opener
   @functools.wraps(original)
   def opener(*a,**kw):
    active=self.current() is not None
    if active:self.safe(self.http_start)
    response=original(*a,**kw)
    if active:
     try:return _Response(response,self)
     except BaseException:self.errors+=1
    return response
   api.opener=opener
 def snapshot(self):
  return {'mode':'MEASUREMENT_ONLY','cycles':self.cycles,'last':self.last,'observer_errors':self.errors,'observations_skipped_busy':self.busy,'log_dropped':self.dropped,'writer_errors':self.writer_errors,
   'log_queue':self.q.qsize(),'day_lines_written_or_attempted':self.lines,'day_bytes_written_or_attempted':self.bytes,'line_limit_per_process_UTC_day':self.MAX_LINES,'byte_limit_per_process_UTC_day':self.MAX_DAY_BYTES,
   'observation_start_utc':self.observation_start.isoformat(),'pre_observation_boundaries_UNKNOWN':self.pre_observation_boundaries,
   'boundary_day_utc':self.p.session,'boundary_counts':dict(self.boundary_counts),'missed_boundary_observed_reason_counts':dict(self.reason_counts),
   'reason_scope':'counts of missed observed boundaries with each measured reason; not exclusive causes; sampling gaps UNKNOWN'}

def attach_native_timing(bridge,rest,session_policy):
 bridge.native_timing=None
 if not review_flags.NATIVE_TIMING_ENABLED:return
 try:
  monitor=NativeTiming(bridge.p,rest,session_policy);monitor.install(bridge);bridge.native_timing=monitor;rest.measure.native_timing=monitor
 except BaseException:pass # Observation cannot prevent original construction.
