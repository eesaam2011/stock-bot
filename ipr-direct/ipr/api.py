"""Fixed read-only Alpaca endpoints. No order/account mutation interface."""
import json,urllib.request,urllib.parse,urllib.error,threading
from datetime import datetime,timezone
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
 def call(self,host,path,params):
  if (host,path) not in {('paper-api.alpaca.markets','/v2/assets'),('paper-api.alpaca.markets','/v2/calendar'),('api.alpaca.markets','/v2/assets'),('api.alpaca.markets','/v2/calendar'),('data.alpaca.markets','/v2/stocks/bars'),('data.alpaca.markets','/v2/stocks/trades'),('data.alpaca.markets','/v2/stocks/snapshots')}:raise APIError('ENDPOINT_FORBIDDEN')
  req=urllib.request.Request('https://'+host+path+'?'+urllib.parse.urlencode(params),headers={'APCA-API-KEY-ID':self.key,'APCA-API-SECRET-KEY':self.secret},method='GET')
  try:
   with self.lock:
    self.requests+=1
    with self.opener(req,timeout=20) as r:
     if r.status!=200:raise APIError('HTTP_NOT_200')
     raw=r.read(32*1024*1024+1)
     if len(raw)>32*1024*1024:raise APIError('RESPONSE_TOO_LARGE')
     return json.loads(raw)
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
