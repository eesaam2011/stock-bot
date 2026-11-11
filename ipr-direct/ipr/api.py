"""Fixed read-only Alpaca endpoints. No order/account mutation interface."""
import json,urllib.request,urllib.parse,urllib.error,threading,time
from datetime import datetime,timezone
from contextlib import contextmanager,nullcontext
from collections import deque

# R10.4: transport scheduling only. False restores the R10.3 single-lock path.
SCAN_LANE_ENABLED = True

class _RequestTurn:
 """FIFO admission to one existing serialization lock; no extra request permits."""
 def __init__(self):self.condition=threading.Condition();self.queue=deque();self.busy=False
 @contextmanager
 def turn(self):
  token=object();entered=False
  try:
   with self.condition:
    self.queue.append(token)
    try:
     while self.busy or self.queue[0] is not token:self.condition.wait()
    except BaseException:
     self.queue.remove(token);self.condition.notify_all();raise
    self.queue.popleft();self.busy=True;entered=True
   yield
  finally:
   if entered:
    with self.condition:self.busy=False;self.condition.notify_all()

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
  self.lock=threading.Lock();self.scan_lock=threading.Lock();self.requests=0
  self.turns={'shared':_RequestTurn(),'scan':_RequestTurn()}
  self.local=threading.local();self.timing_lock=threading.Lock();self.timing={};self.timing_clock=time.monotonic
  self.lane_timing={};self.http_429={};self.last_429_log={};self.active_sections={'shared':0,'scan':0};self.peak_sections=0
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
 def _lane(self,operation,enabled):return 'scan' if enabled and operation=='scan_rest' else 'shared'
 def _timing(self,name,wait,hold,lane):
  with self.timing_lock:
   for rows,key in ((self.timing,name),(self.lane_timing,lane)):
    row=rows.setdefault(key,{'count':0,'wait_total_seconds':0.,'wait_max_seconds':0.,'hold_total_seconds':0.,'hold_max_seconds':0.})
    row['count']+=1
    for kind,value in [('wait',wait),('hold',hold)]:
     row[kind+'_total_seconds']+=value;row[kind+'_max_seconds']=max(row[kind+'_max_seconds'],value)
 def _activity(self,lane,delta):
  with self.timing_lock:
   self.active_sections[lane]+=delta;self.peak_sections=max(self.peak_sections,sum(self.active_sections.values()))
 def _record_429(self,operation,lane):
  # Never replace the original API error with a telemetry error.
  try:
   row=None;minute=int(time.time()//60)
   with self.timing_lock:
    self.http_429[(lane,operation)]=self.http_429.get((lane,operation),0)+1
    # At most one immediate line per lane per UTC minute, even if its caller exits.
    if minute>self.last_429_log.get(lane,-1):
     self.last_429_log[lane]=minute
     row={'stage':'R104_HTTP_429','at':datetime.now(timezone.utc).isoformat(),'lane':lane,'operation':operation,
          'http_429_total':sum(self.http_429.values()),'lane_http_429_count':sum(n for (l,o),n in self.http_429.items() if l==lane),'retry_added':False}
   if row is not None:print(json.dumps(row),flush=True)
  except BaseException:pass
 def lock_metrics(self):
  with self.timing_lock:
   return {'operations':{name:{**v,'wait_mean_seconds':v['wait_total_seconds']/v['count'],'hold_mean_seconds':v['hold_total_seconds']/v['count']} for name,v in self.timing.items()},
           'scope':'acquisitions including denied window rechecks; wait excludes heavy-window sleep; hold includes HTTP/read/JSON; R10.4 lane/FIFO admission changes transport scheduling only',
           'scan_lane_enabled':SCAN_LANE_ENABLED,'request_section_limit':2 if SCAN_LANE_ENABLED else 1,
           'lanes':{lane:{**self.lane_timing.get(lane,{'count':0,'wait_total_seconds':0.,'wait_max_seconds':0.,'hold_total_seconds':0.,'hold_max_seconds':0.}),
                    'active_request_sections':self.active_sections[lane],
                    'http_429_count':sum(n for (l,o),n in self.http_429.items() if l==lane)} for lane in ('scan','shared')},
           'active_request_sections':sum(self.active_sections.values()),'peak_request_sections':self.peak_sections,
           'http_429_total':sum(self.http_429.values()),
           'http_429_by_operation':{name:sum(n for (l,o),n in self.http_429.items() if o==name) for name in ('scan_rest','native','incremental','other')}}
 @contextmanager
 def serialized_request(self):
  enabled=SCAN_LANE_ENABLED # Fixed routing for this request, including a denied-window recheck.
  while True:
   hook=getattr(self,'before_request',None)
   if hook is not None:hook() # wait OUTSIDE the shared API serialization lock
   operation=self._operation();lane=self._lane(operation,enabled);lock=self.scan_lock if lane=='scan' else self.lock
   waiting=self.timing_clock()
   locked=False;active=False;acquired=None;released=None
   try:
    with self.turns[lane].turn() if enabled else nullcontext():
     try:
      lock.acquire();locked=True;acquired=self.timing_clock()
      allowed=getattr(self,'request_allowed',None)
      if allowed is not None and not allowed():continue # window crossed while waiting for lock
      try:self._activity(lane,1);active=True
      except BaseException:pass # Counter failure must not prevent a request.
      yield
      return
     finally:
      try:
       if active:
        try:self._activity(lane,-1)
        except BaseException:pass
      finally:
       if locked:
        try:released=self.timing_clock()
        finally:lock.release()
   finally:
    # Neither a lane turn nor its physical lock is held during observers.
    if acquired is not None and released is not None:
     wait=max(0.,acquired-waiting);hold=max(0.,released-acquired)
     try:self._timing(operation,wait,hold,lane)
     except BaseException:pass
     observer=getattr(self.local,'request_observer',None)
     if observer is not None:
      try:observer(operation,wait,hold)
      except BaseException:pass # Observer failures never mask an application exception.
 def call(self,host,path,params):
  if (host,path) not in {('paper-api.alpaca.markets','/v2/assets'),('paper-api.alpaca.markets','/v2/calendar'),('api.alpaca.markets','/v2/assets'),('api.alpaca.markets','/v2/calendar'),('data.alpaca.markets','/v2/stocks/bars'),('data.alpaca.markets','/v2/stocks/trades'),('data.alpaca.markets','/v2/stocks/snapshots')}:raise APIError('ENDPOINT_FORBIDDEN')
  req=urllib.request.Request('https://'+host+path+'?'+urllib.parse.urlencode(params),headers={'APCA-API-KEY-ID':self.key,'APCA-API-SECRET-KEY':self.secret},method='GET')
  operation=self._operation();lane=self._lane(operation,SCAN_LANE_ENABLED)
  try:
   with self.serialized_request():
    with self.timing_lock:self.requests+=1
    with self.opener(req,timeout=20) as r:
     if r.status!=200:
      if r.status==429:self._record_429(operation,lane)
      raise APIError('HTTP_NOT_200')
     raw=r.read(32*1024*1024+1)
     if len(raw)>32*1024*1024:raise APIError('RESPONSE_TOO_LARGE')
     from .observability import observed_phase
     with observed_phase('rest.JSON'):return json.loads(raw)
  except APIError:raise
  except urllib.error.HTTPError as exc:
   if exc.code==429:self._record_429(operation,lane)
   raise APIError('ALPACA_HTTP_'+str(exc.code)) from None
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
