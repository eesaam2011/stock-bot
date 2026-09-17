from dataclasses import dataclass
import json,urllib.parse,urllib.request
SIP_STREAM_URL="wss://stream.data.alpaca.markets/v2/sip"
DATA_BASE="https://data.alpaca.markets"
TRADING_BASE="https://paper-api.alpaca.markets"
class AlpacaMarketDataError(RuntimeError):pass
class Native5MinDataUnavailable(AlpacaMarketDataError):pass
@dataclass(frozen=True)
class AlpacaCredentials:key_id:str;secret_key:str
class AlpacaREST:
 def __init__(self,creds,opener=None):self.creds=creds;self.opener=opener or urllib.request.urlopen
 def _get(self,path,params):
  req=urllib.request.Request(DATA_BASE+path+"?"+urllib.parse.urlencode(params),headers={"APCA-API-KEY-ID":self.creds.key_id,"APCA-API-SECRET-KEY":self.creds.secret_key})
  try:
   with self.opener(req,timeout=20) as r:return json.loads(r.read().decode())
  except Exception as e:raise AlpacaMarketDataError(str(e))
 def bars(self,symbol,start,end,timeframe,limit=10000):
  base={"timeframe":timeframe,"start":start.isoformat(),"end":end.isoformat(),"feed":"sip","adjustment":"raw","sort":"asc","limit":limit};out=[];token=None
  while True:
   p=dict(base)
   if token:p["page_token"]=token
   d=self._get(f"/v2/stocks/{symbol}/bars",p);out.extend(d.get("bars") or []);token=d.get("next_page_token")
   if not token:return out
 def trades(self,symbol,start,end,limit=10000):
  base={"start":start.isoformat(),"end":end.isoformat(),"feed":"sip","sort":"asc","limit":limit};out=[];token=None
  while True:
   p=dict(base)
   if token:p["page_token"]=token
   d=self._get(f"/v2/stocks/{symbol}/trades",p);out.extend(d.get("trades") or []);token=d.get("next_page_token")
   if not token:return out
 def active_us_equity_assets(self):
  req=urllib.request.Request(TRADING_BASE+"/v2/assets?status=active&asset_class=us_equity",
      headers={"APCA-API-KEY-ID":self.creds.key_id,"APCA-API-SECRET-KEY":self.creds.secret_key})
  try:
   with self.opener(req,timeout=20) as r:return json.loads(r.read().decode())
  except Exception as e:raise AlpacaMarketDataError(str(e))

 def bars_multi(self,symbols,start,end,timeframe,limit=10000,batch_size=200,max_workers=8):
  """Fetch SIP bars for a broad universe in bounded symbol batches.
  Returns {symbol:[bars...]}; any failed batch fails closed.
  """
  from concurrent.futures import ThreadPoolExecutor,as_completed
  syms=[str(x).strip().upper() for x in symbols if str(x).strip()]
  out={s:[] for s in syms}
  batches=[syms[i:i+batch_size] for i in range(0,len(syms),batch_size)]
  def one(batch):
   base={"symbols":",".join(batch),"timeframe":timeframe,"start":start.isoformat(),"end":end.isoformat(),"feed":"sip","adjustment":"raw","sort":"asc","limit":limit};got={s:[] for s in batch};token=None
   while True:
    q=dict(base)
    if token:q["page_token"]=token
    d=self._get("/v2/stocks/bars",q)
    for sym,rows in (d.get("bars") or {}).items():got.setdefault(sym,[]).extend(rows or [])
    token=d.get("next_page_token")
    if not token:return got
  if not batches:return out
  with ThreadPoolExecutor(max_workers=max(1,min(max_workers,len(batches)))) as ex:
   futs=[ex.submit(one,b) for b in batches]
   for f in as_completed(futs):
    for sym,rows in f.result().items():out[sym]=rows
  return out
 def native_recovery_batch(self,symbols,start,end,batch_size=200,max_workers=8):
  """Bounded broad-universe startup recovery: native 1Min + native 5Min."""
  one=self.bars_multi(symbols,start,end,"1Min",batch_size=batch_size,max_workers=max_workers)
  five=self.bars_multi(symbols,start,end,"5Min",batch_size=batch_size,max_workers=max_workers)
  five={s:[{**r,"_timeframe":"native_5Min"} for r in rows] for s,rows in five.items()}
  return one,five
 def native_1m_gap(self,symbol,start,end):return self.bars(symbol,start,end,"1Min")
 def native_5m(self,symbol,start,end):
  try:return [{**r,"_timeframe":"native_5Min"} for r in self.bars(symbol,start,end,"5Min")]
  except AlpacaMarketDataError as e:raise Native5MinDataUnavailable("EARLY_CORE_DATA_UNAVAILABLE") from e
class AlpacaSIPProtocol:
 @staticmethod
 def auth(k,s):return {"action":"auth","key":k,"secret":s}
 @staticmethod
 def subscribe(symbols):
  x=list(symbols);return {"action":"subscribe","trades":x,"quotes":x,"bars":x,"statuses":["*"]}
 @staticmethod
 def classify(m):return {"t":"TRADE","q":"QUOTE","b":"BAR","s":"STATUS"}.get(m.get("T"),"OTHER")
