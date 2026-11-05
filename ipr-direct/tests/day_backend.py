"""Disk-backed contract fake for large workloads; small tests execute real Lua.
No Redis server. No network. Does not claim Upstash transaction equivalence.
"""
from pathlib import Path
from r9core.chain import APPEND,StateConflict
class DiskRedis:
 def __init__(self,path):self.path=Path(path);self.values={};self.hashes={};self.fields_read=[]
 def get(self,k):return self.values.get(k)
 def mget(self,ks):return [self.get(k) for k in ks]
 def hget(self,k,f):
  self.fields_read.append((k,f));p=self.hashes.get(k,{}).get(str(f));return p.read_text() if p else None
 def hset(self,k,f,v):
  fields=self.hashes.setdefault(k,{});p=self.path/('row_'+str(sum(len(x) for x in self.hashes.values())))
  if str(f) in fields:raise StateConflict('TEST_OVERWRITE_FORBIDDEN')
  p.write_text(v);fields[str(f)]=p
 def eval(self,script,n,*args):
  keys=list(args[:n]);a=list(args[n:]);a=[str(x) for x in a]
  if script=="return redis.call('EXISTS',KEYS[1])":return int(keys[0] in self.values or keys[0] in self.hashes)
  if script=="return redis.call('HLEN',KEYS[1])":return len(self.hashes.get(keys[0],{}))
  if script!=APPEND:raise StateConflict('DISK_FAKE_SCRIPT_FORBIDDEN')
  owner,gen,head,cp,segment,seal,latest,waiver,oldsegment=keys
  if self.get(owner)!=a[0] or self.get(gen)!=a[1]:return -10
  if (self.get(head) or '')!=a[2]:return -20
  mode=a[3];expected=int(a[13])
  if mode=='rotate':
   if seal in self.values or len(self.hashes.get(oldsegment,{}))!=expected:return -30
  if mode=='append' and len(self.hashes.get(segment,{}))!=expected:return -30
  if mode=='init' and segment in self.hashes:return -30
  if mode=='rotate':self.values[seal]=a[10]
  if mode!='append':self.hset(segment,a[4],a[5])
  self.hset(segment,a[6],a[7]);self.values[cp]=a[9];self.values[head]=a[8];self.values[latest]=a[11]
  first=int(mode=='init' and waiver not in self.values)
  if first:self.values[waiver]=a[12]
  return [1,first]
