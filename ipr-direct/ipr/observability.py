"""Bounded diagnostics only. Never stores keys, values, credentials, or frame locals."""
import contextlib,math,threading,time
from collections import deque

BINS=(.0001,.0005,.001,.002,.005,.01,.02,.05,.1,.25,.5,1.,2.,5.,10.,30.)
class Diagnostics:
 def __init__(self):
  self.lock=threading.Lock();self.local=threading.local();self.phases={};self.phase_stats={};self.redis={};self.clock=None
  self.loop_heartbeat=None;self.loop_thread=None;self.max_pause=0.;self.pauses=deque(maxlen=8)
 @contextlib.contextmanager
 def phase(self,name):
  tid=threading.get_ident();start=time.monotonic();old=getattr(self.local,'operation',None)
  self.local.operation=(old+'>'+name) if old else name
  with self.lock:prior=self.phases.get(tid);self.phases[tid]=(name,start)
  try:yield
  finally:
   with self.lock:
    if prior is None:self.phases.pop(tid,None)
    else:self.phases[tid]=prior
    key=name if name in self.phase_stats or len(self.phase_stats)<96 else 'overflow'
    d=self.phase_stats.setdefault(key,{'count':0,'total_seconds':0.,'max_seconds':0.})
    elapsed=time.monotonic()-start;d['count']+=1;d['total_seconds']+=elapsed;d['max_seconds']=max(d['max_seconds'],elapsed);d['last_seconds']=elapsed
   self.local.operation=old
 def record_redis(self,command,seconds):
  name=getattr(self.local,'operation',None)
  if name is None:
   from contention_probe import TLS
   name=getattr(TLS,'operation',None) or 'unlabelled'
  key=(command,name)
  with self.lock:
   if key not in self.redis and len(self.redis)>=96:key=('OTHER','overflow')
   d=self.redis.setdefault(key,{'count':0,'sum':0.,'max':0.,'bins':[0]*(len(BINS)+1)})
   d['count']+=1;d['sum']+=seconds;d['max']=max(d['max'],seconds)
   d['bins'][next((i for i,b in enumerate(BINS) if seconds<=b),len(BINS))]+=1
 def tick_loop(self):self.loop_heartbeat=time.monotonic();self.loop_thread=threading.get_ident()
 def snapshot(self):
  now=time.monotonic()
  with self.lock:
   redis=[]
   for (cmd,op),d in sorted(self.redis.items()):
    target=math.ceil(.95*d['count']);total=0;p95=None
    for i,n in enumerate(d['bins']):
     total+=n
     if total>=target:p95=BINS[i] if i<len(BINS) else d['max'];break
    redis.append({'command':cmd,'operation':op,'count':d['count'],'mean_seconds':d['sum']/d['count'],'p95_upper_seconds':p95,'max_seconds':d['max']})
   phases=[{'thread_id':t,'phase':v[0],'elapsed_seconds':now-v[1]} for t,v in self.phases.items()]
   pauses=list(self.pauses)
   phase_stats={key:{**v,'mean_seconds':v['total_seconds']/v['count']} for key,v in self.phase_stats.items()}
  return {'REDIS_RTT':redis,'redis_scope':'Client command elapsed including connection-pool/client wait, excluding application decision/journal lock wait; not pure network RTT. p95 histogram upper bound. No keys/values.',
    'loop_heartbeat_age_seconds':None if self.loop_heartbeat is None else now-self.loop_heartbeat,'loop_thread_id':self.loop_thread,
    'clock_thread':None if self.clock is None else self.clock.heartbeat_snapshot(),'active_phases':phases,'phase_durations':phase_stats,'largest_pause_observed_seconds':self.max_pause,
    'pause_observations':pauses,'phase_is_proven_cause':False}
OBS=Diagnostics()
def observed_phase(name):return OBS.phase(name)

class RedisTelemetry:
 """One measurement per invoked client command; no argument retention."""
 COMMANDS={'get','set','mget','eval','evalsha','hget','hset','hlen','hmget','scan','exists','time','ttl','incr','delete','ping'}
 def __init__(self,raw,diagnostics=OBS):self.raw=raw;self.diagnostics=diagnostics
 def __getattr__(self,name):
  original=getattr(self.raw,name)
  if name not in self.COMMANDS:return original
  def measured(*a,**kw):
   start=time.monotonic()
   try:return original(*a,**kw)
   finally:self.diagnostics.record_redis(name.upper(),time.monotonic()-start)
  return measured

class PauseObserver:
 """Observer can itself be delayed by GIL/throttling; correlation is not attribution."""
 def __init__(self,probe,emit,diagnostics=OBS):self.probe=probe;self.emit=emit;self.d=diagnostics;self.stop=threading.Event();self.thread=None
 def start(self):
  def watch():
   fired=False
   while not self.stop.wait(.1):
    tick=self.d.loop_heartbeat
    if tick is None:continue
    age=time.monotonic()-tick;self.d.max_pause=max(self.d.max_pause,age)
    if age<=.5:fired=False;continue
    if fired:continue
    fired=True;s=self.d.snapshot()
    row={'stage':'LOOP_PAUSE_OBSERVED','loop_heartbeat_age_seconds':age,'active_phases':s['active_phases'],'clock_thread':s['clock_thread'],'cause':'UNPROVEN'}
    with self.d.lock:self.d.pauses.append(row)
    try:self.emit(row)
    except Exception:pass
    if self.probe is not None:self.probe.request('LOOP_HEARTBEAT_OVER_0_5_SECONDS',token=('r7-loop',tick))
  self.thread=threading.Thread(target=watch,name='ipr-pause-observer',daemon=True);self.thread.start()
 def close(self):
  self.stop.set()
  if self.thread:self.thread.join(timeout=1)
