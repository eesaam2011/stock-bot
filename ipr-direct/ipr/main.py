"""Direct alert worker. No approval file, StageGate, shadow phase or trading orders."""
import sys,os,time,json,uuid,signal,subprocess,asyncio
from pathlib import Path
from datetime import datetime,timezone
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'reference_app'))
from .diagnostics import reason
from .api import Alpaca
from .preparation import prepare_root,load
from .runtime import policy_for,run
from r4a.parent import ProcessGuard
from r4a.child_boundary import arm_parent_death
from r4a.guards import cgroup_sample

def credentials(env):
 for name in ('REDIS_URL','APCA_API_KEY_ID','APCA_API_SECRET_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID'):
  if not env.get(name):raise ValueError('MISSING_'+name)
 return True

def main():
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
  client=redis.Redis.from_url(env['REDIS_URL'],decode_responses=True,socket_timeout=5,socket_connect_timeout=5,retry_on_timeout=False,retry=Retry(NoBackoff(),0));api=Alpaca(env)
  if args.child:
   bundle=load(client,'root:'+args.session)
   if not bundle:raise ValueError('PREP_ROOT_MISSING')
   asyncio.run(run(bundle,api,client,env,args.boot_id));return 0
  stopped=[False];guard=[None]
  def stop(*_):
   stopped[0]=True
   if guard[0]:guard[0].stop()
  signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
  parked_day=None
  while not stopped[0]:
   now=datetime.now(timezone.utc);day=now.date().isoformat()
   if parked_day==day:
    time.sleep(1);continue
   try:bundle=prepare_root(api,client,now)
   except Exception as e:
    print(json.dumps({'stage':'PREP_ROOT_FAILED','reason':reason(e)}),flush=True);time.sleep(30);continue
   if bundle is None:time.sleep(30);continue
   policy=policy_for(bundle)
   def authorize():
    if not policy.time_allows(time.time()):raise ValueError('SESSION_CLOSED')
   def spawn(boot,number):return subprocess.Popen([sys.executable,'-m','ipr.main','--child','--boot-id',boot,'--parent-pid',str(os.getpid()),'--session',day],cwd=ROOT)
   guard[0]=ProcessGuard(client,policy,authorize,spawn,cgroup_sample);result=guard[0].run();guard[0]=None
   print(json.dumps({'stage':'IPR_PARENT_EXIT',**result}),flush=True)
   if result.get('action')=='WORKER_EXIT' and result.get('exit_code')==75:continue
   parked_day=day
  return 0
 except Exception as e:
  print(json.dumps({'stage':'IPR_FATAL','reason':reason(e)}),flush=True);return 2
if __name__=='__main__':raise SystemExit(main())
