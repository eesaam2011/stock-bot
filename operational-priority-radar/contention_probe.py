"""V6 bounded diagnostics. Never records Redis arguments, values or frame locals."""
import contextlib,threading,time,sys,traceback,queue
from collections import defaultdict
from datetime import datetime,timezone
TLS=threading.local()
BINS=(.0001,.001,.01,.1,.5,1.,2.,3.,5.,10.)
OPS={'on_bar','on_trade','on_status','_memory_probe','memory_stats','poll_native5','refresh_runtime_caches','expire_entries','ingress_BAR','ingress_TRADE','ingress_STATUS','_confluence','_try_entry','expire_entry_opportunities'}
class Probe:
 def __init__(self,session,budget=None):
  self.session=session;self.lock=None;self.stats={};self.guard=threading.Lock();self.requests=queue.Queue(maxsize=5);self.requested=0;self.trigger_keys=set();self.dumped=0;self.budget=budget;self.errors=0
  self.writer=None
 def record(self,kind,name,seconds):
  key=kind+':'+str(name)[:48]
  with self.guard:
   if key not in self.stats and len(self.stats)>=96:key='overflow'
   s=self.stats.setdefault(key,{'count':0,'max_seconds':0.,'sum_seconds':0.,'histogram':[0]*11})
   s['count']+=1;s['sum_seconds']+=seconds
   if seconds>=s['max_seconds']:
    s['max_seconds']=seconds;s['max_at_utc']=datetime.now(timezone.utc).isoformat();s['max_at_monotonic']=time.monotonic()
   s['histogram'][next((i for i,b in enumerate(BINS) if seconds<=b),10)]+=1
 def request(self,reason,token=None):
  # Frame capture is done at trigger time, without waiting for decision lock.
  with self.guard:
   if self.requested>=5 or (token is not None and token in self.trigger_keys):return
   if token is not None:self.trigger_keys.add(token)
   self.requested+=1
  try:
   frames=sys._current_frames();stacks=[]
   for tid,frame in list(frames.items())[:128]:
    stack=[]
    while frame is not None and len(stack)<24:
     stack.append({'file':frame.f_code.co_filename,'line':frame.f_lineno,'function':frame.f_code.co_name});frame=frame.f_back
    stacks.append({'thread_id':tid,'frames':list(reversed(stack))})
   self.requests.put_nowait({'stage':'V6_CONTENTION_STACKS','reason':reason,'session':self.session,'utc':datetime.now(timezone.utc).isoformat(),'trigger_monotonic':time.monotonic(),'owner':self.lock.owner_snapshot() if self.lock else None,'threads':stacks,'omitted_threads':max(0,len(frames)-128),'scope':'up to128 threads; last24 frames/thread; no locals/source text'})
  except Exception:self.errors+=1
 def start(self,logger):
  if self.writer:return
  def work():
   while True:
    row=self.requests.get()
    try:
     if self.budget is None or self.budget():
      import builtins
      threads=row.pop('threads')
      builtins.print({**row,'thread_count':len(threads)},flush=True)
      for item in threads:builtins.print({'stage':'V6_CONTENTION_THREAD','session':self.session,'reason':row['reason'],**item},flush=True)
      self.dumped+=1
    except Exception:self.errors+=1
    finally:self.requests.task_done()
  self.writer=threading.Thread(target=work,name='v6-stack-writer',daemon=True);self.writer.start()
 def snapshot(self):
  with self.guard:return {'histogram_upper_seconds':BINS,'stats':{k:{**v,'histogram':list(v['histogram'])} for k,v in self.stats.items()},'owner':self.lock.owner_snapshot() if self.lock else None,'stack_requests':self.requested,'stack_dumps':self.dumped,'errors':self.errors,'scope':'per-process aggregates; dump budget Redis-gated per session'}
class ObservedRLock:
 def __init__(self,probe):self.raw=threading.RLock();self.probe=probe;probe.lock=self;self.owner=None;self.frames=[]
 @contextlib.contextmanager
 def operation(self,name):
  old=getattr(TLS,'operation',None);TLS.operation=name if name in OPS else 'other'
  try:yield
  finally:TLS.operation=old
 def acquire(self,blocking=True,timeout=-1):
  t=time.monotonic();ok=self.raw.acquire(blocking,timeout)
  if ok:
   name=getattr(TLS,'operation',None) or 'other';self.probe.record('lock_wait',name,time.monotonic()-t)
   self.frames.append((name,time.monotonic()));self.owner=threading.get_ident();TLS.decision_probe=self.probe
  return ok
 def release(self):
  name,t=self.frames.pop();self.probe.record('lock_hold',name,time.monotonic()-t)
  if not self.frames:self.owner=None;TLS.decision_probe=None
  self.raw.release()
 def __enter__(self):self.acquire();return self
 def __exit__(self,*exc):self.release()
 def _is_owned(self):return self.raw._is_owned()
 def owner_snapshot(self):
  frames=list(self.frames)
  return {'thread_id':self.owner,'operation':frames[-1][0] if frames else None,'held_seconds':time.monotonic()-frames[0][1] if frames else 0.,'depth':len(frames)}
def observe_redis(client,probe):
 # Instrument this client execute_command once; registered Script calls it.
 # Separate Pipeline objects/private _execute_command bypass this wrapper.
 if getattr(client,'_v6_instrumented',False):return
 names=['execute_command'] if callable(getattr(client,'execute_command',None)) else ['get','set','mget','eval','evalsha','scan','ttl','incr','hget','hset']
 for name in names:
  original=getattr(client,name,None)
  if not callable(original):continue
  def timed(*a,_original=original,_name=name,**kw):
   t=time.monotonic()
   try:return _original(*a,**kw)
   finally:
    if getattr(TLS,'ingress',False):
     command=str(a[0]).split()[0].upper() if _name=='execute_command' and a else _name
     probe.record('redis',command,time.monotonic()-t)
  setattr(client,name,timed)
 client._v6_instrumented=True

def session_budget(client,session):
 script="local n=redis.call('INCR',KEYS[1]); redis.call('EXPIRE',KEYS[1],172800); return n"
 def claim():
  try:return int(client.eval(script,1,'opr:v6:stack_dump_budget:'+session))<=5
  except Exception:return False # Never reset/guess when Redis is unavailable.
 return claim

def observed(fn):
 from functools import wraps
 @wraps(fn)
 def wrapped(self,*a,**kw):
  t=time.monotonic()
  with self._decision_lock.operation(fn.__name__):
   try:return fn(self,*a,**kw)
   finally:self.contention_probe.record('operation',fn.__name__,time.monotonic()-t)
 return wrapped
