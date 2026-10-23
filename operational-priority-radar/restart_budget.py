"""New per-session safety ledger. No production calls occur on import.
Never reuses/modifies the leadership lease key or its TTL.
"""
from dataclasses import dataclass
from datetime import datetime,timezone
import hashlib,json

UNTRUSTED_AFTER_ACK_SEC=120.0
MAX_RESTARTS=3
EXIT_RESTART_REQUESTED=75

@dataclass(frozen=True)
class SessionPolicy:
    session:str
    start_utc:str
    end_utc:str
    frozen_session:str=''
    frozen_manifest_sha256:str=''
    frozen_count:int=0

    @staticmethod
    def parse_dt(value):
        x=datetime.fromisoformat(value.replace('Z','+00:00'))
        if x.tzinfo is None or x.utcoffset().total_seconds()!=0:raise ValueError('UTC_REQUIRED')
        return x

    def validate(self,env):
        start=self.parse_dt(self.start_utc);end=self.parse_dt(self.end_utc)
        day=datetime.fromisoformat(self.session).date().isoformat()
        if self.session!=day or start.date().isoformat()!=day or end.date().isoformat()!=day or not start<end:raise ValueError('SESSION_POLICY_MISMATCH')
        for key,expected in [('OPR_SESSION',self.session),('OPR_SESSION_START_UTC',self.start_utc),('OPR_SESSION_END_UTC',self.end_utc)]:
            value=env.get(key)
            if value and (value!=expected if key=='OPR_SESSION' else self.parse_dt(value)!=self.parse_dt(expected)):raise ValueError('FIXED_ENV_SESSION_MISMATCH:'+key)
        if env.get('OPR_SHADOW_MODE','true').lower()!='true':raise ValueError('SHADOW_ONLY')
        return self

    @property
    def end_epoch(self):return self.parse_dt(self.end_utc).timestamp()
    @property
    def day_epoch(self):return datetime.fromisoformat(self.session).replace(tzinfo=timezone.utc).timestamp()
    @property
    def digest(self):
        raw=json.dumps({'session':self.session,'start':self.start_utc,'end':self.end_utc,'T':120,'max_restarts':MAX_RESTARTS,'frozen_session':self.frozen_session,'frozen_manifest_sha256':self.frozen_manifest_sha256,'frozen_count':self.frozen_count},sort_keys=True,separators=(',',':')).encode()
        return hashlib.sha256(raw).hexdigest()
    @property
    def key(self):return 'operational_priority_radar:v1:runtime:self_stop_budget:'+self.session
    def time_allows(self,now):return self.day_epoch<=now<self.end_epoch

# One hash, no expiry: a process restart must not reset its budget.
# CLAIM counts ALL application boots (initial + at most three retries), not only watchdog exits.
BUDGET_LUA="""
local now=tonumber(redis.call('TIME')[1])
local operation=ARGV[1]
local policy=ARGV[2]
local boot=ARGV[3]
local day=tonumber(ARGV[4])
local finish=tonumber(ARGV[5])
local maximum=tonumber(ARGV[6])
if now<day or now>=finish then return {'SESSION_CLOSED',0} end
local old=redis.call('HGET',KEYS[1],'policy_sha256')
if old and old~=policy then return {'POLICY_CONFLICT',0} end
local initialized=false
if not old then
  if operation~='INITIALIZE' and operation~='CLAIM' then return {'LEDGER_MISSING',0} end
  if redis.call('EXISTS',KEYS[1])~=0 then return {'LEDGER_INVALID',0} end
  redis.call('HSET',KEYS[1],'policy_sha256',policy,'boots',0,'restart_requests',0)
  if operation=='INITIALIZE' then return {'INITIALIZED',0} end
  initialized=true
end
local boots=tonumber(redis.call('HGET',KEYS[1],'boots'))
local requests=tonumber(redis.call('HGET',KEYS[1],'restart_requests'))
if not boots or not requests or boots~=boots or requests~=requests or boots~=math.floor(boots) or requests~=math.floor(requests) or boots<0 or boots>maximum or requests<0 or requests>maximum-1 then return {'LEDGER_INVALID',0} end
if operation=='INITIALIZE' then return {'EXISTS',boots} end
local seen=redis.call('HGET',KEYS[1],'boot:'..boot)
if operation=='CLAIM' then
  if seen then return {'ALLOW',tonumber(seen)} end
  if boots>=maximum then return {'BUDGET_EXHAUSTED',boots} end
  boots=boots+1
  redis.call('HSET',KEYS[1],'boots',boots,'boot:'..boot,boots)
  if initialized then return {'INITIALIZED_BY_FIRST_CLAIM',boots} end
  return {'ALLOW',boots}
elseif operation=='RESTART' then
  if not seen then return {'BOOT_UNCLAIMED',boots} end
  if boots>=maximum or requests>=maximum-1 then return {'BUDGET_EXHAUSTED',boots} end
  if not redis.call('HGET',KEYS[1],'requested:'..boot) then
    redis.call('HSET',KEYS[1],'requested:'..boot,1,'restart_requests',requests+1)
  end
  return {'ALLOW',boots}
end
return {'INVALID_OPERATION',boots}
"""

class RedisBudget:
    def __init__(self,client):self.client=client
    def call(self,operation,policy,boot_id):
        result=self.client.eval(BUDGET_LUA,1,policy.key,operation,policy.digest,boot_id,int(policy.day_epoch),int(policy.end_epoch),MAX_RESTARTS+1)
        if not isinstance(result,(list,tuple)) or len(result)!=2:raise RuntimeError('BUDGET_RESPONSE_INVALID')
        status=result[0].decode() if isinstance(result[0],bytes) else result[0]
        if not isinstance(status,str):raise RuntimeError('BUDGET_STATUS_INVALID')
        return status,int(result[1])
    def initialize(self,policy):return self.call('INITIALIZE',policy,'initializer')
    def claim(self,policy,boot_id):return self.call('CLAIM',policy,boot_id)
    def restart(self,policy,boot_id):return self.call('RESTART',policy,boot_id)
