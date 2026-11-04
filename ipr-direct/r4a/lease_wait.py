"""Read the ORIGINAL lease; never delete, renew, refund or guess its expiry."""
import json,time
LEADER_KEY='ipr_direct:v1:canonical:operational_priority_radar:v1:runtime:leader'
QUIET_SECONDS=35.0

def wait_for_lease(redis,authorize,*,stopped=lambda:False,clock=time.monotonic,sleep=time.sleep,emit=None):
 emit=emit or (lambda x:print(json.dumps(x),flush=True))
 began=clock();free_since=None;last_log=None
 while True:
  if stopped():return False
  authorize()
  owner=redis.get(LEADER_KEY) # Redis errors propagate; unavailable is NOT free.
  now=clock()
  if owner is not None:free_since=None
  elif free_since is None:free_since=now
  free=0. if free_since is None else now-free_since
  if last_log is None or now-last_log>=5 or free>=QUIET_SECONDS:
   emit({'stage':'GUARD_WAIT_FOR_LEASE','waited_seconds':now-began,'observed_free_seconds':free,
         'required_free_seconds':QUIET_SECONDS,'lease_occupied':owner is not None,'budget_claimed':False})
   last_log=now
  if free>=QUIET_SECONDS:return True
  sleep(min(1.,QUIET_SECONDS-free) if free_since is not None else 1.)

def acquire_without_budget(lead,redis,authorize,**ports):
 from leadership import NotLeader
 while True:
  authorize()
  try:lead.acquire();return
  except NotLeader:
   # A different deploy won the read/acquire race. Original atomic NX acquisition
   # is authoritative. No boot has been claimed; wait again rather than refund.
   if not wait_for_lease(redis,authorize,**ports):raise RuntimeError('LEASE_WAIT_STOPPED')
