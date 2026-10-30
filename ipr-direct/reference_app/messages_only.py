"""Messages-only adapter. Disabled unless explicitly installed; no broker/trading API.
One HTTP POST maximum per durable event. Credentials are read only from env.
No HTTP/Redis operation occurs on import. No exception/body/URL is logged.
"""
import hashlib,json,os,uuid
from dataclasses import dataclass,field
from datetime import datetime,timezone

TEST_TEXT='TEST — OPR messages-only connectivity test. No trading instruction.'

@dataclass(frozen=True)
class MessageConfig:
    enabled:bool=False
    cap:int=0
    token:str=field(default='',repr=False)
    chat_id:str=field(default='',repr=False)
    @classmethod
    def from_env(cls,env=None):
        env=os.environ if env is None else env
        flag=env.get('OPR_MESSAGES_ONLY_ENABLED','false')
        if flag not in ('true','false'):raise ValueError('MESSAGE_FLAG_INVALID')
        if flag=='false':return cls()
        cap=int(env.get('OPR_MESSAGES_SESSION_CAP','0'))
        if cap<=0:raise ValueError('MESSAGE_CAP_REQUIRED')
        token=env.get('TELEGRAM_BOT_TOKEN','');chat=env.get('TELEGRAM_CHAT_ID','')
        if not token or not chat:raise ValueError('MESSAGE_CREDENTIALS_MISSING')
        return cls(True,cap,token,chat)
    def policy_hash(self,session_policy):
        return hashlib.sha256(json.dumps({'version':3,'session_policy':session_policy.digest,'cap':self.cap,
               'destination_sha256':hashlib.sha256(self.chat_id.encode()).hexdigest(),'delivery':'AT_MOST_ONE_POST','identity':'SESSION_SYMBOL_EVENT_TYPE'},sort_keys=True,separators=(',',':')).encode()).hexdigest()

# Redis hash is intentionally retained, with no TTL or DEL. Durability is a
# deployment precondition, not something proven by this local package.
MESSAGE_LUA='''
local op=ARGV[1]; local policy=ARGV[2]; local eid=ARGV[3]; local attempt=ARGV[4]
local cap=tonumber(ARGV[5]); local start=tonumber(ARGV[6]); local finish=tonumber(ARGV[7])
local worker=ARGV[8]; local generation=ARGV[9]; local outcome=ARGV[10]
local old=redis.call('HGET',KEYS[1],'policy')
if old and old~=policy then return {'POLICY_CONFLICT',0} end
local raw_count=redis.call('HGET',KEYS[1],'count')
if old and not raw_count then return {'LEDGER_INVALID',0} end
if not old and redis.call('EXISTS',KEYS[1])~=0 then return {'LEDGER_INVALID',0} end
local count=tonumber(raw_count or '0')
if not count or count~=count or count<0 or count~=math.floor(count) or count>cap then return {'LEDGER_INVALID',0} end
local state=redis.call('HGET',KEYS[1],'state:'..eid)
if op=='START' then
 local now=tonumber(redis.call('TIME')[1])
 if now<start or now>=finish then return {'SESSION_CLOSED',count} end
 if redis.call('GET',KEYS[2])~=worker or redis.call('GET',KEYS[3])~=generation then return {'NOT_LEADER',count} end
 if state then
  if state=='SEND_STARTED' then
   redis.call('HSET',KEYS[1],'state:'..eid,'UNKNOWN_FINAL')
   return {'UNKNOWN_FINAL',count}
  end
  return {state,count}
 end
 if count>=cap then return {'CAP_REACHED',count} end
 count=count+1
 redis.call('HSET',KEYS[1],'policy',policy,'count',count,'state:'..eid,'SEND_STARTED','attempt:'..eid,attempt)
 return {'ALLOW',count}
elseif op=='FINISH' then
 if not old then return {'LEDGER_MISSING',0} end
 if redis.call('HGET',KEYS[1],'attempt:'..eid)~=attempt then return {'ATTEMPT_CONFLICT',count} end
 if state~='SEND_STARTED' then return {state or 'STATE_MISSING',count} end
 if outcome~='SENT' and outcome~='FAILED_FINAL' and outcome~='UNKNOWN_FINAL' then return {'OUTCOME_INVALID',count} end
 redis.call('HSET',KEYS[1],'state:'..eid,outcome)
 return {outcome,count}
end
return {'OP_INVALID',count}
'''

class RedisMessageLedger:
    def __init__(self,client,config,policy,worker,generation):
        self.client=client;self.config=config;self.policy=policy;self.worker=worker;self.generation=str(generation)
        prefix='operational_priority_radar:v1'
        self.keys=(prefix+':messages_only:'+policy.session,prefix+':runtime:leader',prefix+':runtime:leader_generation')
    def call(self,operation,event_id,attempt,outcome='',test=False):
        start=self.policy.day_epoch if test else self.policy.parse_dt(self.policy.start_utc).timestamp()
        event_hash=hashlib.sha256(event_id.encode()).hexdigest()
        r=self.client.eval(MESSAGE_LUA,3,*self.keys,operation,self.config.policy_hash(self.policy),event_hash,attempt,
              self.config.cap,int(start),int(self.policy.end_epoch),self.worker,self.generation,outcome)
        if not isinstance(r,(tuple,list)) or len(r)!=2:raise ValueError('MESSAGE_LEDGER_RESPONSE')
        state=r[0].decode() if isinstance(r[0],bytes) else r[0]
        if not isinstance(state,str):raise ValueError('MESSAGE_LEDGER_STATE')
        return state,int(r[1])

class TelegramHTTP:
    """Actual HTTPS transport, no redirects or HTTP retries. Socket timeout5s.
    A socket timeout is NOT a strict end-to-end wall-clock deadline.
    """
    def __init__(self,config,connection_factory=None):
        self.config=config
        if connection_factory is None:
            from http.client import HTTPSConnection
            connection_factory=HTTPSConnection
        self.connection_factory=connection_factory
    def post(self,text):
        conn=None
        try:
            import ssl
            conn=self.connection_factory('api.telegram.org',timeout=5,context=ssl.create_default_context())
            body=json.dumps({'chat_id':self.config.chat_id,'text':text},ensure_ascii=False).encode()
            conn.request('POST','/bot'+self.config.token+'/sendMessage',body,{'Content-Type':'application/json'})
            response=conn.getresponse();raw=response.read(65537)
            if len(raw)>65536:return 'UNKNOWN_FINAL'
            data=json.loads(raw)
            if response.status==200 and data.get('ok') is True and isinstance(data.get('result',{}).get('message_id'),int):return 'SENT'
            # Telegram's explicit ok=false is final here; never auto-retry429/5xx.
            return 'FAILED_FINAL' if data.get('ok') is False else 'UNKNOWN_FINAL'
        except Exception:
            return 'UNKNOWN_FINAL' # Never include exception URLs, token, body, or chat.
        finally:
            if conn is not None:
                try:conn.close()
                except Exception:pass

class MessagesOnlySender:
    def __init__(self,config,policy,ledger,transport=None,log=None):
        self.config=config;self.policy=policy;self.ledger=ledger
        self.transport=transport or TelegramHTTP(config)
        self.log=log or (lambda row:print(json.dumps(row,sort_keys=True),flush=True))
        self.cap_reached=False
    def send(self,event,*,trusted=False,not_before=None,test=False):
        if not self.config.enabled:return 'DISABLED'
        if not test and not trusted:return 'UNTRUSTED_BLOCKED'
        if test:
            eid='TEST:'+self.policy.session;text=TEST_TEXT
        else:
            if event.get('session')!=self.policy.session or event.get('state')!='PENDING':return 'OUT_OF_SCOPE'
            if event.get('shadow_mode') is not True or event.get('runtime_mode')!='SHADOW':return 'PROVENANCE_BLOCKED'
            if event.get('event_type') not in {'ENTRY','T1','T2','STOP','POST_T1_EXIT','FINAL'}:return 'TYPE_BLOCKED'
            try:created=datetime.fromisoformat(event['created_at'].replace('Z','+00:00'))
            except Exception:return 'TIME_INVALID'
            if created.tzinfo is None or not_before is None:return 'PRE_TRUST_OR_RESTART_BLOCKED'
            cutoff=max(not_before,self.policy.parse_dt(self.policy.start_utc))
            if created<cutoff:return 'PRE_TRUST_OR_RESTART_BLOCKED'
            outbox_id=event.get('event_id');symbol=event.get('symbol')
            if not isinstance(outbox_id,str) or not outbox_id or not isinstance(symbol,str) or not symbol or ':' in symbol:return 'EVENT_ID_INVALID'
            eid='MESSAGE:'+self.policy.session+':'+symbol+':'+event['event_type']
            # No Markdown parse_mode, no remote formatting, no URLs or trading API.
            from message_template import format_message
            text=format_message(event,created)
            if len(text)>3500:return 'MESSAGE_TOO_LONG'
        attempt=uuid.uuid4().hex
        try:status,count=self.ledger.call('START',eid,attempt,test=test)
        except Exception as exc:
            self.log({'stage':'MESSAGE_LEDGER_ERROR','error_type':type(exc).__name__});return 'LEDGER_UNAVAILABLE'
        self.cap_reached=count>=self.config.cap
        if status!='ALLOW':
            self.log({'stage':'MESSAGE_NOT_SENT','result':status,'session_count':count});return status
        # The durable SEND_STARTED precedes every HTTP request, including TEST.
        try:outcome=self.transport.post(text)
        except BaseException:
            # A simulated/process crash deliberately leaves SEND_STARTED.
            raise
        try:final,count=self.ledger.call('FINISH',eid,attempt,outcome,test=test)
        except Exception as exc:
            self.log({'stage':'MESSAGE_FINALIZE_UNKNOWN','error_type':type(exc).__name__});return 'UNKNOWN_FINAL'
        self.log({'stage':'MESSAGE_ATTEMPT_FINAL','result':final,'session_count':count})
        return final
