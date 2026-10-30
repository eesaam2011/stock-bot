"""RTH-only native1Min aggregation; ephemeral SQLite, durable small daily summaries."""
import json,sqlite3,hashlib,math
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo
NY=ZoneInfo('America/New_York')
def dt(x):
 d=x if isinstance(x,datetime) else datetime.fromisoformat(x.replace('Z','+00:00'))
 if d.tzinfo is None:raise ValueError('TIMESTAMP_REQUIRED')
 return d.astimezone(timezone.utc)
def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def rth(row,calendar,now):
 t=dt(row['t']);day=t.astimezone(NY).date().isoformat();c=calendar.get(day)
 if not c or not dt(c['open'])<=t<dt(c['close']) or t+timedelta(minutes=1)>now:return None
 vals=[row[k] for k in ('o','h','l','c','v')]
 if any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for v in vals):raise ValueError('INVALID_OHLCV')
 o,h,l,cl,v=vals
 if v<0 or min(o,l,cl)<=0 or not l<=o<=h or not l<=cl<=h:raise ValueError('INVALID_OHLCV')
 return day

def prepare_history(api,symbols,calendar,session,now,log=lambda row:None,previous=None):
 prior=[c for c in calendar if c['session']<session][-20:]
 if len(prior)!=20:raise ValueError('HISTORY_CALENDAR_LT20')
 cal={c['session']:c for c in prior};previous=previous or {};sums={s:{day:dict(row) for day,row in previous.get(s,{}).items() if day in cal} for s in symbols};pages=0
 groups={}
 for symbol in symbols:
  missing=tuple(c['session'] for c in prior if c['session'] not in sums[symbol])
  if missing:groups.setdefault(missing,[]).append(symbol)
 for missing,batch_symbols in groups.items():
  for bars in api.bar_pages(batch_symbols,cal[missing[0]]['open'],cal[missing[-1]]['close']):
   for s,rs in bars.items():
    for b in rs:
     day=rth(b,cal,now)
     if day is None or day not in missing:continue
     d=sums[s].get(day);t=dt(b['t']).isoformat()
     if d and t<=d['last']:raise ValueError('HISTORY_DUPLICATE_OR_UNSORTED')
     if d:d.update(v=d['v']+b['v'],h=max(d['h'],b['h']),l=min(d['l'],b['l']),c=b['c'],last=t)
     else:sums[s][day]={'v':b['v'],'h':b['h'],'l':b['l'],'c':b['c'],'last':t}
   pages+=1
   if pages%25==0:log({'stage':'PREP_PROGRESS','pages':pages,'api_requests':api.requests})
 result={}
 for s,days in sums.items():
  if set(days)!=set(cal):continue
  rows=[days[c['session']] for c in prior];adv=sum(r['v'] for r in rows)/20
  if adv>0:result[s]={'adv20':adv,'high':max(r['h'] for r in rows),'low':min(r['l'] for r in rows),'close':rows[-1]['c']}
 return {'session':session,'observed_at':now.isoformat(),'source':'sip_native_1Min_RTH_SUM','calendar_sessions':[c['session'] for c in prior],'features':result,'daily_aggregates':sums}

class SessionVolumes:
 def __init__(self,path,calendar,session):
  from pathlib import Path
  import re
  for old in Path('/tmp').glob('ipr_rth_*.sqlite*'):
   match=re.fullmatch(r'ipr_rth_(\d{4}-\d{2}-\d{2})\.sqlite(?:-wal|-shm)?',old.name)
   if match and match.group(1)<session:old.unlink(missing_ok=True)
  self.db=sqlite3.connect(path,check_same_thread=False);self.db.execute('PRAGMA cache_size=-2048');self.db.execute('PRAGMA journal_mode=WAL')
  self.db.execute('CREATE TABLE IF NOT EXISTS bars(symbol TEXT,t TEXT,o REAL,c REAL,v REAL,PRIMARY KEY(symbol,t))');self.db.commit()
  self.calendar={c['session']:c for c in calendar};self.session=session;self.watermark=None
 def update(self,api,symbols,now):
  op=dt(self.calendar[self.session]['open']);end=min(now,dt(self.calendar[self.session]['close']))
  start=max(op,(self.watermark-timedelta(minutes=5)) if self.watermark else op)
  # Commit complete request only. Partial pagination is rolled back and watermark retained.
  self.db.execute('BEGIN')
  try:
   for page in api.bar_pages(symbols,start.isoformat(),end.isoformat()):
    for s,rs in page.items():
     for b in rs:
      if rth(b,self.calendar,now)!=self.session:continue
      self.db.execute('INSERT INTO bars VALUES(?,?,?,?,?) ON CONFLICT(symbol,t) DO UPDATE SET o=excluded.o,c=excluded.c,v=excluded.v',(s,dt(b['t']).isoformat(),b['o'],b['c'],b['v']))
   self.db.commit();self.watermark=end
  except BaseException:self.db.rollback();raise
  return {s:{'volume':v,'open':o} for s,v,o in self.db.execute('SELECT b.symbol,SUM(b.v),(SELECT x.o FROM bars x WHERE x.symbol=b.symbol ORDER BY t LIMIT 1) FROM bars b GROUP BY b.symbol')}
