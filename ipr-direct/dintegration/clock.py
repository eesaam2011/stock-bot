import time,threading,json
from dreview.core import Blocked
class ClockUnavailable(Blocked):pass
class ClockProof:
 """Atomic proof; independent refresh worker; validity is still ten seconds."""
 def __init__(self,server_time,emit=None,loop_lag=None):
  self.server_time=server_time;self.emit=emit or (lambda row:print(json.dumps(row),flush=True));self.loop_lag=loop_lag or (lambda:None)
  self._state=(None,0.,None,None,None);self._stop=threading.Event();self._thread=None
 def snapshot(self):
  u,until,last,rtt,error=self._state;now=time.monotonic()
  return {'last_success_age_seconds':None if last is None else now-last,'uncertainty_seconds':u,'rtt_seconds':rtt,'proof_remaining_seconds':max(0.,until-now),'refresh_error_type':error,'loop_lag_seconds':self.loop_lag()}
 def failed(self):
  try:self.emit({'stage':'CLOCK_PROOF_FAILED',**self.snapshot()})
  except Exception:pass # Observability cannot change proof result.
 def refresh(self):
  start=time.time();m=time.monotonic();old=self._state
  try:
   remote=self.server_time();end=time.time();rtt=time.monotonic()-m;server=float(remote[0])+float(remote[1])/1000000
   u=abs(server-(start+end)/2)+(end-start)/2;ok=u<=1 and end>=start
   now=time.monotonic();self._state=(u,now+10 if ok else 0.,now if ok else old[2],rtt,None)
  except Exception as e:
   self._state=(old[0],0.,old[2],time.monotonic()-m,type(e).__module__+'.'+type(e).__qualname__);self.failed();raise
  if not ok:self.failed()
  return ok
 def check(self):
  u,until,_,_,_=self._state;ok=u is not None and u<=1 and time.monotonic()<until
  if not ok:self.failed()
  return ok
 def start(self):
  if self._thread is not None:return
  def work():
   while not self._stop.is_set():
    began=time.monotonic()
    try:self.refresh()
    except Exception:pass
    if self._stop.wait(max(0.,5-(time.monotonic()-began))):break
  self._thread=threading.Thread(target=work,name='ipr-clock-proof',daemon=True);self._thread.start()
 def close(self):
  self._stop.set()
  if self._thread is not None:self._thread.join(timeout=1.)
