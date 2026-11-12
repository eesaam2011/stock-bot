"""Local contract fixtures only; no Redis/network clients, no R10.5/6 code."""
import json,fnmatch,threading,time,urllib.parse
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace as NS
from production_pipeline import ProductionDecisionPipeline
from early_core_engine import EarlyCoreCrossing
from r103_support import Lock
from ipr.api import Alpaca
from dintegration.rest import MarketREST

AT=datetime(2026,11,10,16,35,1,tzinfo=timezone.utc)
POLICY={'open_utc':'2026-11-10T14:30:00+00:00','close_utc':'2026-11-10T21:00:00+00:00'}
def packed(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def item(i,state='PENDING'):
 return {'state':state,'event_id':'E%03d'%i,'updated_at':'%03d'%(i%7),'payload':{'n':i}}
class Box:
 def __init__(self,rows=None,pages=None):self.rows=dict(rows or {});self.pages=pages;self.calls=[];self.before_read=None;self.after_scan=None;self.error=None
 def scan(self,cursor=0,match='*',count=100):
  self.calls.append(('SCAN',cursor,match,count));keys=sorted(k for k in self.rows if fnmatch.fnmatch(k,match))
  pages=self.pages if self.pages is not None else [keys];n=int(cursor);out=list(pages[n]);next_cursor=n+1 if n+1<len(pages) else 0
  if self.after_scan:self.after_scan(self,n)
  return next_cursor,out
 def _read(self,key):
  if self.before_read:self.before_read(self,key)
  if self.error:raise self.error
  return self.rows.get(key)
 def get(self,key):self.calls.append(('GET',key));return self._read(key)
 def mget(self,keys):
  self.calls.append(('MGET',list(keys)))
  if self.before_read:self.before_read(self,None)
  if self.error:raise self.error
  return [self.rows.get(k) for k in keys]
 def set(self,key,value,**kw):self.calls.append(('SET',key,kw));self.rows[key]=value;return True

def box(n,mixed=False):return Box({'operational_priority_radar:v1:outbox:%03d'%i:json.dumps(item(i,'SENT' if mixed and i%2 else 'PENDING')) for i in range(n)})

class Response:
 status=200
 def __init__(self,data,delay=0):self.data=data;self.delay=delay;self.read_calls=[]
 def __enter__(self):return self
 def __exit__(self,*a):return False
 def read(self,n):self.read_calls.append(n);time.sleep(self.delay);return self.data

class HTTP:
 def __init__(self):self.calls=[];self.error=None;self.delay=0;self.guard=threading.Lock();self.active=0;self.peak=0;self.responses=[];self.gap=False
 def open(self,req,timeout):
  q={k:v[0] for k,v in urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query).items()};self.calls.append((q,timeout))
  if self.error:raise self.error
  bars={s:[dict(t=(AT.replace(second=0)-timedelta(minutes=5)).isoformat(),o=10.,h=10.1,l=9.9,c=10.,v=1000,n=20,vw=10.)] for s in q.get('symbols','').split(',')}
  if self.gap:bars={s:[] for s in bars}
  parent=self
  class R(Response):
   def __enter__(self):
    with parent.guard:parent.active+=1;parent.peak=max(parent.peak,parent.active)
    return self
   def __exit__(self,*a):
    with parent.guard:parent.active-=1
    return False
  r=R(packed({'bars':bars,'next_page_token':None}),self.delay);self.responses.append(r);return r

def native_fixture(n=201):
 syms=['S%03d'%i for i in range(n)];http=HTTP();api=Alpaca({'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'},opener=http.open);rest=MarketREST(api.get,NS(symbols=set(syms)),NS())
 p=object.__new__(ProductionDecisionPipeline);p.contention_probe=NS(record=lambda *v:None);p.symbols=syms;p.session=AT.date().isoformat();p._decision_lock=Lock();p._confluence_retry=set();p._structure_watch_symbols=set();p.rest=rest
 p.utc_now=lambda:AT;p.signal_gate=NS(window_reason=lambda at:None,reason=lambda *v:None);p._memory_probe=lambda *v,**kw:None;store={};p.r=NS(mget=lambda keys:[store.get(k.rsplit(':',1)[-1]) for k in keys]);p.liquidity_telemetry=NS(first=lambda *v:{})
 def evaluate(s,rows,at):
  if s!=syms[0]:return {},None
  end=AT.replace(second=0);return {},EarlyCoreCrossing(s,p.session,end-timedelta(minutes=5),end,at,at,3.,1.)
 p.ec=NS(required_history_minutes=lambda:305,evaluate_with_telemetry=evaluate)
 def persist(c):
  row={'symbol':c.symbol,'score':c.score,'bar_end_ts':c.bar_end_ts.isoformat(),'received_at':c.received_at.isoformat(),'decision_available_ts':c.decision_available_ts.isoformat()};store[c.symbol]=row;return c.symbol,row
 p.ew=NS(persist_first_e=persist);p._confluence=lambda *v:None
 bridge=NS(p=p,native_cycle=lambda s,now,allow:p.poll_native5(now,allow))
 return syms,http,api,rest,p,store,bridge
