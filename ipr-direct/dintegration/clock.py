import time
class ClockProof:
 """Background refresh at inherited renewal cadence; market callbacks read cache only."""
 def __init__(self,server_time):self.server_time=server_time;self.uncertainty=None;self.proved_until=0.
 def refresh(self):
  start=time.time();remote=self.server_time();end=time.time();server=float(remote[0])+float(remote[1])/1000000
  self.uncertainty=abs(server-(start+end)/2)+(end-start)/2
  ok=self.uncertainty<=1 and end>=start
  self.proved_until=time.monotonic()+10 if ok else 0.
  return ok
 def check(self):return self.uncertainty is not None and self.uncertainty<=1 and time.monotonic()<self.proved_until
