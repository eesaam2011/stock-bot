"""Bounded BAR receipt-minute histograms; observational only, never safety input.
10ms upper-bound buckets. Last3 receipt UTC minutes, not all-session retention.
"""
from array import array
import math,time
from datetime import datetime,timezone
class MinuteDelayStats:
 def __init__(self,*,keep=3,step=.01,bins=1024,wall_offset=None):
  self.keep=keep;self.step=step;self.bins=bins
  self.offset=time.time()-time.monotonic() if wall_offset is None else wall_offset
  self.windows={};self.record_errors=0;self.near_limit_80_total=0
 def _new(self):
  return {'count':0,'failed_or_dropped':0,'near_limit_80_count':0,'start':array('I',[0])*(self.bins+1),'complete':array('I',[0])*(self.bins+1),'start_max':0.,'complete_max':0.}
 def record(self,received_mono,start_delay,complete_delay,max_age,status='OK'):
  if not all(math.isfinite(x)and x>=0 for x in [start_delay,complete_delay,max_age]):raise ValueError('INVALID_DELAY_SAMPLE')
  key=int((received_mono+self.offset)//60)*60
  if key not in self.windows:
   self.windows[key]=self._new()
   while len(self.windows)>self.keep:del self.windows[min(self.windows)]
  w=self.windows.get(key)
  if w is None:return # a late old job cannot evict a newer receipt-minute population.
  if max(start_delay,complete_delay)>=.8*max_age:
   w['near_limit_80_count']+=1;self.near_limit_80_total+=1
  if status!='OK':w['failed_or_dropped']+=1;return
  w['count']+=1
  for name,value in [('start',start_delay),('complete',complete_delay)]:
   # ceil assigns an upper-bound bucket. Last bucket is overflow, backed by exact max.
   index=min(self.bins,int(math.ceil(value/self.step)));w[name][index]+=1;w[name+'_max']=max(w[name+'_max'],value)
 def _summary(self,w,name):
  n=w['count'];out={'count':n,'max_seconds':w[name+'_max'] if n else None}
  targets={q:max(1,math.ceil(n*q)) for q in [.5,.95,.99]};result={};total=0
  for i,v in enumerate(w[name]):
   total+=v
   for q,rank in targets.items():
    if q not in result and total>=rank:result[q]=w[name+'_max']if i==self.bins else i*self.step
  for q,label in [(.5,'p50_seconds'),(.95,'p95_seconds'),(.99,'p99_seconds')]:out[label]=result.get(q)if n else None
  return out
 def snapshot(self):
  # Arrays/dicts owned by event loop; snapshot called in heartbeat thread underGIL.
  # A per-field observational snapshot, not a transactional census.
  rows=[]
  for key,w in sorted(list(self.windows.items())):
   rows.append({'receipt_minute_utc':datetime.fromtimestamp(key,timezone.utc).isoformat(),'start':self._summary(w,'start'),'complete':self._summary(w,'complete'),'near_limit_80_count':w['near_limit_80_count'],'failed_or_dropped_count':w['failed_or_dropped']})
  return {'receipt_minutes':rows,'near_limit_80_total':self.near_limit_80_total,'record_errors':self.record_errors,'quantile_kind':'10ms histogram upper bounds; overflow uses exact observed max; processed BAR only, censored when work is dropped','max_retained_minutes':self.keep,'scope':'receipt UTC minute for BAR callbacks, not bar event-time minute or wire time; last3 minutes only; exact maxima'}
