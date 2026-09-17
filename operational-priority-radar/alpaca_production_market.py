from dataclasses import dataclass
import json,urllib.parse,urllib.request
SIP_STREAM_URL="wss://stream.data.alpaca.markets/v2/sip"
DATA_BASE="https://data.alpaca.markets"
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
