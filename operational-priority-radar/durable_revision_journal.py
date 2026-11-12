"""Bounded fenced Redis revision evidence. Pending means blocked, never resolved.

No TTL: a process restart or elapsed time must not erase unresolved revisions.
Absence of this journal is not proof that no historical revisions occurred.
"""
import json
from revision_recovery_inbox import RevisionRecoveryInbox, RevisionInboxUnsafe
from redis_lua_production import LeaseLost

RECORD_LUA = """
if redis.call('GET',KEYS[1])~=ARGV[1] then return -10 end
if redis.call('GET',KEYS[2])~=ARGV[2] then return -11 end
if redis.call('HGET',KEYS[3],'blocked') then return -20 end
local old=redis.call('HGET',KEYS[3],ARGV[3])
if old then
 if old==ARGV[4] then return 0 end
 redis.call('HSET',KEYS[3],'blocked','REVISION_JOURNAL_CONFLICT')
 return -21
end
local count=tonumber(redis.call('HGET',KEYS[3],'count') or '0')
if count>=tonumber(ARGV[5]) then
 redis.call('HSET',KEYS[3],'blocked','REVISION_JOURNAL_OVERFLOW')
 return -22
end
redis.call('HSET',KEYS[3],ARGV[3],ARGV[4],'count',count+1)
return 1
"""


class DurableRevisionJournal:
    def __init__(self,lua,leadership,*,max_records=128):
        if type(max_records) is not int or not 1<=max_records<=128:
            raise ValueError('REVISION_JOURNAL_LIMIT_INVALID')
        self.lua,self.leadership,self.max_records=lua,leadership,max_records
        self.key=lua.prefix+':recovery:unresolved_revisions'

    @staticmethod
    def validate(terminal):
        capture=terminal.get('capture_before_teardown') if isinstance(terminal,dict) else None
        if not isinstance(capture,dict) or 'revision_diagnostic' not in capture:return None
        inbox=RevisionRecoveryInbox();inbox.observe_terminal(terminal)
        return inbox.snapshot()

    def record_terminal(self,terminal):
        value=self.validate(terminal)
        if value is None:return {'recorded':False}
        token=self.leadership.require_current()
        worker=getattr(token,'worker_instance_id',None);generation=getattr(token,'leader_generation',None)
        if not isinstance(worker,str) or not worker or type(generation) is not int or generation<1:
            raise LeaseLost('REVISION_JOURNAL_LEADERSHIP_INVALID')
        raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
        result=int(self.lua.r.eval(RECORD_LUA,3,self.lua.leader_key,self.lua.generation_key,
            self.key,worker,str(generation),'d:'+value['diagnostic_sha256'],raw,self.max_records))
        if result in (-10,-11):raise LeaseLost('REVISION_JOURNAL_FENCED')
        if result<0:raise RevisionInboxUnsafe('REVISION_JOURNAL_BLOCKED:'+str(result))
        return {'recorded':True,'inserted':result==1,'resolved':False}

    def snapshot(self):
        values=self.lua.r.hgetall(self.key)
        if values.get('blocked'):raise RevisionInboxUnsafe(values['blocked'])
        records=[]
        try:
            count=int(values.get('count',0))
            if count<0 or count>self.max_records:raise ValueError()
            for key,raw in values.items():
                if key=='count':continue
                if not key.startswith('d:') or len(raw.encode())>16384:raise ValueError()
                value=json.loads(raw)
                result=self.validate({'epoch':value.get('epoch'),
                    'capture_before_teardown':{'revision_diagnostic':value}})
                if result is None or key!='d:'+result['diagnostic_sha256']:raise ValueError()
                records.append(result)
            if len(records)!=count:raise ValueError()
        except (ValueError,TypeError,AttributeError) as exc:
            raise RevisionInboxUnsafe('REVISION_JOURNAL_CORRUPT') from exc
        return tuple(sorted(records,key=lambda row:row['diagnostic_sha256']))
