"""R10.9: native-only transport containment; no retries or clock changes.

The production BAR cap and poll_native5 batch are both 500. All HTTP pages
finish before the single batch is evaluated. Reject a larger native scope
before entering the pipeline; never catch a failure after that barrier.
"""
import errno,functools,http.client,inspect,json,socket,ssl,threading,time,urllib.error
from contextlib import nullcontext
from datetime import datetime,timezone
from .api import APIError,Alpaca
from . import review_flags

UTC=timezone.utc
# from None hides, but does not erase, the original Python __context__.
_source,_line=inspect.getsourcelines(Alpaca.call)
_TRANSPORT_LINES=frozenset(_line+i for i,s in enumerate(_source) if 'with self.opener(' in s or 'raw=r.read(' in s)
_NETWORK_ERRNOS=frozenset(getattr(errno,n) for n in ('ECONNRESET','ECONNREFUSED','ECONNABORTED','ETIMEDOUT','EPIPE','ENETDOWN','ENETUNREACH','EHOSTUNREACH'))

def transport_cause(exc,api):
 """Only a transport failure at the actual Alpaca opener/read boundary.

No HTTP status, JSON/schema, hook/Redis, evidence, or generic APIError is
admitted. A matching string alone is never provenance.
 """
 if type(exc) is not APIError or exc.args!=('ALPACA_READ_FAILED',):return None
 cause=exc.__context__
 allowed=isinstance(cause,(TimeoutError,ConnectionError,urllib.error.URLError,http.client.IncompleteRead,http.client.RemoteDisconnected,ssl.SSLError,socket.gaierror,socket.herror))
 if not allowed and type(cause) is OSError:allowed=cause.errno in _NETWORK_ERRNOS
 if not allowed:return None
 tb=cause.__traceback__
 while tb is not None:
  f=tb.tb_frame
  if f.f_code is Alpaca.call.__code__ and f.f_locals.get('self') is api and tb.tb_lineno in _TRANSPORT_LINES and f.f_locals.get('host')=='data.alpaca.markets' and f.f_locals.get('path')=='/v2/stocks/bars':return type(cause).__name__
  tb=tb.tb_next
 return None

class NativeResilience:
 def __init__(self,api,bridge,session,clock=time.monotonic,wall=lambda:datetime.now(UTC),emit=None):
  self.api=api;self.bridge=bridge;self.session=session;self.clock=clock;self.wall=wall;self.emit=emit or (lambda row:print(json.dumps(row,separators=(',',':')),flush=True))
  self.lock=threading.RLock();self.local=threading.local();self.day=None;self.total=0;self.successes=0;self.consecutive=0;self.trusted=True;self.active_since=None;self.last_success=None;self.failure_since=None;self.observer_errors=0;self.last_failure=None;self.last_success_utc=None
  self._install()
 def _log(self,stage,**fields):
  try:self.emit({'stage':stage,'session':self.session,'utc':self.wall().isoformat(),**fields})
  except BaseException:self.observer_errors+=1 # Telemetry cannot replace an application exception.
 def _day(self):
  day=self.wall().date().isoformat()
  if day!=self.day:self.day=day;self.total=0;self.successes=0
 def _untrust(self,why):
  if self.trusted:
   self.trusted=False;self._log('R109_NATIVE_TRUST',native_trusted=False,reason=why,consecutive_failures=self.consecutive)
 def _fatal_if_due(self,at):
  if self.failure_since is not None and at-self.failure_since>=600.:
   self._log('R109_NATIVE_FATAL',reason='CONTINUOUS_TRANSPORT_FAILURE_600S',failure_seconds=at-self.failure_since)
   raise APIError('ALPACA_READ_FAILED') from None
 def tick(self,in_session):
  with self.lock:
   self._day();at=self.clock()
   if not in_session:
    self.active_since=None;self.last_success=None;self.failure_since=None
    return
   if self.active_since is None:self.active_since=at
   self._fatal_if_due(at)
   base=self.last_success if self.last_success is not None else self.active_since
   if at-base>=90.:self._untrust('NO_SUCCESS_90S')
 def _full_fetch(self):
  # No nonfatal Alpaca request remains after this barrier in the <=500
  # production cycle. Restore native admission only after ALL pages and schema
  # validation have completed, never merely after a single HTTP response.
  with self.lock:
   self._fatal_if_due(self.clock())
   if not self.trusted:
    self.trusted=True;self._log('R109_NATIVE_TRUST',native_trusted=True,reason='COMPLETE_NATIVE_FETCH_VALIDATED',consecutive_failures=self.consecutive)
 def _install(self):
  rest=getattr(getattr(self.bridge,'p',None),'rest',None)
  if rest is not None:
   old=rest.bars_multi
   @functools.wraps(old)
   def bars(*a,**kw):
    c=getattr(self.local,'cycle',None)
    if c is None:return old(*a,**kw) # scan/recover/other threads: exact delegation.
    if c['fetches'] or len(a[0])>500:raise RuntimeError('R109_NATIVE_ATOMIC_BATCH_BOUND')
    c['fetches']+=1
    try:result=old(*a,**kw)
    except APIError as exc:
     cause=transport_cause(exc,self.api)
     if cause is not None:c['transport_error']=exc;c['original_type']=cause
     raise
    self._full_fetch();return result
   rest.bars_multi=bars
  old_opener=getattr(self.api,'opener',None)
  if old_opener is not None:
   @functools.wraps(old_opener)
   def opener(*a,**kw):
    c=getattr(self.local,'cycle',None)
    if c is not None:c['request_count']+=1
    return old_opener(*a,**kw)
   self.api.opener=opener
 def work(self,symbols,now,allowed=True):
  if len(symbols)>500:raise RuntimeError('R109_NATIVE_ATOMIC_BATCH_BOUND')
  c={'start':self.clock(),'fetches':0,'transport_error':None,'original_type':None,'request_count':0,'rest_hold_seconds':0.}
  prior=getattr(self.local,'cycle',None);self.local.cycle=c
  previous=getattr(getattr(self.api,'local',None),'request_observer',None)
  def request(operation,wait,hold):
   if operation=='native':c['rest_hold_seconds']+=hold
   if previous is not None:previous(operation,wait,hold)
  observe=self.api.observe_requests(request) if hasattr(self.api,'observe_requests') else nullcontext()
  try:
   try:
    with observe:result=self.bridge.native_cycle(symbols,now,allowed)
   except APIError as exc:
    if c['transport_error'] is not exc:raise
    with self.lock:
     self._day();at=self.clock();self.consecutive+=1;self.total+=1
     if self.failure_since is None:self.failure_since=at
     self.last_failure={'utc':self.wall().isoformat(),'cycle_seconds':max(0.,at-c['start']),'rest_hold_seconds':c['rest_hold_seconds'],'request_count':c['request_count'],'exception_type':c['original_type']}
     self._log('R109_NATIVE_FAILED',**self.last_failure,consecutive_failures=self.consecutive,total_failures_UTC_day=self.total)
     if self.consecutive>=3:self._untrust('CONSECUTIVE_FAILURES_3')
     base=self.last_success if self.last_success is not None else self.active_since
     if base is not None and at-base>=90.:self._untrust('NO_SUCCESS_90S')
     self._fatal_if_due(at)
    return None # Only this transport failure takes the original 5-second pause.
   with self.lock:
    self._fatal_if_due(self.clock());self._day();self.last_success=self.clock();self.last_success_utc=self.wall().isoformat();self.successes+=1;self.consecutive=0;self.failure_since=None
    if not self.trusted:self.trusted=True;self._log('R109_NATIVE_TRUST',native_trusted=True,reason='NATIVE_CYCLE_SUCCEEDED',consecutive_failures=0)
   return result
  finally:
   c['transport_error']=None # Do not retain response/traceback frames after a contained failure.
   self.local.cycle=prior
 def snapshot(self):
  with self.lock:
   self._day()
   return {'enabled':True,'session':self.session,'UTC_day':self.day,'native_trusted':self.trusted,'consecutive_failures':self.consecutive,'total_failures_UTC_day':self.total,'successful_cycles_UTC_day':self.successes,'last_success_utc':self.last_success_utc,'last_failure':dict(self.last_failure) if self.last_failure else None,'observer_errors':self.observer_errors,'untrusted_after_consecutive_failures':3,'untrusted_after_no_success_seconds':90,'fatal_after_continuous_failure_seconds':600,'scope':'native only; per-process UTC-day totals; no retries; existing pause=5s'}

def resilience_for(runtime,api,bundle):
 if not review_flags.NATIVE_FAILURE_NONFATAL_ENABLED:return None
 m=getattr(runtime,'native_resilience',None)
 if m is None:
  m=NativeResilience(api,runtime.a.bridge,bundle.get('session','UNKNOWN'));runtime.native_resilience=m
  measure=getattr(getattr(getattr(runtime.a.bridge,'p',None),'rest',None),'measure',None)
  if measure is not None:measure.native_resilience=m
 return m

async def watch_native_health(runtime,api,bundle,pause):
 """Check deadlines even while the native coroutine awaits its worker.

This timer adds no request, retry, worker, or native-loop backoff. A due fatal
propagates through the existing task failure latch and main IPR_FATAL path.
 """
 from .data import dt
 m=resilience_for(runtime,api,bundle)
 while not runtime.stop.is_set():
  market=bundle['market'];now=runtime.wall()
  m.tick(bool(market and dt(market['open'])<=now<dt(market['close'])))
  await pause(runtime.stop,1.)
