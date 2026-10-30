import json
from state_store import canonical_json,validate_record,record_key,CanonicalConflict
class RedisCanonicalBackend:
 def __init__(self,r):self.r=r
 def get(self,k):return self.r.get(k)
 def set_if_absent(self,k,v):return bool(self.r.set(k,v,nx=True))
 def compare_and_set(self,k,expected,v):
  # Not used for decision-changing atomic transitions in production.
  if self.r.get(k)!=expected:return False
  self.r.set(k,v);return True
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
