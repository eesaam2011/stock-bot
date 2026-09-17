class RedisLuaError(RuntimeError): pass
class LeaseLost(RedisLuaError): pass
class AtomicConflict(RedisLuaError): pass

ACQUIRE_LEASE_LUA = """
local leader=KEYS[1]
local genkey=KEYS[2]
local owner=ARGV[1]
local ttl=tonumber(ARGV[2])
if redis.call('EXISTS',leader)==1 then return {0,redis.call('GET',leader),redis.call('GET',genkey) or '0'} end
local g=redis.call('INCR',genkey)
redis.call('SET',leader,owner,'EX',ttl)
return {1,owner,tostring(g)}
"""

RENEW_LEASE_LUA = """
if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end
redis.call('EXPIRE',KEYS[1],tonumber(ARGV[2]))
return 1
"""

DELETE_LEASE_LUA = """
if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end
return redis.call('DEL',KEYS[1])
"""

ATOMIC_ENTRY_LUA = """
local leader=KEYS[1]
local opp=KEYS[2]
local trade=KEYS[3]
local outbox=KEYS[4]
if redis.call('GET',leader)~=ARGV[1] then return -10 end
if redis.call('GET',opp)~=ARGV[2] then return -20 end
local t=redis.call('GET',trade)
if t and t~=ARGV[4] then return -30 end
local o=redis.call('GET',outbox)
if o and o~=ARGV[5] then return -40 end
redis.call('SET',opp,ARGV[3])
redis.call('SET',trade,ARGV[4])
redis.call('SET',outbox,ARGV[5])
return 1
"""

ATOMIC_TRADE_EVENT_LUA = """
local leader=KEYS[1]
local trade=KEYS[2]
local outbox=KEYS[3]
if redis.call('GET',leader)~=ARGV[1] then return -10 end
if redis.call('GET',trade)~=ARGV[2] then return -20 end
local o=redis.call('GET',outbox)
if o and o~=ARGV[4] then return -30 end
redis.call('SET',trade,ARGV[3])
redis.call('SET',outbox,ARGV[4])
return 1
"""

class ProductionRedisLua:
    def __init__(self,redis_client,prefix="operational_priority_radar:v1"):
        self.r=redis_client;self.prefix=prefix
    @property
    def leader_key(self):return f"{self.prefix}:runtime:leader"
    @property
    def generation_key(self):return f"{self.prefix}:runtime:leader_generation"
    def acquire(self,worker_id,ttl=30):
        v=self.r.eval(ACQUIRE_LEASE_LUA,2,self.leader_key,self.generation_key,worker_id,ttl)
        return bool(int(v[0])),int(v[2])
    def renew(self,worker_id,ttl=30):
        if int(self.r.eval(RENEW_LEASE_LUA,1,self.leader_key,worker_id,ttl))!=1:raise LeaseLost()
    def release(self,worker_id):
        return bool(int(self.r.eval(DELETE_LEASE_LUA,1,self.leader_key,worker_id)))
    def atomic_entry(self,worker_id,opp_key,expected_raw,new_raw,trade_key,trade_raw,outbox_key,outbox_raw):
        rc=int(self.r.eval(ATOMIC_ENTRY_LUA,4,self.leader_key,opp_key,trade_key,outbox_key,worker_id,expected_raw,new_raw,trade_raw,outbox_raw))
        if rc==-10:raise LeaseLost()
        if rc<0:raise AtomicConflict(str(rc))
        return True
    def atomic_trade_event(self,worker_id,trade_key,expected_raw,new_raw,outbox_key,outbox_raw):
        rc=int(self.r.eval(ATOMIC_TRADE_EVENT_LUA,3,self.leader_key,trade_key,outbox_key,worker_id,expected_raw,new_raw,outbox_raw))
        if rc==-10:raise LeaseLost()
        if rc<0:raise AtomicConflict(str(rc))
        return True
