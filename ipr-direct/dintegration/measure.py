import os,time,json,asyncio,resource,threading
from collections import defaultdict
class Measurements:
 def __init__(self):self.guard=threading.RLock();self.spans=defaultdict(lambda:{'count':0,'total':0.,'max':0.});self.last_cpu=None;self.last_wall=None
 def observe(self,name,seconds):
  if seconds<0:raise ValueError('CLOCK_REVERSED')
  with self.guard:
   d=self.spans[name];d['count']+=1;d['total']+=seconds;d['max']=max(d['max'],seconds)
 def sample(self):
  now=time.monotonic();cpu=time.process_time();rss=None;cg=None;throttling=None
  try:
   for line in open('/proc/self/status'):
    if line.startswith('VmRSS:'):rss=int(line.split()[1])*1024
  except OSError:pass
  try:cg=int(open('/sys/fs/cgroup/memory.current').read())
  except OSError:pass
  try:throttling=open('/sys/fs/cgroup/cpu.stat').read()
  except OSError:pass
  memory_stat={};sqlite_files=[];sqlite_stat_error=None
  try:
   memory_stat=dict(line.split() for line in open('/sys/fs/cgroup/memory.stat'))
   memory_stat={k:int(memory_stat[k]) for k in ('anon','file','shmem')}
  except (OSError,KeyError,ValueError):memory_stat={}
  try:
   from pathlib import Path
   sqlite_files=[{'file':p.name,'bytes':p.stat().st_size} for p in Path('/tmp').glob('ipr_rth_*.sqlite*') if p.is_file()]
  except OSError as exc:sqlite_files=None;sqlite_stat_error=type(exc).__name__
  cores=(cpu-self.last_cpu)/(now-self.last_wall) if self.last_wall is not None and now>self.last_wall else None;self.last_cpu=cpu;self.last_wall=now
  with self.guard:span_copy={k:{'count':v['count'],'mean':v['total']/v['count'],'max':v['max']} for k,v in self.spans.items()}
  from ipr.observability import OBS
  from ipr.telemetry_safe import call
  recovery_io=call(getattr(self,'recovery_io',None),'snapshot')
  result={'native_resilience':call(getattr(self,'native_resilience',None),'snapshot'),'native_fast_timing':call(getattr(self,'native_timing',None),'snapshot'),'R7_diagnostics':OBS.snapshot(),'memory_stat':memory_stat,'anon_bytes':memory_stat.get('anon'),'file_bytes':memory_stat.get('file'),'shmem_bytes':memory_stat.get('shmem'),'sqlite_files':sqlite_files,'sqlite_total_bytes':sum(x['bytes'] for x in sqlite_files) if sqlite_files is not None else None,'sqlite_stat_error':sqlite_stat_error,'sample_monotonic':now,'rss_bytes':rss,'cgroup_memory_bytes':cg,'rss_peak_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'cpu_cores_interval':cores,'cpu_stat':throttling,'spans':span_copy,'scope':'local observed process; not Render or wire latency','latency193':'ASSUMPTION_NOT_MEASURED'}
  if recovery_io is not None:result['recovery_io']=recovery_io
  return result
 async def loop_lag(self,stop,period=.1):
  while not stop.is_set():
   from ipr.observability import OBS
   OBS.tick_loop();start=time.monotonic();self.loop_tick=start;await asyncio.sleep(period);self.loop_last=max(0,time.monotonic()-start-period);self.observe('loop_lag',self.loop_last)
 def current_loop_lag(self):
  tick=getattr(self,'loop_tick',None);last=getattr(self,'loop_last',0.)
  return None if tick is None else max(last,time.monotonic()-tick-.1,0.)
 def export(self,path):
  with open(path,'a') as f:f.write(json.dumps(self.sample(),allow_nan=False)+'\n')
