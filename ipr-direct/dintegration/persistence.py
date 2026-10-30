"""Production Redis namespace adapter only; no local state fallback."""
class StateConflict(RuntimeError):pass
class NamespacedRedis:
 """Translate physical keys, not canonical record identities. Never fallback to old keys."""
 def __init__(self,r,prefix='ipr_direct:v1:canonical:'):
  if not prefix.startswith('ipr_direct:v1:'):raise StateConflict('NAMESPACE_FORBIDDEN')
  self.r=r;self.prefix=prefix
 def get(self,k):return self.r.get(self.prefix+k)
 def set(self,k,v,**kw):return self.r.set(self.prefix+k,v,**kw)
 def mget(self,ks):return self.r.mget([self.prefix+k for k in ks])
 def scan(self,cursor=0,match='*',count=500):
  cursor,ks=self.r.scan(cursor=cursor,match=self.prefix+match,count=count)
  return cursor,[(k.decode() if isinstance(k,bytes) else k)[len(self.prefix):] for k in ks]
 def eval(self,script,nkeys,*args):return self.r.eval(script,nkeys,*[self.prefix+k for k in args[:nkeys]],*args[nkeys:])
 def time(self):return self.r.time()
