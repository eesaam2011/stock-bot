"""Direct alert worker. No approval file, StageGate, shadow phase or trading orders."""
import sys,os,time,json,uuid,signal,subprocess,asyncio
from pathlib import Path
from datetime import datetime,timezone
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'reference_app'))
from .diagnostics import reason,exception_fields
from .api import Alpaca
from .preparation import prepare_root,load
from .runtime import policy_for,run
from r4a.direct_parent import DirectProcessGuard as ProcessGuard
from r4a.child_boundary import arm_parent_death
from r4a.guards import cgroup_sample

def chain_error_fields(exc):
 import re
 match=re.search(r'key=(ipr_direct:v1:chain2:[A-Za-z0-9:_-]+)',str(exc))
 original=exc
 while original.__cause__ is not None:original=original.__cause__
 return {'exception_type':type(original).__module__+'.'+type(original).__qualname__,'key':match.group(1) if match else None}

def credentials(env):
 for name in ('REDIS_URL','APCA_API_KEY_ID','APCA_API_SECRET_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID'):
  if not env.get(name):raise ValueError('MISSING_'+name)
 return True

def observe(event):
 # Diagnostics must never change the worker/parent decision or exception path.
 try:print(json.dumps(event),flush=True)
 except Exception:pass

class ParentLogs:
 def __init__(self):self.none_day=None;self.last_idle=None
 def pause(self,seconds,parked_day):
  self.idle(datetime.now(timezone.utc).date().isoformat(),parked_day,time.monotonic())
  time.sleep(seconds)
 def idle(self,day,parked_day,now):
  if self.last_idle is None or now-self.last_idle>=3600:
   import hashlib
   observe({'stage':'PARENT_IDLE','at':datetime.now(timezone.utc).isoformat(),'day':day,'parked_day':parked_day,'fingerprint':hashlib.sha256((day+'|'+str(parked_day)).encode()).hexdigest()[:12]})
   self.last_idle=now
 def prepared(self,bundle,day,none_reason):
  if bundle is None:
   if self.none_day!=day:
    observe({'stage':'PREP_ROOT_NONE','session':day,'reason':none_reason});self.none_day=day
  else:
   observe({'stage':'PREP_ROOT_OK','session':bundle['session'],'eligible_symbols':len(bundle['eligible']),'new_entries_prepared':bundle['new_entries_prepared']})

def main():
 child_started=time.monotonic()
 import argparse
 ap=argparse.ArgumentParser();ap.add_argument('--child',action='store_true',help=argparse.SUPPRESS);ap.add_argument('--boot-id',help=argparse.SUPPRESS);ap.add_argument('--parent-pid',type=int,help=argparse.SUPPRESS);ap.add_argument('--session',help=argparse.SUPPRESS);args=ap.parse_args()
 try:
  env=os.environ;credentials(env)
  if args.child:
   if not args.parent_pid or not args.boot_id or not args.session:raise ValueError('CHILD_REQUIRES_PARENT')
   arm_parent_death(args.parent_pid)
  import redis
  from redis.retry import Retry
  from redis.backoff import NoBackoff
  from .observability import RedisTelemetry
  client=RedisTelemetry(redis.Redis.from_url(env['REDIS_URL'],decode_responses=True,socket_timeout=5,socket_connect_timeout=5,retry_on_timeout=False,retry=Retry(NoBackoff(),0)));api=Alpaca(env)
  if args.child:
   bundle=load(client,'root:'+args.session)
   if not bundle:raise ValueError('PREP_ROOT_MISSING')
   exit_reason=asyncio.run(run(bundle,api,client,env,args.boot_id,started_monotonic=child_started))
   print(json.dumps({'stage':'IPR_CHILD_EXIT','exit_code':0,'reason':exit_reason or 'WORKER_TASK_COMPLETED_WITHOUT_EXCEPTION'}),flush=True);return 0
  stopped=[False];guard=[None]
  def stop(*_):
   stopped[0]=True
   if guard[0]:guard[0].stop()
  signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
  parked_day=None;parent_logs=ParentLogs()
  while not stopped[0]:
   now=datetime.now(timezone.utc);day=now.date().isoformat()
   parent_logs.idle(day,parked_day,time.monotonic())
   if parked_day==day:
    time.sleep(1);continue
   try:
    from r9core.chain import validate_bootstrap
    none_reason=['NO_SESSION_AND_NO_ACTIVE']
    validate_bootstrap(client,day);bundle=prepare_root(api,client,now,observe_none=lambda why:none_reason.__setitem__(0,why))
   except Exception as e:
    print(json.dumps({'stage':'PREP_ROOT_FAILED','reason':reason(e),**chain_error_fields(e)}),flush=True);time.sleep(30);continue
   parent_logs.prepared(bundle,day,none_reason[0])
   if bundle is None:time.sleep(30);continue
   policy=policy_for(bundle)
   def authorize():
    if not policy.time_allows(time.time()):raise ValueError('SESSION_CLOSED')
   def spawn(boot,number):return subprocess.Popen([sys.executable,'-m','ipr.main','--child','--boot-id',boot,'--parent-pid',str(os.getpid()),'--session',day],cwd=ROOT)
   guard[0]=ProcessGuard(client,policy,authorize,spawn,cgroup_sample,sleep=lambda seconds:parent_logs.pause(seconds,parked_day));result=guard[0].run();guard[0]=None
   print(json.dumps({'stage':'IPR_PARENT_EXIT',**result}),flush=True)
   if result.get('action')=='WORKER_EXIT' and result.get('exit_code')==75:continue
   parked_day=day
  return 0
 except Exception as e:
  if args.child and isinstance(e,ValueError) and str(e)=='SESSION_CLOSED':
   observe({'stage':'CHILD_SESSION_CLOSED_EXPECTED','at':datetime.now(timezone.utc).isoformat(),'session':args.session,'reason':'SESSION_CLOSED'})
  print(json.dumps({'stage':'IPR_FATAL','reason':reason(e),**exception_fields(e)}),flush=True)
  if args.child:print(json.dumps({'stage':'IPR_CHILD_EXIT','exit_code':2,'reason':reason(e),**exception_fields(e)}),flush=True)
  return 2
if __name__=='__main__':raise SystemExit(main())
