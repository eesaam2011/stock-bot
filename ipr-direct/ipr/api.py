"""Fixed read-only Alpaca endpoints. No order/account mutation interface."""
import json,urllib.request,urllib.parse,urllib.error,threading,time
from datetime import datetime,timezone
from contextlib import contextmanager
class APIError(RuntimeError):pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k):return None
class Alpaca:
 def __init__(self,env,opener=None):
  self.reference_host=urllib.parse.urlparse(env.get('APCA_API_BASE_URL','https://paper-api.alpaca.markets')).hostname
  if self.reference_host not in {'api.alpaca.markets','paper-api.alpaca.markets'}:raise APIError('REFERENCE_HOST_FORBIDDEN')
  self.key=env.get('APCA_API_KEY_ID','');self.secret=env.get('APCA_API_SECRET_KEY','')
  if not self.key or not self.secret:raise APIError('ALPACA_CREDENTIALS_MISSING')
  self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open
  self.lock=threading.Lock();self.requests=0
  self.local=threading.local();self.timing_lock=threading.Lock();self.timing={};self.timing_clock=time.monotonic
 @contextmanager
 def operation(self,name):
  old=getattr(self.local,'name',None);self.local.name=name
  try:yield
  finally:self.local.name=old
 def _operation(self):
  name=getattr(self.local,'name',None)
  if name is None:name=getattr(self,'request_operation',lambda:None)()
  return name if name in {'scan_rest','incremental','native'} else 'other'
 @contextmanager
 def observe_requests(self,observer):
  old=getattr(self.local,'request_observer',None);self.local.request_observer=observer
  try:yield
  finally:self.local.request_observer=old
 def _timing(self,name,wait,hold):
  with self.timing_lock:
   row=self.timing.setdefault(name,{'count':0,'wait_total_seconds':0.,'wait_max_seconds':0.,'hold_total_seconds':0.,'hold_max_seconds':0.})
   row['count']+=1
   for kind,value in [('wait',wait),('hold',hold)]:
    row[kind+'_total_seconds']+=value;row[kind+'_max_seconds']=max(row[kind+'_max_seconds'],value)
 def lock_metrics(self):
  with self.timing_lock:
   return {'operations':{name:{**v,'wait_mean_seconds':v['wait_total_seconds']/v['count'],'hold_mean_seconds':v['hold_total_seconds']/v['count']} for name,v in self.timing.items()},
           'scope':'acquisitions including denied window rechecks; wait excludes heavy-window sleep; hold includes HTTP/read/JSON; no scheduling change'}
 @contextmanager
 def serialized_request(self):
  while True:
   hook=getattr(self,'before_request',None)
   if hook is not None:hook() # wait OUTSIDE the shared API serialization lock
   operation=self._operation();waiting=self.timing_clock();self.lock.acquire();acquired=self.timing_clock()
   try:
    allowed=getattr(self,'request_allowed',None)
    if allowed is not None and not allowed():continue # window crossed while waiting for lock
    yield
    return
   finally:
    released=self.timing_clock();self.lock.release()
    self._timing(operation,max(0.,acquired-waiting),max(0.,released-acquired))
    # Measurement after release; observer is local to this requesting thread.
    observer=getattr(self.local,'request_observer',None)
    if observer is not None:
     try:observer(operation,max(0.,acquired-waiting),max(0.,released-acquired))
     except BaseException:pass # Observer failures never mask an application exception.
 def call(self,host,path,params):
  if (host,path) not in {('paper-api.alpaca.markets','/v2/assets'),('paper-api.alpaca.markets','/v2/calendar'),('api.alpaca.markets','/v2/assets'),('api.alpaca.markets','/v2/calendar'),('data.alpaca.markets','/v2/stocks/bars'),('data.alpaca.markets','/v2/stocks/trades'),('data.alpaca.markets','/v2/stocks/snapshots')}:raise APIError('ENDPOINT_FORBIDDEN')
  req=urllib.request.Request('https://'+host+path+'?'+urllib.parse.urlencode(params),headers={'APCA-API-KEY-ID':self.key,'APCA-API-SECRET-KEY':self.secret},method='GET')
  try:
   with self.serialized_request():
    self.requests+=1
    with self.opener(req,timeout=20) as r:
     if r.status!=200:raise APIError('HTTP_NOT_200')
     raw=r.read(32*1024*1024+1)
     if len(raw)>32*1024*1024:raise APIError('RESPONSE_TOO_LARGE')
     from .observability import observed_phase
     with observed_phase('rest.JSON'):return json.loads(raw)
  except APIError:raise
  except urllib.error.HTTPError as exc:raise APIError('ALPACA_HTTP_'+str(exc.code)) from None
  except Exception:raise APIError('ALPACA_READ_FAILED') from None
 def get(self,path,params):return self.call('data.alpaca.markets',path,params)
 def calendar(self,start,end):
  rows=self.call(self.reference_host,'/v2/calendar',{'start':start,'end':end})
  from zoneinfo import ZoneInfo
  if not isinstance(rows,list):raise APIError('CALENDAR_SCHEMA')
  out=[]
  for r in rows:
   def dt(x):
    value=x if 'T' in x else r['date']+'T'+x
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    return (d if d.tzinfo else d.replace(tzinfo=ZoneInfo('America/New_York'))).astimezone(timezone.utc).isoformat()
   out.append({'session':r['date'],'open':dt(r['open']),'close':dt(r['close'])})
  if [r['session'] for r in out]!=sorted(set(r['session'] for r in out)):raise APIError('CALENDAR_ORDER')
  return out
 def assets(self):
  rows=self.call(self.reference_host,'/v2/assets',{'status':'active','asset_class':'us_equity'})
  if not isinstance(rows,list) or not rows:raise APIError('ASSETS_SCHEMA')
  out={}
  for r in rows:
   if not isinstance(r,dict) or not isinstance(r.get('symbol'),str) or r['symbol'] in out:raise APIError('ASSET_SCHEMA')
   if r.get('class')!='us_equity':continue
   out[r['symbol']]=r
  if not out:raise APIError('NO_US_EQUITY_ASSETS')
  return out
 def bar_pages(self,symbols,start,end):
  symbols=sorted(symbols)
  for offset in range(0,len(symbols),100):
   batch=symbols[offset:offset+100];params={'symbols':','.join(batch),'timeframe':'1Min','feed':'sip','adjustment':'raw','sort':'asc','limit':10000,'start':start,'end':end};seen=set()
   while True:
    d=self.get('/v2/stocks/bars',params);bars=d.get('bars')
    if not isinstance(bars,dict) or set(bars)-set(batch):raise APIError('BAR_PAGE_SCHEMA')
    yield bars
    token=d.get('next_page_token')
    if not token:break
    if token in seen:raise APIError('PAGINATION_CYCLE')
    seen.add(token);params['page_token']=token
