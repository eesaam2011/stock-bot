"""Original leadership semantics, isolated physical Redis namespace."""
from leadership import LeadershipManager,NotLeader,StaleLeaderGeneration
from redis_lua_production import ProductionRedisLua
from dintegration.persistence import NamespacedRedis
from .durable import PREFIX
class LeaseBackend:
 def __init__(self,r):
  self.r=NamespacedRedis(r,PREFIX+':canonical:');self.lua=ProductionRedisLua(self.r)
 def acquire(self,worker,ttl):
  ok,g=self.lua.acquire(worker,ttl);return g if ok else None
 def compare_and_renew(self,worker,ttl):
  self.lua.renew(worker,ttl);return True
 def compare_and_delete(self,worker):return self.lua.release(worker)
 def current_identity(self):
  owner,generation=self.r.mget([self.lua.leader_key,self.lua.generation_key])
  return (owner.decode() if isinstance(owner,bytes) else owner,int(generation or 0))
 def current_owner(self):
  v=self.r.get(self.lua.leader_key);return v.decode() if isinstance(v,bytes) else v
 def current_generation(self):return int(self.r.get(self.lua.generation_key) or 0)
class AtomicLeadershipManager(LeadershipManager):
 def require_current(self):
  if self.token is None:raise NotLeader('no leadership token')
  owner,generation=self.backend.current_identity()
  if owner!=self.worker_instance_id:
   self.token=None;raise NotLeader('lease ownership lost')
  if generation!=self.token.leader_generation:raise StaleLeaderGeneration('stale fencing generation')
  return self.token

def leadership(client,worker):return AtomicLeadershipManager(LeaseBackend(client),worker)
