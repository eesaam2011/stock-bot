"""Only transport changes to accepted NewsReader: HMGET up to750 in3 batches."""
import json,time
from .connectors import NewsReader
class BatchNews(NewsReader):
 def __init__(self,env,post,clock=None):super().__init__(env,post,clock);self.cache={};self.present=None;self.error=None
 def command(self,cmd):
  if cmd[0] not in {'HMGET','HLEN'} or cmd[1]!=self.key:raise ValueError('READ_ONLY_NEWS')
  if not self.env.get('UPSTASH_REDIS_REST_URL') or not self.env.get('UPSTASH_REDIS_REST_TOKEN'):self.error='CONFIG_MISSING';return None
  try:
   r=self.post(self.env['UPSTASH_REDIS_REST_URL'],headers={'Authorization':'Bearer '+self.env['UPSTASH_REDIS_REST_TOKEN']},json=cmd,timeout=20,allow_redirects=False)
   if r.status_code!=200 or r.json().get('error'):raise ValueError()
   self.error=None;return r.json().get('result')
  except Exception:self.error='NEWS_READ_FAILED';return None
 def count(self):
  n=self.command(['HLEN',self.key]);self.present=n if type(n) is int and n>=0 else None;return self.present
 def refresh(self,symbols):
  now=self.clock() if self.clock else time.time();symbols=list(dict.fromkeys(symbols))[:750]
  self.cache={s:v for s,v in self.cache.items() if 0<=now-v[0]<60 and s in symbols}
  missing=[s for s in symbols if s not in self.cache]
  for offset in range(0,len(missing),250):
   batch=missing[offset:offset+250];result=self.command(['HMGET',self.key,*batch])
   if not isinstance(result,list) or len(result)!=len(batch):result=[None]*len(batch)
   for s,raw in zip(batch,result):
    try:d=json.loads(raw) if raw else None
    except Exception:d=None
    self.cache[s]=(now,d if isinstance(d,dict) else None)
 def raw(self,symbol):
  now=self.clock() if self.clock else time.time();v=self.cache.get(symbol.upper().strip())
  return v[1] if v and 0<=now-v[0]<60 else None
