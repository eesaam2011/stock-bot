"""Fenced, CAS-protected Redis epoch/checkpoint. No SQLite production fallback."""
import hashlib,json,re
from dintegration.persistence import StateConflict,NamespacedRedis
from restart_budget import RedisBudget
PREFIX='ipr_direct:v1'
def canonical(o):return json.dumps(o,sort_keys=True,separators=(',',':'),allow_nan=False)
def sha(s):return hashlib.sha256(s.encode()).hexdigest()
# Validate all types/integers before mutation: Redis Lua failures do NOT roll back writes.
APPEND_LUA='''
local function typ(k) return redis.call('TYPE',k).ok end
if typ(KEYS[1])~='string' or typ(KEYS[2])~='string' then return -10 end
if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('GET',KEYS[2])~=ARGV[2] then return -10 end
for i=3,4 do local t=typ(KEYS[i]);if t~='none' and t~='string' then return -30 end end
local t=typ(KEYS[5]);if t~='none' and t~='hash' then return -30 end
local current=redis.call('GET',KEYS[3]) or '0'
if current~=ARGV[3] then return -20 end
local n=tonumber(current)
if not n or n<0 or n~=math.floor(n) then return -30 end
local next=tostring(n+1)
redis.call('HSET',KEYS[5],next,ARGV[4])
redis.call('SET',KEYS[3],next)
redis.call('SET',KEYS[4],ARGV[4])
return n+1
'''
READ_LUA='''
local raw=redis.call('GET',KEYS[2])
local seq=redis.call('GET',KEYS[1])
if not raw and not seq and redis.call('EXISTS',KEYS[3])==0 then return {} end
if not raw or not seq then return {'CORRUPT'} end
local n=tonumber(seq)
if not n or n<1 or n~=math.floor(n) then return {'CORRUPT'} end
if redis.call('HLEN',KEYS[3])~=n then return {'CORRUPT'} end
if redis.call('HGET',KEYS[3],seq)~=raw then return {'CORRUPT'} end
return {seq,raw}
'''
class RedisState:
 def __init__(self,client,session,root_hash,worker,generation,prefix=PREFIX):
  if prefix!=PREFIX:raise StateConflict('FIXED_NAMESPACE_REQUIRED')
  if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',session):raise StateConflict('BAD_SESSION')
  if not re.fullmatch('[0-9a-f]{64}',root_hash):raise StateConflict('BAD_ROOT_HASH')
  self.r=client;self.worker=worker;self.generation=generation;self.session=session;self.root_hash=root_hash
  self.base=prefix+':session:'+session;self.leader=prefix+':canonical:operational_priority_radar:v1:runtime:leader';self.gen=prefix+':canonical:operational_priority_radar:v1:runtime:leader_generation'
  self.seq=0;self.previous=None;self.restored=False
 @property
 def keys(self):return [self.base+':epoch_seq',self.base+':checkpoint',self.base+':epochs']
 def restore(self):
  v=self.r.eval(READ_LUA,3,*self.keys)
  if not isinstance(v,(list,tuple)):raise StateConflict('BAD_CHECKPOINT_RESPONSE')
  if not v:self.seq=0;self.previous=None;self.restored=True;return None
  if len(v)!=2:raise StateConflict('CHECKPOINT_CORRUPT')
  seq,raw=v;seq=int(seq);raw=raw.decode() if isinstance(raw,bytes) else raw
  # Full chain verification; no flag from old process is granted as current trust.
  previous=None
  for i in range(1,seq+1):
   b=self.r.hget(self.keys[2],str(i));b=b.decode() if isinstance(b,bytes) else b
   if b is None:raise StateConflict('EPOCH_MISSING')
   x=json.loads(b);h=x.pop('sha256',None)
   if x.get('sequence')!=i or x.get('previous_sha256')!=previous or sha(canonical(x))!=h:raise StateConflict('EPOCH_CHAIN_CORRUPT')
   if x.get('session')!=self.session or x.get('root_hash')!=self.root_hash:raise StateConflict('ROOT_OR_SESSION_CONFLICT')
   previous=h
  if b!=raw:raise StateConflict('CHECKPOINT_CHANGED_DURING_RESTORE')
  self.seq=seq;self.previous=previous;self.restored=True
  return {'checkpoint':json.loads(raw),'transport_trusted':False,'requires_recovery':True}
 def append_epoch(self,epoch):
  if not self.restored:raise StateConflict('RESTORE_REQUIRED')
  x={'sequence':self.seq+1,'previous_sha256':self.previous,'session':self.session,'root_hash':self.root_hash,'worker':self.worker,'generation':self.generation,'epoch':epoch}
  h=sha(canonical(x));raw=canonical({**x,'sha256':h})
  v=int(self.r.eval(APPEND_LUA,5,self.leader,self.gen,*self.keys,self.worker,str(self.generation),str(self.seq),raw))
  if v!=self.seq+1:raise StateConflict({-10:'FENCE_LOST',-20:'CHECKPOINT_CAS_CONFLICT',-30:'CHECKPOINT_TYPE_INVALID'}.get(v,'BAD_APPEND_RESPONSE'))
  self.seq=v;self.previous=h;return h
class SessionBudget:
 """Execute original BUDGET_LUA without touching the old service's physical key."""
 def __init__(self,client):self.original=RedisBudget(NamespacedRedis(client,PREFIX+':budget:'))
 def claim(self,p,boot):return self.original.claim(p,boot)
 def restart(self,p,boot):return self.original.restart(p,boot)

