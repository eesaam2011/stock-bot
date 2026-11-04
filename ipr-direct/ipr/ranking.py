import math
from datetime import datetime,timezone
from dintegration.rest import instant

def clip(x):return max(0.,min(1.,x))
def float_points(value):
 if value.get('status')!='VALID':return 0
 f=value['float']
 return 20 if f<=10e6 else 15 if f<=20e6 else 10 if f<=30e6 else 5 if f<=50e6 else 0

def base_score(symbol,snapshot,history,volume,now,op,floats):
 trade=snapshot.get('latestTrade',{});p=trade.get('p');t=instant(trade['t']) if trade.get('t') else None
 if isinstance(p,bool) or not isinstance(p,(float,int)) or not math.isfinite(p) or not 1<=p<=50 or t is None or not 0<=(now-t).total_seconds()<=60:return None
 if t<op:return None
 if not history or not volume or history['adv20']<=0:return None
 elapsed=(now-op).total_seconds()/60
 if not 0<elapsed<=390:return None
 ratio=volume['volume']/(history['adv20']*elapsed/390)
 activity=30*clip(math.log2(max(ratio,1))/3)
 response=15*clip((p/volume['open']-1)/.10)
 compression=10*(1-clip((history['high']-history['low'])/history['close']/.20))
 proximity=20*(1-clip(abs(p/history['high']-1)/.05))
 parts={'activity':activity,'response':response,'compression':compression,'proximity':proximity,'float':float_points(floats.value(symbol))}
 return {'symbol':symbol,'score95':round(sum(parts.values()),12),'parts':parts,'rvol_linear_approx':ratio,'snapshot_ts':t.isoformat()}

class Selection:
 def __init__(self):
  self.current=set();self.last=None;self.window_start=None;self.used=0;self.pending=set();self.deferred_minutes={}
 def update(self,scores,pins,now,blocked=False,utc_minute=None):
  from .turnover import OPTIONAL_REPLACEMENTS,OPTIONAL_WINDOW_SECONDS
  pins=set(pins)
  if len(pins)>500:raise ValueError('PIN_CAP_CONFLICT_NO_DROP')
  available=500-len(pins)
  # Missing/invalid current scores cannot stay eligible for new entry. Pins persist.
  valid=(self.current & scores.keys())-pins
  chosen=set(sorted(valid,key=lambda s:(-scores[s]['score'],s))[:available])
  if self.window_start is None or now-self.window_start>=OPTIONAL_WINDOW_SECONDS:
   self.window_start=now;self.used=0
  due=self.last is None or now-self.last>=120 or bool(self.pending)
  self.pending=set();deferred=0
  if not blocked and due:
   for symbol in sorted(scores,key=lambda s:(-scores[s]['score'],s)):
    if symbol in pins or symbol in chosen:continue
    if len(chosen)<available:chosen.add(symbol);continue
    if not chosen:break
    worst=sorted(chosen,key=lambda s:(-scores[s]['score'],s))[-1]
    if scores[symbol]['score']<scores[worst]['score']+5:continue
    if self.used>=OPTIONAL_REPLACEMENTS:
     self.pending.add(symbol);deferred+=1;continue
    chosen.remove(worst);chosen.add(symbol);self.used+=1
   self.last=now
  elif blocked:
   # Do not consume the valve or lose a pending overflow during ACK/recovery.
   self.pending={symbol for symbol in scores if symbol not in chosen|pins}
  self.current=pins|chosen
  minute=utc_minute or str(int(now//60));self.deferred_minutes[minute]=deferred
  while len(self.deferred_minutes)>6:self.deferred_minutes.pop(next(iter(self.deferred_minutes)))
  return sorted(self.current)
 def metrics(self):
  return {'optional_replacements_in_window':self.used,'deferred_optional_replacements_by_minute':dict(self.deferred_minutes),'scope':'latest scan deferred count per minute; not cumulative repeated deferrals of unique symbols'}
