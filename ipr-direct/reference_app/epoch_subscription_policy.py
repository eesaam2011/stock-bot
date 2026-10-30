"""Static bar universe per transport epoch. No entry-price worker or gate."""
from collections import Counter
from threading import RLock
from frozen_static_scope import FrozenSubscriptionUniverse
from state_store import validate_record
import json
from datetime import datetime,timezone

class EpochSubscriptionPolicy:
 def __init__(self,root,session):
  self.lock=RLock();self.frozen=FrozenSubscriptionUniverse(root,session)
  self.scope=frozenset();self.pending=set();self.counters=Counter();self.epoch=0;self.failure=None
 def read_needs(self,r):
  values=set()
  # All active trades, including another session; E/B/opportunity current-session
  # records are conservatively retained, even terminal opportunities.
  for typ in ('trade','early_core','base_ready','opportunity'):
   pattern=('operational_priority_radar:v1:trade:*' if typ=='trade' else
            f'operational_priority_radar:v1:{typ}:{self.frozen.session}:*')
   cursor=0
   while True:
    cursor,keys=r.scan(cursor=cursor,match=pattern,count=500)
    for i in range(0,len(keys),500):
     batch=keys[i:i+500];raws=r.mget(batch) if hasattr(r,'mget') else [r.get(k) for k in batch]
     if len(raws)!=len(batch):raise RuntimeError('CANONICAL_SCOPE_SNAPSHOT_INCOMPLETE')
     for raw in raws:
      if raw is None:raise RuntimeError('CANONICAL_SCOPE_SNAPSHOT_CHANGED_RETRY_NEXT_EPOCH')
      x=json.loads(raw);validate_record(x,typ)
      if typ!='trade' or x['state'] in {'ACTIVE_PRE_T1','ACTIVE_POST_T1','HALTED_ACTIVE'}:values.add(x['symbol'])
    if int(cursor)==0:break
  return values
 def freeze(self,r):
  # A failed canonical scan aborts connection; guessing active needs is unsafe.
  needs=self.read_needs(r)
  with self.lock:
   self.frozen.check_session(self.frozen.session)
   self.failure=self.frozen.error
   self.scope=frozenset(self.frozen.static|needs|self.pending)
   self.pending.clear();self.epoch+=1
   if not self.scope:
    self.counters['NO_SAFE_SUBSCRIPTION_SCOPE']+=1
    raise RuntimeError('NO_SAFE_SUBSCRIPTION_SCOPE:'+str(self.failure or 'EMPTY')+':count='+str(self.counters['NO_SAFE_SUBSCRIPTION_SCOPE']))
   return tuple(sorted(self.scope))
 def require(self,symbol,required=True):
  with self.lock:
   if not required or symbol in self.scope:return True
   if symbol not in self.pending:
    self.pending.add(symbol);self.counters['DEFERRED_OUTSIDE_EPOCH_SCOPE']+=1
   return False
 def observe_redis(self,r):
  try:
   for s in self.read_needs(r):self.require(s)
  except Exception:
   with self.lock:self.failure='CANONICAL_SCOPE_OBSERVATION_FAILED';self.counters[self.failure]+=1
 def _day_state(self):
  day=max(self.frozen.session,datetime.now(timezone.utc).date().isoformat())
  return (day,self.frozen.engine.day_state(day) if self.frozen.engine and self.frozen.engine.integrity_ok else None)
 def new_nominations_allowed(self):
  with self.lock:
   day,state=self._day_state()
   if day>self.frozen.session and not self.failure:
    self.failure=(state['state'] if state and not state['allowed'] else 'SESSION_ROLLOVER_REQUIRES_RECOMPOSITION')
    self.counters[self.failure]+=1
   return not self.failure and not self.pending
 def snapshot(self):
  with self.lock:
   allowed=self.new_nominations_allowed();day,state=self._day_state()
   return {'configured_session':self.frozen.session,'evaluated_day':day,'scope_epoch_revision':self.epoch,'static_count':len(self.frozen.static),
           'frozen_scope_count':len(self.scope),'pending_outside_scope_count':len(self.pending),
           'rejected_additions_total':self.counters['DEFERRED_OUTSIDE_EPOCH_SCOPE'],
           'failure':self.failure,'day_state':state,
           'new_nominations_allowed':allowed,'counters':dict(self.counters),'frozen_counters':dict(self.frozen.counters),
           'price_worker_installed':False,'entry_filters_installed':False,'full_fallback':False}
