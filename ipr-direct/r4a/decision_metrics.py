"""Observational proxy; the ORIGINAL RLock still provides all serialization."""
import threading,time
from contextlib import contextmanager,nullcontext

def operation(lock,name):
 return lock.operation(name) if hasattr(lock,'operation') else nullcontext()

class MeasuredDecisionLock:
 def __init__(self,raw):
  self.raw=raw;self.local=threading.local();self.guard=threading.Lock();self.stats={}
 def __getattr__(self,key):return getattr(self.raw,key)
 @contextmanager
 def operation(self,name):
  old=getattr(self.local,'operation','unlabelled');self.local.operation=name
  try:
   with operation(self.raw,name):yield
  finally:self.local.operation=old
 def acquire(self,blocking=True,timeout=-1):
  start=time.monotonic();ok=self.raw.acquire(blocking,timeout)
  wait=time.monotonic()-start
  with self.guard:
   name='lock_wait:'+getattr(self.local,'operation','unlabelled');r=self.stats.setdefault(name,{'count':0,'total_seconds':0.,'max_seconds':0.});r['count']+=1;r['total_seconds']+=wait;r['max_seconds']=max(r['max_seconds'],wait)
  if ok:
   if not hasattr(self.local,'frames'):self.local.frames=[]
   self.local.frames.append((getattr(self.local,'operation','unlabelled'),time.monotonic()))
  return ok
 def release(self):
  name,at=self.local.frames.pop();elapsed=time.monotonic()-at
  self.raw.release()
  with self.guard:
   if name not in self.stats and len(self.stats)>=96:name='overflow'
   r=self.stats.setdefault(name,{'count':0,'total_seconds':0.,'max_seconds':0.})
   r['count']+=1;r['total_seconds']+=elapsed;r['max_seconds']=max(r['max_seconds'],elapsed)
 def __enter__(self):self.acquire();return self
 def __exit__(self,*exc):self.release()
 def snapshot(self):
  with self.guard:return {k:{**v,'mean_seconds':v['total_seconds']/v['count']} for k,v in self.stats.items()}
