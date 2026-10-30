from dataclasses import dataclass
from collections import deque
from market_trust import Trust,transition,new_entries_allowed
from redis_lua_production import ProductionRedisLua
class RuntimeFailClosed(RuntimeError):pass
class BoundedPriorityQueue:
 def __init__(self,maxlen=10000):self.maxlen=maxlen;self.q=deque()
 def put(self,item,priority):
  if len(self.q)>=self.maxlen:
   worst=max(p for p,_ in self.q)
   if priority>=worst:return False
   for i,(p,_) in enumerate(self.q):
    if p==worst:del self.q[i];break
  self.q.append((priority,item));return True
 def get(self):
  if not self.q:raise IndexError
  i=min(range(len(self.q)),key=lambda j:self.q[j][0]);p,x=self.q[i];del self.q[i];return x
 def __len__(self):return len(self.q)
@dataclass
class RuntimeComponents:
 redis_client:object;sip:object;rest:object;early_core:object;base_ready:object;outbox:object;resource_guard:object;recovery:object
class RuntimeOrchestrator:
 def __init__(self,c,worker_id,queue_max=10000):
  self.c=c;self.worker_id=worker_id;self.redis=ProductionRedisLua(c.redis_client);self.trust=Trust();self.queue=BoundedPriorityQueue(queue_max)
 def acquire_leadership(self):
  ok,g=self.redis.acquire(self.worker_id,30)
  if not ok:raise RuntimeFailClosed("LEADER_NOT_ACQUIRED")
  return g
 def begin_recovery(self):
  self.trust=transition(self.trust,"START_RECOVERY");r=self.c.recovery.run()
  if not r.get("gap_recovered"):raise RuntimeFailClosed("GAP_RECOVERY_FAILED")
  self.trust=transition(self.trust,"GAP_RECOVERED");return r
 def mark_stream_connected(self):
  self.trust=transition(self.trust,"STREAM_CONNECTED");return self.trust.state
 def finish_reconciliation(self,continuity_ok=True):
  self.trust=transition(self.trust,"RECONCILED")
  if not continuity_ok:raise RuntimeFailClosed("CONTINUITY_FAILED")
  self.trust=transition(self.trust,"CONTINUITY_OK");return self.trust.state
 def startup_recovery(self):
  self.trust=transition(self.trust,"START_RECOVERY");r=self.c.recovery.run()
  if not r.get("gap_recovered"):raise RuntimeFailClosed("GAP_RECOVERY_FAILED")
  self.trust=transition(self.trust,"GAP_RECOVERED");self.c.sip.connect_and_subscribe();self.trust=transition(self.trust,"STREAM_CONNECTED")
  if not r.get("reconciled"):raise RuntimeFailClosed("RECONCILIATION_FAILED")
  self.trust=transition(self.trust,"RECONCILED")
  if not self.c.sip.verify_continuity():raise RuntimeFailClosed("CONTINUITY_FAILED")
  self.trust=transition(self.trust,"CONTINUITY_OK");return self.trust.state
 def on_disconnect(self):self.trust=transition(self.trust,"DISCONNECT");return self.trust.state
 def new_decisions_allowed(self):return new_entries_allowed(self.trust) and self.c.resource_guard.new_entries_allowed()
 def route_completed_1m(self,s,b):return self.c.base_ready.on_completed_native_1m(s,b) if self.new_decisions_allowed() else {"accepted":False,"reason":"FAIL_CLOSED"}
 def route_native_5m(self,s,r,a):return self.c.early_core.evaluate(s,r,a) if self.new_decisions_allowed() else {"accepted":False,"reason":"FAIL_CLOSED"}
