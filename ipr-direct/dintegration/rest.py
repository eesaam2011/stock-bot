from datetime import datetime,timezone,timedelta
import math
class DataUnavailable(RuntimeError):pass
def instant(s):return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(timezone.utc)
class MarketREST:
 """Injected GET transport; all pagination is exhausted or fails closed."""
 def __init__(self,get,root,measure):self.get=get;self.root=root;self.measure=measure
 def pages(self,path,params,key):
  seen=set();p=dict(params)
  while True:
   d=self.get(path,p)
   if not isinstance(d,dict) or key not in d:raise DataUnavailable('MISSING_RESPONSE_KEY')
   yield d[key]
   token=d.get('next_page_token')
   if not token:return
   if token in seen:raise DataUnavailable('PAGINATION_CYCLE')
   seen.add(token);p['page_token']=token
 def snapshots(self,symbols):
  syms=sorted(set(symbols))
  if not set(syms)<=self.root.symbols:raise DataUnavailable('OUTSIDE_ROOT')
  out={}
  for n in range(0,len(syms),100):
   batch=syms[n:n+100];d=self.get('/v2/stocks/snapshots',{'symbols':','.join(batch),'feed':'sip'})
   if not isinstance(d,dict) or set(d)-set(batch):raise DataUnavailable('SNAPSHOT_SCHEMA')
   out.update(d)
  return out
 def snapshot_envelopes(self,symbols,now):
  # Capture receipt time for EACH batch, never relabel the first batch at scan end.
  out={};syms=sorted(set(symbols))
  for offset in range(0,len(syms),100):
   raw=self.snapshots(syms[offset:offset+100]);captured=now().isoformat()
   for symbol,row in raw.items():out[symbol]={'symbol':symbol,'feed':'sip','captured_at':captured,'data':row}
  return out
 def bars_multi(self,symbols,start,end,timeframe,**kw):
  if timeframe not in {'1Min','5Min'}:raise DataUnavailable('NATIVE_ONLY')
  if not set(symbols)<=self.root.symbols or end<start:raise DataUnavailable('BAR_SCOPE_OR_TIME')
  out={s:[] for s in symbols}
  for n in range(0,len(symbols),100):
   batch=list(symbols)[n:n+100]
   q={'symbols':','.join(batch),'start':start.isoformat(),'end':end.isoformat(),'timeframe':timeframe,'feed':'sip','adjustment':'raw','sort':'asc','limit':10000}
   for bars in self.pages('/v2/stocks/bars',q,'bars'):
    if not isinstance(bars,dict) or set(bars)-set(batch):raise DataUnavailable('BARS_SCHEMA')
    for s,rows in bars.items():
     if not isinstance(rows,list):raise DataUnavailable('BARS_SCHEMA')
     for row in rows:
      t=instant(row['t'])
      if not start<=t<=end:raise DataUnavailable('FUTURE_OR_OUTSIDE_BAR')
      if not all(isinstance(row.get(k),(float,int)) and math.isfinite(row[k]) for k in ['o','h','l','c','v']):raise DataUnavailable('INVALID_BAR')
      if row['l']<=0 or row['h']<row['l'] or row['v']<0:raise DataUnavailable('INVALID_BAR')
      out[s].append({**row,**({'_timeframe':'native_5Min'} if timeframe=='5Min' else {})})
  for s,rs in out.items():
   by={}
   for r in rs:
    if r['t'] in by and r!=by[r['t']]:raise DataUnavailable('CONFLICTING_BAR_REVISION')
    by[r['t']]=r
   out[s]=[by[t] for t in sorted(by)]
  return out
 def trades(self,symbol,start,end,limit=10000):
  if symbol not in self.root.symbols or start>end:raise DataUnavailable('TRADE_SCOPE_OR_TIME')
  params={'symbols':symbol,'start':start.isoformat(),'end':end.isoformat(),'feed':'sip','sort':'asc','limit':limit};out=[]
  for payload in self.pages('/v2/stocks/trades',params,'trades'):
   if not isinstance(payload,dict) or set(payload)-{symbol}:raise DataUnavailable('TRADES_SCHEMA')
   rows=payload.get(symbol,[])
   if not isinstance(rows,list):raise DataUnavailable('TRADES_SCHEMA')
   for row in rows:
    if not isinstance(row,dict) or 't' not in row or 'p' not in row:raise DataUnavailable('TRADE_INPUT_MISSING')
    if not start<=instant(row['t'])<=end:raise DataUnavailable('TRADE_OUTSIDE_WINDOW')
    if isinstance(row['p'],bool) or not isinstance(row['p'],(int,float)) or not math.isfinite(row['p']):raise DataUnavailable('TRADE_PRICE_INVALID')
    out.append(dict(row))
  # Do not collapse equal timestamps or invent ordering; the original reconciler
  # receives literal rows and handles its frozen equal-timestamp ambiguity policy.
  return out
 def native_1m_gap(self,s,start,end):return self.bars_multi([s],start,end,'1Min')[s]
 def native_5m(self,s,start,end):return self.bars_multi([s],start,end,'5Min')[s]
 def warmup(self,symbols,now,required5_minutes):
  one=self.bars_multi(list(symbols),now-timedelta(minutes=60),now,'1Min');five=self.bars_multi(list(symbols),now-timedelta(minutes=required5_minutes),now,'5Min')
  # Completed bars only; do not convert historical bars into live trades.
  return ({s:[r for r in rs if instant(r['t'])+timedelta(minutes=1)<=now] for s,rs in one.items()},{s:[r for r in rs if instant(r['t'])+timedelta(minutes=5)<=now] for s,rs in five.items()})
