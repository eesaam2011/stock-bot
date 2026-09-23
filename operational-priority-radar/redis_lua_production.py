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
local genkey=KEYS[2]
local opp=KEYS[3]
local trade=KEYS[4]
local outbox=KEYS[5]
if redis.call('GET',leader)~=ARGV[1] then return -10 end
if tostring(redis.call('GET',genkey) or '0')~=ARGV[6] then return -11 end
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
local genkey=KEYS[2]
local trade=KEYS[3]
local outbox=KEYS[4]
if redis.call('GET',leader)~=ARGV[1] then return -10 end
if tostring(redis.call('GET',genkey) or '0')~=ARGV[5] then return -11 end
if redis.call('GET',trade)~=ARGV[2] then return -20 end
local o=redis.call('GET',outbox)
if o and o~=ARGV[4] then return -30 end
redis.call('SET',trade,ARGV[3])
redis.call('SET',outbox,ARGV[4])
return 1
"""

# Step 3D: validate every outbox before writing any recovered state.
ATOMIC_TRADE_RECOVERY_LUA = """
local leader=KEYS[1]
local genkey=KEYS[2]
local trade=KEYS[3]
local n=#KEYS-3
if n<1 or n>4 or #ARGV~=4+n then return -40 end
if redis.call('GET',leader)~=ARGV[1] then return -10 end
if tostring(redis.call('GET',genkey) or '0')~=ARGV[4] then return -11 end
if redis.call('GET',trade)~=ARGV[2] then return -20 end
local seen={}
for i=1,n do
 local k=KEYS[3+i]
 if seen[k] or k==trade or k==leader or k==genkey then return -50 end
 seen[k]=true
 local existing=redis.call('GET',k)
 if existing and existing~=ARGV[4+i] then return -30 end
end
redis.call('SET',trade,ARGV[3])
for i=1,n do
 redis.call('SET',KEYS[3+i],ARGV[4+i])
end
return 1
"""

# Step3L: low-level E/B batch primitive; production recovery stays disabled.
ATOMIC_EB_BATCH_LUA = """
local n=#KEYS-2
if n<1 or n>80 or #ARGV~=3+n then return -40 end
if redis.call('GET',KEYS[1])~=ARGV[1] then return -10 end
if tostring(redis.call('GET',KEYS[2]) or '0')~=ARGV[2] then return -11 end
local seen={}
for i=1,n do
 local k=KEYS[i+2]
 if seen[k] or k==KEYS[1] or k==KEYS[2] then return -50 end
 seen[k]=true
 local existing=redis.call('GET',k)
 if existing and existing~=ARGV[i+3] then return -20 end
end
local inserted=0
for i=1,n do
 if redis.call('SET',KEYS[i+2],ARGV[i+3],'NX') then inserted=inserted+1 end
end
return inserted
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
    @staticmethod
    def _assert_payload_generation(expected_generation,*records):
        import json
        try:
            expected=int(expected_generation)
            if any(int(json.loads(raw)["leader_generation"])!=expected for raw in records):
                raise AtomicConflict("PAYLOAD_GENERATION_MISMATCH")
        except (TypeError,ValueError,KeyError,UnicodeError) as exc:
            raise AtomicConflict("INVALID_GENERATION_PAYLOAD") from exc
        return str(expected)
    def atomic_entry(self,worker_id,opp_key,expected_raw,new_raw,trade_key,trade_raw,outbox_key,outbox_raw,expected_generation):
        g=self._assert_payload_generation(expected_generation,new_raw,trade_raw,outbox_raw)
        rc=int(self.r.eval(ATOMIC_ENTRY_LUA,5,self.leader_key,self.generation_key,opp_key,trade_key,outbox_key,
                           worker_id,expected_raw,new_raw,trade_raw,outbox_raw,g))
        if rc in (-10,-11):raise LeaseLost(f"ENTRY_FENCED:{rc}")
        if rc<0:raise AtomicConflict(str(rc))
        return True
    def atomic_trade_recovery(self,worker_id,trade_key,expected_raw,new_raw,
                              outboxes,expected_generation):
        """One fenced transaction: final trade and 1..4 recovered outboxes."""
        if (not isinstance(outboxes,(list,tuple)) or not 1<=len(outboxes)<=4
            or any(not isinstance(x,(list,tuple)) or len(x)!=2 for x in outboxes)):
            raise AtomicConflict("INVALID_RECOVERY_OUTBOX_BATCH")
        keys=[x[0] for x in outboxes]
        if (any(not isinstance(k,str) or not k for k in keys)
            or len(set(keys))!=len(keys)
            or trade_key in keys or self.leader_key in keys
            or self.generation_key in keys):
            raise AtomicConflict("INVALID_RECOVERY_OUTBOX_KEYS")
        raws=[x[1] for x in outboxes]
        g=self._assert_payload_generation(expected_generation,new_raw,*raws)
        rc=int(self.r.eval(
            ATOMIC_TRADE_RECOVERY_LUA,3+len(outboxes),
            self.leader_key,self.generation_key,trade_key,*keys,
            worker_id,expected_raw,new_raw,g,*raws))
        if rc in (-10,-11):raise LeaseLost(f"RECOVERY_FENCED:{rc}")
        if rc<0:raise AtomicConflict(f"RECOVERY_CONFLICT:{rc}")
        if rc!=1:raise RedisLuaError(f"RECOVERY_UNEXPECTED:{rc}")
        return True
    def _atomic_eb_batch_testonly(self,worker_id,records,expected_generation):
        """Low-level Lua test harness; no production recovery coverage gate."""
        from state_store import validate_record,record_key,canonical_json,SchemaError
        if (not isinstance(worker_id,str) or not worker_id
            or not isinstance(expected_generation,int)
            or isinstance(expected_generation,bool) or expected_generation<1
            or not isinstance(records,(tuple,list)) or not 1<=len(records)<=80):
            raise AtomicConflict("INVALID_EB_BATCH")
        keys=[];raws=[]
        for record in records:
            if not isinstance(record,dict) or record.get("record_type") not in ("early_core","base_ready"):
                raise AtomicConflict("INVALID_EB_RECORD_TYPE")
            try:validate_record(record,record["record_type"])
            except (SchemaError,TypeError,ValueError) as exc:
                raise AtomicConflict("INVALID_EB_SCHEMA") from exc
            if (record.get("worker_instance_id")!=worker_id
                or type(record.get("leader_generation")) is not int
                or record["leader_generation"]!=expected_generation):
                raise AtomicConflict("EB_PAYLOAD_LEADERSHIP_MISMATCH")
            keys.append(record_key(record));raws.append(canonical_json(record))
        if len(set(keys))!=len(keys) or self.leader_key in keys or self.generation_key in keys:
            raise AtomicConflict("DUPLICATE_OR_RESERVED_EB_KEY")
        rc=int(self.r.eval(ATOMIC_EB_BATCH_LUA,2+len(keys),
            self.leader_key,self.generation_key,*keys,
            worker_id,str(expected_generation),"RESERVED",*raws))
        if rc in (-10,-11):raise LeaseLost(f"EB_BATCH_FENCED:{rc}")
        if rc<0:raise AtomicConflict(f"EB_BATCH_CONFLICT:{rc}")
        if rc>len(keys):raise RedisLuaError("EB_BATCH_UNEXPECTED_RESULT")
        return {"inserted":rc,"already_identical":len(keys)-rc}
    def atomic_recovery_eb(self,*args,**kwargs):
        """No production E/B recovery writes until independent coverage proof."""
        raise AtomicConflict("RECOVERY_EB_COVERAGE_GATE_NOT_IMPLEMENTED")
    def atomic_trade_event(self,worker_id,trade_key,expected_raw,new_raw,outbox_key,outbox_raw,expected_generation):
        g=self._assert_payload_generation(expected_generation,new_raw,outbox_raw)
        rc=int(self.r.eval(ATOMIC_TRADE_EVENT_LUA,4,self.leader_key,self.generation_key,trade_key,outbox_key,
                           worker_id,expected_raw,new_raw,outbox_raw,g))
        if rc in (-10,-11):raise LeaseLost(f"TRADE_FENCED:{rc}")
        if rc<0:raise AtomicConflict(str(rc))
        return True
