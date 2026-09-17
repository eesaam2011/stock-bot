import json
class RedisOutboxStore:
 def __init__(self,r,prefix="operational_priority_radar:v1"):self.r=r;self.prefix=prefix
 def pending(self,count=100):
  cursor=0;out=[]
  pattern=f"{self.prefix}:outbox:*"
  while True:
   cursor,keys=self.r.scan(cursor=cursor,match=pattern,count=count)
   for k in keys:
    raw=self.r.get(k)
    if raw:
     x=json.loads(raw)
     if x.get("state")=="PENDING":out.append(x)
   if int(cursor)==0:break
  return sorted(out,key=lambda x:(x.get("updated_at",""),x.get("event_id","")))
 def mark_attempt(self,event_id,success):
  key=f"{self.prefix}:outbox:{event_id}"
  raw=self.r.get(key)
  if raw is None:return False
  x=json.loads(raw);x["attempt_count"]=int(x.get("attempt_count",0))+1
  if success:x["state"]="SENT"
  self.r.set(key,json.dumps(x,sort_keys=True,separators=(",",":")));return True
