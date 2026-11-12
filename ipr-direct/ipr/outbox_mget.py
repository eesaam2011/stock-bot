"""Same pending outbox interface, original SCAN plus bounded MGET reads.

No cache, key skipping, state writes or delivery policy. mark_attempt is inherited
literally from the immutable original. Concurrent GET/MGET snapshots can differ;
the equivalence contract is the same returned values, not arbitrary interleavings.
"""
import json
from redis_outbox_store import RedisOutboxStore
from . import review_flags

MGET_BATCH_KEYS = 100

class MGETOutboxStore(RedisOutboxStore):
 def pending(self,count=100):
  cursor=0;out=[]
  pattern=f"{self.prefix}:outbox:*"
  while True:
   cursor,keys=self.r.scan(cursor=cursor,match=pattern,count=count)
   for offset in range(0,len(keys),MGET_BATCH_KEYS):
    values=self.r.mget(keys[offset:offset+MGET_BATCH_KEYS])
    for raw in values:
     if raw:
      x=json.loads(raw)
      if x.get('state')=='PENDING':out.append(x)
   if int(cursor)==0:break
  return sorted(out,key=lambda x:(x.get('updated_at',''),x.get('event_id','')))

def outbox_store(redis):
 return MGETOutboxStore(redis) if review_flags.OUTBOX_MGET_ENABLED else RedisOutboxStore(redis)
