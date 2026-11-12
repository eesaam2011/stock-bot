import json
from state_store import canonical_json,validate_record,record_key,CanonicalConflict
class RedisCanonicalBackend:
 def __init__(self,r):self.r=r
 def get(self,k):return self.r.get(k)
 def set_if_absent(self,k,v):return bool(self.r.set(k,v,nx=True))
 def set_if_absent_fenced(self,k,v,worker_id,generation):
  # Owner, generation and SET NX are checked in one Redis Lua operation.
  from redis_lua_production import LeaseLost
  script="""
  if redis.call('GET',KEYS[1])~=ARGV[1] then return -10 end
  if tostring(redis.call('GET',KEYS[2]) or '0')~=ARGV[2] then return -11 end
  if redis.call('SET',KEYS[3],ARGV[3],'NX') then return 1 end
  return 0
  """
  rc=int(self.r.eval(script,3,'operational_priority_radar:v1:runtime:leader',
                     'operational_priority_radar:v1:runtime:leader_generation',
                     k,worker_id,str(int(generation)),v))
  if rc in (-10,-11):raise LeaseLost(f'CANONICAL_WRITE_FENCED:{rc}')
  if rc not in (0,1):raise RuntimeError(f'CANONICAL_WRITE_UNEXPECTED:{rc}')
  return rc==1
 def compare_and_set(self,k,expected,v):
  # Unfenced GET+SET can overwrite a successor leader.
  raise RuntimeError('UNFENCED_CANONICAL_COMPARE_AND_SET_FORBIDDEN')
class RedisLeadershipFacade:
 def __init__(self,lua,worker_id):
  self.lua=lua;self.worker_id=worker_id;self.generation=None
 def sync_generation(self):
  self.generation=int(self.lua.r.get(self.lua.generation_key) or 0);return self.require_current()
 def require_current(self):
  owner=self.lua.r.get(self.lua.leader_key)
  if owner!=self.worker_id:raise RuntimeError("NOT_CURRENT_LEADER")
  g=int(self.lua.r.get(self.lua.generation_key) or 0)
  if self.generation is None:self.generation=g
  if g!=self.generation:raise RuntimeError("STALE_LEADER_GENERATION")
  class T:pass
  t=T();t.worker_instance_id=self.worker_id;t.leader_generation=g;return t
