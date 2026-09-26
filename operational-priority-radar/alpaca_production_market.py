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
 def _single_symbol_paged(self,symbol,start,end,kind,*,timeframe=None,
                            limit=10000,max_pages=64,max_rows=100000):
  """Bounded single-symbol SIP REST fetch, for active-trade recovery.

  A terminal API page proves pagination exhaustion only. It is NOT proof
  that SIP recorded every trade/bar or that halt/status events were replayed.
  """
  from datetime import datetime
  if (not isinstance(symbol,str) or not symbol or symbol!=symbol.strip()
      or any(ch in symbol for ch in "/?& ")
      or kind not in {"bars","trades"}
      or (kind=="bars" and timeframe not in {"1Min","5Min"})
      or not isinstance(start,datetime) or not isinstance(end,datetime)
      or start.tzinfo is None or end.tzinfo is None or start>=end
      or not isinstance(limit,int) or not 1<=limit<=10000
      or not isinstance(max_pages,int) or not 1<=max_pages<=1000
      or not isinstance(max_rows,int) or not 1<=max_rows<=350000):
   raise AlpacaMarketDataError("INVALID_SINGLE_SYMBOL_REST_REQUEST")
  base={"start":start.isoformat(),"end":end.isoformat(),"feed":"sip",
        "sort":"asc","limit":limit}
  if kind=="bars":base.update(timeframe=timeframe,adjustment="raw")
  out=[];token=None;seen_tokens=set();pages=0
  while True:
   if pages>=max_pages:raise AlpacaMarketDataError("REST_PAGE_LIMIT_EXCEEDED")
   q=dict(base)
   if token is not None:q["page_token"]=token
   d=self._get(f"/v2/stocks/{symbol}/{kind}",q)
   if not isinstance(d,dict) or kind not in d:
    raise AlpacaMarketDataError("REST_MISSING_SINGLE_SYMBOL_ROWS")
   rows=d[kind]
   if rows is None:rows=[]  # Alpaca can report no observations for a symbol.
   if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):
    raise AlpacaMarketDataError("REST_INVALID_SINGLE_SYMBOL_ROWS")
   pages+=1
   if len(out)+len(rows)>max_rows:
    raise AlpacaMarketDataError("REST_ROW_LIMIT_EXCEEDED")
   out.extend(rows)
   next_token=d.get("next_page_token")
   if next_token is None or next_token=="":
    return out,{"kind":kind,"timeframe":timeframe,"symbol":symbol,
                "pages":pages,"rows":len(out),"api_pagination_exhausted":True,
                "full_session_coverage_proven":False,
                "sip_trade_coverage_proven":False,"halt_coverage_proven":False}
   if (not isinstance(next_token,str) or len(next_token)>4096
       or next_token in seen_tokens):
    raise AlpacaMarketDataError("REST_REPEATED_OR_INVALID_PAGE_TOKEN")
   seen_tokens.add(next_token);token=next_token
 def bars_audited(self,symbol,start,end,timeframe,limit=10000,*,
                  max_pages=64,max_rows=100000):
  return self._single_symbol_paged(symbol,start,end,"bars",timeframe=timeframe,
                  limit=limit,max_pages=max_pages,max_rows=max_rows)
 def trades_audited(self,symbol,start,end,limit=10000,*,
                    max_pages=64,max_rows=100000):
  return self._single_symbol_paged(symbol,start,end,"trades",limit=limit,
                  max_pages=max_pages,max_rows=max_rows)
 def bars(self,symbol,start,end,timeframe,limit=10000):
  return self.bars_audited(symbol,start,end,timeframe,limit=limit)[0]
 def trades(self,symbol,start,end,limit=10000):
  return self.trades_audited(symbol,start,end,limit=limit)[0]
 def active_us_equity_assets(self):
  req=urllib.request.Request(TRADING_BASE+"/v2/assets?status=active&asset_class=us_equity",
      headers={"APCA-API-KEY-ID":self.creds.key_id,"APCA-API-SECRET-KEY":self.creds.secret_key})
  try:
   with self.opener(req,timeout=20) as r:return json.loads(r.read().decode())
  except Exception as e:raise AlpacaMarketDataError(str(e))

 def bars_multi(self,symbols,start,end,timeframe,limit=10000,batch_size=200,max_workers=8):
  """Bounded SIP pagination; never silently accept a repeated page token."""
  rows,_=self.bars_multi_audited(symbols,start,end,timeframe,limit=limit,
                                batch_size=batch_size,max_workers=max_workers)
  return rows
 def bars_multi_audited(self,symbols,start,end,timeframe,limit=10000,
                         batch_size=200,max_workers=8,max_pages=64,
                         max_rows_per_batch=100000):
  """Return rows plus an API pagination audit, NOT market-data coverage proof.

  A completed next_page_token chain attests only that the API returned its
  terminal page for the requested parameters. Missing bars can be legitimate
  (illiquid symbols, halts); they are never inferred as full session coverage.
  """
  from concurrent.futures import ThreadPoolExecutor,as_completed
  from datetime import datetime
  syms=[str(x).strip().upper() for x in symbols]
  if (not syms or any(not s for s in syms) or len(syms)!=len(set(syms))
      or timeframe not in {"1Min","5Min"} or not 1<=batch_size<=200
      or not 1<=max_workers<=8 or not 1<=limit<=10000
      or not 1<=max_pages<=1000 or not 1<=max_rows_per_batch<=350000
      or not isinstance(start,datetime) or not isinstance(end,datetime)
      or start.tzinfo is None or end.tzinfo is None or start>=end):
   raise AlpacaMarketDataError("INVALID_AUDITED_REST_REQUEST")
  batches=[syms[i:i+batch_size] for i in range(0,len(syms),batch_size)]
  def one(batch):
   base={"symbols":",".join(batch),"timeframe":timeframe,
         "start":start.isoformat(),"end":end.isoformat(),"feed":"sip",
         "adjustment":"raw","sort":"asc","limit":limit}
   got={s:[] for s in batch};token=None;seen_tokens=set();pages=0;rows_count=0
   while True:
    if pages>=max_pages:raise AlpacaMarketDataError("REST_PAGE_LIMIT_EXCEEDED")
    q=dict(base)
    if token is not None:q["page_token"]=token
    d=self._get("/v2/stocks/bars",q)
    if not isinstance(d,dict) or not isinstance(d.get("bars"),dict):
     raise AlpacaMarketDataError("REST_INVALID_BARS_RESPONSE")
    pages+=1
    for sym,rows in d["bars"].items():
     if sym not in got:raise AlpacaMarketDataError("REST_UNREQUESTED_SYMBOL")
     if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):
      raise AlpacaMarketDataError("REST_INVALID_SYMBOL_ROWS")
     rows_count+=len(rows)
     if rows_count>max_rows_per_batch:
      raise AlpacaMarketDataError("REST_ROW_LIMIT_EXCEEDED")
     got[sym].extend(rows)
    next_token=d.get("next_page_token")
    if next_token is None or next_token=="":
     return got,{"requested_symbols":len(batch),"pages":pages,
                 "rows":rows_count,"terminal_page_seen":True,
                 "empty_symbols":sum(not got[s] for s in batch)}
    if (not isinstance(next_token,str) or len(next_token)>4096
        or next_token in seen_tokens):
     raise AlpacaMarketDataError("REST_REPEATED_OR_INVALID_PAGE_TOKEN")
    seen_tokens.add(next_token);token=next_token
  out={s:[] for s in syms};audits=[]
  with ThreadPoolExecutor(max_workers=min(max_workers,len(batches))) as ex:
   futs=[ex.submit(one,b) for b in batches]
   for f in as_completed(futs):
    got,audit=f.result()
    out.update(got);audits.append(audit)
  return out,{"timeframe":timeframe,"batch_count":len(audits),
              "pages":sum(a["pages"] for a in audits),
              "rows":sum(a["rows"] for a in audits),
              "empty_symbols":sum(a["empty_symbols"] for a in audits),
              "api_pagination_exhausted":all(a["terminal_page_seen"] for a in audits),
              "full_session_coverage_proven":False,
              "source":"Alpaca SIP REST raw"}
 def native_recovery_batch(self,symbols,start,end,batch_size=200,max_workers=8):
  """Legacy two-lane API; still bounded, but no returned audit."""
  one,five,_=self.native_recovery_batch_audited(symbols,start,end,
                               batch_size=batch_size,max_workers=max_workers)
  return one,five
 def native_recovery_batch_audited(self,symbols,start,end,batch_size=200,
                                    max_workers=8):
  """Both independent native 1Min/5Min paginated REST chains must terminate."""
  one,a1=self.bars_multi_audited(symbols,start,end,"1Min",batch_size=batch_size,
                                 max_workers=max_workers)
  five,a5=self.bars_multi_audited(symbols,start,end,"5Min",batch_size=batch_size,
                                  max_workers=max_workers)
  if set(one)!=set(five) or not (a1["api_pagination_exhausted"] and
                                 a5["api_pagination_exhausted"]):
   raise AlpacaMarketDataError("REST_INCOMPLETE_NATIVE_LANES")
  five={s:[{**r,"_timeframe":"native_5Min"} for r in rows]
        for s,rows in five.items()}
  return one,five,{"native_1m":a1,"native_5m":a5,
                   "both_api_page_chains_exhausted":True,
                   "full_session_coverage_proven":False,
                   "sip_continuity_proven":False}
 def native_1m_gap(self,symbol,start,end):return self.bars(symbol,start,end,"1Min")
 def native_5m(self,symbol,start,end):
  try:return [{**r,"_timeframe":"native_5Min"} for r in self.bars(symbol,start,end,"5Min")]
  except AlpacaMarketDataError as e:raise Native5MinDataUnavailable("EARLY_CORE_DATA_UNAVAILABLE") from e
class AlpacaSIPProtocol:
 @staticmethod
 def auth(k,s):return {"action":"auth","key":k,"secret":s}
 @staticmethod
 def subscribe(symbols,trade_symbols=None):
  # Backwards-compatible default keeps the v1 contract for isolated callers.
  # v1.1 production passes an explicit bounded trade scope (normally empty).
  x=list(symbols);t=x if trade_symbols is None else list(trade_symbols)
  return {"action":"subscribe","trades":t,"bars":x,"statuses":["*"]}
 @staticmethod
 def subscribe_trades(symbols):return {"action":"subscribe","trades":list(symbols)}
 @staticmethod
 def unsubscribe_trades(symbols):return {"action":"unsubscribe","trades":list(symbols)}
 @staticmethod
 def classify(m):return {"t":"TRADE","q":"QUOTE","b":"BAR","s":"STATUS"}.get(m.get("T"),"OTHER")
