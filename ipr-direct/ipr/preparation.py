import json,zlib,base64
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from dintegration.persistence import NamespacedRedis
from r4a.durable import PREFIX
from .classifier import Classifier
from .diagnostics import reason
from .data import dt,digest

def save(redis,key,value):
 raw=json.dumps({'body':value,'sha256':digest(value)},sort_keys=True,separators=(',',':')).encode()
 ttl=7*24*60*60 if key.startswith(('root:','history:','scan:')) else None
 redis.set(PREFIX+':prepared:'+key,base64.b64encode(zlib.compress(raw)).decode(),ex=ttl)
def load(redis,key):
 raw=redis.get(PREFIX+':prepared:'+key)
 if raw is None:return None
 d=json.loads(zlib.decompress(base64.b64decode(raw)))
 if digest(d['body'])!=d['sha256']:raise ValueError('PREP_CACHE_CORRUPT')
 return d['body']

def pins(redis):
 r=NamespacedRedis(redis,PREFIX+':canonical:');out=set()
 for kind,states in [('trade',{'ACTIVE_PRE_T1','ACTIVE_POST_T1','HALTED_ACTIVE'}),('opportunity',{'CONFLUENCE_VALID','ENTRY_DATA_WAIT','ENTRY_PRICE_READY'})]:
  cursor=0
  while True:
   cursor,keys=r.scan(cursor=cursor,match='operational_priority_radar:v1:'+kind+':*',count=200)
   for raw in r.mget(keys):
    if raw:
     d=json.loads(raw)
     if d.get('state') in states:out.add(d['symbol'])
   if int(cursor)==0:break
 # Preserve unresolved U/R from the CURRENT previous journals through canonical opportunities;
 # R with no canonical object is restored by B's current session journal before subscription.
 return out

def prepare_root(api,redis,now):
 day=now.date().isoformat();cached=load(redis,'root:'+day)
 if cached:
  if cached['session']!=day:raise ValueError('PREP_SESSION_MISMATCH')
  return cached
 start=(now.date()-timedelta(days=65)).isoformat();end=(now.date()+timedelta(days=30)).isoformat()
 errors=[];calendar=None;assets=None
 try:
  if load(redis,'calendar_day')==day:calendar=load(redis,'calendar')
  else:calendar=api.calendar(start,end);save(redis,'calendar',calendar);save(redis,'calendar_day',day)
 except Exception as exc:
  errors.append('CALENDAR_FAILED');print(json.dumps({'stage':'ROOT_SOURCE_FAILED','component':'calendar','reason':reason(exc)}),flush=True);calendar=load(redis,'calendar')
 carry_u,carry_r=carried(redis,day)
 active=pins(redis)|carry_u|carry_r
 current=next((c for c in (calendar or []) if c['session']==day),None)
 if current is None and not active:return None
 try:assets=api.assets();save(redis,'assets',{'observed_at':now.isoformat(),'assets':assets})
 except Exception as exc:
  errors.append('ASSETS_FAILED');print(json.dumps({'stage':'ROOT_SOURCE_FAILED','component':'assets','reason':reason(exc)}),flush=True)
 if current is None and not active:return None
 prior=load(redis,'assets')
 if assets is None:assets=prior['assets'] if prior else {}
 cls=Classifier(assets);allowed={s for s,a in assets.items() if cls.eligible(s)}
 if errors:allowed=set() # cached classification may support monitoring, never silently new nominations
 if not allowed and not active:return None
 result={'session':day,'observed_at':now.isoformat(),'assets':assets,'eligible':sorted(allowed),'symbols':sorted(allowed|active),'calendar':calendar or [],'market':current,'new_entries_prepared':not errors and current is not None and 0<(dt(current['close'])-dt(current['open'])).total_seconds()<=23400,'errors':errors}
 save(redis,'root:'+day,result);return result

def carried(redis,day):
 from r4a.durable import RedisState
 from r4b.store import ProofStore
 from dintegration.stream_proof import StreamProof
 keys=[];cursor=0
 while True:
  cursor,ks=redis.scan(cursor=cursor,match=PREFIX+':session:*:checkpoint',count=100)
  keys.extend(k.decode() if isinstance(k,bytes) else k for k in ks)
  if int(cursor)==0:break
 keys=[k for k in keys if k.split(':session:')[1].split(':')[0]<=day]
 if not keys:return set(),set()
 key=sorted(keys)[-1];raw=redis.get(key);head=json.loads(raw);session=head['session']
 state=RedisState(redis,session,head['root_hash'],'read_only',0);state.restore()
 store=ProofStore(state,StreamProof());store.load_archive()
 return set(store.pending_U),set(store.pending_R)
