"""Offline transport contention model, not market replay or a live time forecast."""
import json,threading,time,tempfile
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from ipr import api as module
from ipr.api import Alpaca
from ipr.data import SessionVolumes
from dintegration.rest import MarketREST
from types import SimpleNamespace

def load_case(enabled):
 lock=threading.Lock();first=threading.Event();scan_done=threading.Event();failures=[]
 activity={'active':0,'peak':0,'calls':0};progress={'native':0,'incremental':0,'other':0}
 local=threading.local();rows={};scan_api=None
 class Response:
  status=200
  def __init__(self,kind,slow):self.kind=kind;self.slow=slow
  def __enter__(self):
   with lock:
    activity['active']+=1;activity['peak']=max(activity['peak'],activity['active'])
   if self.slow:first.set()
   return self
  def read(self,n):
   time.sleep(.20 if self.slow else .001)
   return b'{"bars":{}}' if self.kind=='bars' else b'{}'
  def __exit__(self,*a):
   with lock:activity['active']-=1
 def opener(req,timeout):
  with lock:
   activity['calls']+=1;slow=activity['calls']==1
  return Response('bars' if '/bars?' in req.full_url else 'snapshots',slow)
 def worker(name):
  try:
   with scan_api.operation(name):
    for _ in range(24):
     scan_api.get('/v2/stocks/bars',{})
     with lock:progress[name]+=1
  except BaseException as exc:failures.append(repr(exc))
 symbols=['X'+str(i) for i in range(4845)]
 now=datetime(2026,11,10,14,35,tzinfo=timezone.utc)
 with patch.object(module,'SCAN_LANE_ENABLED',enabled):
  scan_api=Alpaca({'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'},opener=opener)
  workers=[threading.Thread(target=worker,args=(n,),daemon=True) for n in progress]
  workers[0].start()
  if not first.wait(3):raise AssertionError('synthetic holder did not start')
  for w in workers[1:]:w.start()
  rest=MarketREST(scan_api.get,SimpleNamespace(symbols=frozenset(symbols)),None)
  with tempfile.TemporaryDirectory(prefix='ipr-r104-sim-') as temp:
   calendar=[{'session':'2026-11-10','open':'2026-11-10T14:30:00Z','close':'2026-11-10T21:00:00Z'}]
   volumes=SessionVolumes(str(Path(temp)/'volume.sqlite'),calendar,'2026-11-10')
   at=time.monotonic();phase={}
   try:
    with scan_api.operation('scan_rest'):
     start=time.monotonic();volumes.update(scan_api,symbols,now);phase['volume_fetch_seconds']=time.monotonic()-start
     start=time.monotonic();rest.snapshot_envelopes(symbols,lambda:now);phase['snapshot_fetch_seconds']=time.monotonic()-start
   finally:volumes.db.close()
   elapsed=time.monotonic()-at;scan_done.set()
  for w in workers:w.join(5)
  if any(w.is_alive() for w in workers):raise AssertionError('synthetic worker starved')
  if failures:raise AssertionError(failures)
  metrics=scan_api.lock_metrics()
  return {'scan_lane_enabled':enabled,'scope':'ASSUMPTION model: 4845 symbols, actual SessionVolumes/MarketREST, 49 volume+49 snapshot HTTP requests; empty synthetic payloads; 3 competing paths x24 requests; first holder sleeps 0.20s, others 0.001s; not live timing or full ranking/model CPU',
          'scan_duration_seconds':elapsed,'scan_wait_seconds':metrics['operations']['scan_rest']['wait_total_seconds'],
          'scan_hold_seconds':metrics['operations']['scan_rest']['hold_total_seconds'],
          'scan_requests':metrics['operations']['scan_rest']['count'],'competing_progress':progress,
          'peak_HTTP':activity['peak'],'active_HTTP_at_end':activity['active'],'HTTP_calls':activity['calls'],
          'phase_seconds':phase,'metrics':metrics}

