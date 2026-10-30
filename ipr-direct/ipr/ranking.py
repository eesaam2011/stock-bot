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
 def __init__(self):self.current=set();self.last=None
 def update(self,scores,pins,now):
  pins=set(pins)
  if len(pins)>500:raise ValueError('PIN_CAP_CONFLICT_NO_DROP')
  available=500-len(pins)
  if self.last is not None and now-self.last<120:
   keep=sorted(self.current-pins,key=lambda s:(-scores.get(s,{'score':-1})['score'],s))[:available]
   self.current=pins|set(keep);return sorted(self.current)
  chosen=set(sorted((self.current&scores.keys())-pins,key=lambda s:(-scores[s]['score'],s))[:available])
  for s in sorted(scores,key=lambda s:(-scores[s]['score'],s)):
   if s in pins or s in chosen:continue
   if len(chosen)<available:chosen.add(s);continue
   if not chosen:break
   worst=sorted(chosen,key=lambda s:(-scores[s]['score'],s))[-1]
   if scores[s]['score']>=scores[worst]['score']+5:chosen.remove(worst);chosen.add(s)
  self.current=pins|chosen;self.last=now;return sorted(self.current)
