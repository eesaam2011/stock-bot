"""Read-only IPR connectors. Secrets from existing env names; no writes to sources."""
import json,math
from datetime import datetime,date
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from . import original_news_consumer as original

class FloatReader:
 filename='float_cache.json'
 def __init__(self,env,get):self.env=env;self.get=get;self.rows={};self.reason='NOT_LOADED'
 def refresh(self):
  self.rows={}
  if not self.env.get('GIST_ID') or not self.env.get('GITHUB_TOKEN'):self.reason='CONFIG_MISSING';return False
  try:
   response=self.get('https://api.github.com/gists/'+self.env['GIST_ID'],headers={'Authorization':'token '+self.env['GITHUB_TOKEN'],'Accept':'application/vnd.github.v3+json'},timeout=10,allow_redirects=False)
   if response.status_code!=200:self.reason='GIST_HTTP_FAILED';return False
   f=response.json()['files'][self.filename]
   if f.get('truncated') is True:self.reason='GIST_CONTENT_TRUNCATED';return False
   rows=json.loads(f['content'])
   if not isinstance(rows,dict):raise ValueError()
   self.rows=rows;self.reason='LOADED';return True
  except Exception:self.reason='GIST_READ_FAILED';return False
 def value(self,symbol,today=None):
  today=today or datetime.now(ZoneInfo('Asia/Riyadh')).date()
  row=self.rows.get(symbol.upper())
  unknown={'status':'UNKNOWN','float':None,'updated':None,'cause':'UNKNOWN_NOT_INFERRED'}
  if not isinstance(row,dict):return unknown
  try:
   v=row['float'];d=date.fromisoformat(row['updated']);age=(today-d).days
   if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or v<=0 or not 0<=age<=7:return unknown
   return {'status':'VALID','float':v,'updated':row['updated'],'age_days':age}
  except Exception:return unknown

class NewsReader:
 key='market_radar:news'
 def __init__(self,env,post,clock=None):self.env=env;self.post=post;self.clock=clock
 def raw(self,symbol):
  if not self.env.get('UPSTASH_REDIS_REST_URL') or not self.env.get('UPSTASH_REDIS_REST_TOKEN'):return None
  try:
   r=self.post(self.env['UPSTASH_REDIS_REST_URL'],headers={'Authorization':'Bearer '+self.env['UPSTASH_REDIS_REST_TOKEN']},json=['HGET',self.key,symbol.upper().strip()],timeout=20,allow_redirects=False)
   if r.status_code!=200:return None
   d=r.json()
   if d.get('error') or d.get('result') is None:return None
   row=json.loads(d['result'])
   return row if isinstance(row,dict) else None
  except Exception:return None
 def read(self,symbol):
  # Private copy of original function globals: no mutation of other readers or original module.
  import types
  context=dict(vars(original))
  if self.clock is not None:context['time']=SimpleNamespace(time=self.clock)
  for name in ('filter_recent_news_articles','normalize_shared_news_item'):
   old=getattr(original,name);context[name]=types.FunctionType(old.__code__,context,name,old.__defaults__)
  context['get_cached_symbol_news']=lambda s:context['normalize_shared_news_item'](s,self.raw(s))
  fn=types.FunctionType(original.get_symbol_news.__code__,context,'get_symbol_news')
  return fn(symbol)
