"""Reviewed alternative entrypoint. Parent does NOT import numpy or app engines.

No activation by import. --policy is mandatory; expired/invalid policy parks.
Only the child runs the unchanged startup and decision code.
"""
import argparse,json,os,signal,subprocess,sys,time,uuid
from pathlib import Path
from datetime import datetime,timezone
from restart_budget import SessionPolicy,RedisBudget,MAX_RESTARTS,EXIT_RESTART_REQUESTED
from session_policy_tool import read_bound_policy

class ParentController:
    def __init__(self,policy,budget,boot_id=None,wall=time.time):
        self.policy=policy;self.budget=budget;self.boot_id=boot_id or uuid.uuid4().hex;self.wall=wall;self.boot_number=None
    def preflight(self):
        if not self.policy.time_allows(self.wall()):return 'SESSION_CLOSED'
        try:status,n=self.budget.claim(self.policy,self.boot_id)
        except Exception as exc:
            transient=type(exc).__name__ in {'TimeoutError','ConnectionError','ConnectionResetError','BusyLoadingError'}
            return ('BUDGET_UNAVAILABLE:' if transient else 'BUDGET_FATAL:')+type(exc).__name__
        if status=='INITIALIZED_BY_FIRST_CLAIM':
            parent_log(status,policy_sha256=self.policy.digest,boot_id=self.boot_id,boots=n)
            status='ALLOW'
        if status!='ALLOW':return status
        if not 1<=n<=MAX_RESTARTS+1:return 'BUDGET_RESPONSE_INVALID'
        if not self.policy.time_allows(self.wall()):return 'SESSION_CLOSED_AFTER_CLAIM'
        self.boot_number=n;return 'ALLOW'
    def after_worker_exit(self,external_stop=False):
        if external_stop:return 'EXTERNAL_STOP'
        if not self.policy.time_allows(self.wall()):return 'SESSION_CLOSED'
        if self.boot_number is None or self.boot_number>=MAX_RESTARTS+1:return 'BUDGET_EXHAUSTED'
        try:status,n=self.budget.restart(self.policy,self.boot_id)
        except Exception as exc:
            transient=type(exc).__name__ in {'TimeoutError','ConnectionError','ConnectionResetError','BusyLoadingError'}
            return ('BUDGET_UNAVAILABLE:' if transient else 'BUDGET_FATAL:')+type(exc).__name__
        if status!='ALLOW':return status
        if not self.policy.time_allows(self.wall()):return 'SESSION_CLOSED_AFTER_RESTART_CHECK'
        return 'RESTART_REQUESTED'

def parent_log(stage,**fields):
    print(json.dumps({'stage':stage,'utc':datetime.now(timezone.utc).isoformat(),'actionable_alerts':False,**fields},sort_keys=True,separators=(',',':')),flush=True)

def park(reason):
    # Keep the GUARD alive; no child, no lease renewal, no market ingestion.
    signal.signal(signal.SIGTERM,signal.SIG_DFL)
    signal.signal(signal.SIGINT,signal.SIG_DFL)
    while True:
        parent_log('GUARD_PARKED',reason=reason,application_running=False,manual_action_required=True)
        time.sleep(60)

def load_policy(path,env=os.environ):
    return read_bound_policy(Path(path).read_text(),env)

def create_budget_client(url):
    from redis import Redis
    from redis.retry import Retry
    from redis.backoff import NoBackoff
    return Redis.from_url(url,decode_responses=True,socket_connect_timeout=5,socket_timeout=5,retry_on_timeout=False,retry=Retry(NoBackoff(),0))

def startup_claim(controller, *, sleeper=time.sleep, monotonic=time.monotonic):
    # Same boot_id is reused after ambiguous Redis responses: claim is idempotent.
    # Guard-only attempts do not create an application/leadership/WS connection.
    started=monotonic()
    for attempt,delay in enumerate((0,5,15),1):
        if delay:sleeper(delay)
        status=controller.preflight()
        if not status.startswith('BUDGET_UNAVAILABLE:'):return status
        parent_log('GUARD_REDIS_STARTUP_RETRY',attempt=attempt,max_attempts=3,error_type=status.split(':',1)[1],application_running=False)
    # A continuing outage cannot be durably counted in unreachable Redis.
    # Bound the exit frequency instead; never guess/reset a budget.
    remaining=max(0,120-(monotonic()-started))
    if remaining:sleeper(remaining)
    if not controller.policy.time_allows(controller.wall()):return 'SESSION_CLOSED'
    parent_log('GUARD_REDIS_STARTUP_EXHAUSTED',exit_code=75,min_guard_lifetime_sec=120,application_running=False)
    return 'TRANSIENT_STARTUP_EXIT'

def restart_cooldown(policy,started,*,stopped=lambda:False,wall=time.time,sleeper=time.sleep,monotonic=time.monotonic):
    # Guard-only delay, never extends the worker's existing 60s shutdown tail.
    parent_log('GUARD_RESTART_COOLDOWN',min_guard_lifetime_sec=120,application_running=False)
    while monotonic()-started<120:
        if stopped() or not policy.time_allows(wall()):return False
        sleeper(min(1,120-(monotonic()-started)))
    return not stopped() and policy.time_allows(wall())

def run_child(args,policy):
    # Linux parent-death signal prevents a surviving application after guard death.
    if sys.platform!='linux':raise RuntimeError('LINUX_PARENT_DEATH_SIGNAL_REQUIRED')
    import ctypes
    if ctypes.CDLL(None,use_errno=True).prctl(1,signal.SIGKILL,0,0,0)!=0:raise RuntimeError('PRCTL_FAILED')
    if os.getppid()!=args.parent_pid:return 0
    import asyncio
    from operational_priority_radar import WorkerConfig
    import shadow_worker_main as original
    from guarded_runtime import GuardedRuntime
    from metrics_stdout import JsonStdout,serialized_prints
    loop=asyncio.new_event_loop();asyncio.set_event_loop(loop)
    async def worker():
        if not policy.time_allows(time.time()):return 'SESSION_CLOSED_IN_CHILD'
        p=original.startup_probe_env()
        if not p['ok']:raise original.StartupBlocked(str(p))
        cfg=WorkerConfig.from_env();cfg.validate()
        supervisor=original.build_runtime(cfg) # Current composition/startup, unchanged.
        from messages_only_runtime import install_messages_only
        install_messages_only(supervisor,policy) # default false; original shadow/trust gates unchanged
        sink=JsonStdout(identity={'boot_id':args.boot_id,'worker_id':supervisor.orchestrator.worker_id,'session':policy.session})
        runtime=GuardedRuntime(supervisor,original,policy,args.boot_id,args.boot_number,sink)
        for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,runtime.signal_stop)
        with serialized_prints():
            outcome=await runtime.run()
        return outcome.reason
    try:
        reason=loop.run_until_complete(worker())
        # SELF_STOP_OUTCOME already passed through the bounded stdout writer.
        # Do not perform a new synchronous print before os._exit.
    except BaseException as exc:
        import threading
        log=threading.Thread(target=parent_log,args=('WORKER_FATAL',),kwargs={'error_type':type(exc).__name__,'error':str(exc)[:160],'boot_id':args.boot_id},daemon=True)
        log.start();log.join(.05) # best effort; a blocked log pipe cannot veto worker exit
    # Intentionally bypass executor-join on a dying WORKER only. Parent decides
    # restart vs park and remains alive when budget/session forbids a restart.
    os._exit(EXIT_RESTART_REQUESTED)

def main():
    guard_started=time.monotonic()
    parser=argparse.ArgumentParser();source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--policy');source.add_argument('--policy-env')
    parser.add_argument('--initialize-budget',action='store_true')
    parser.add_argument('--worker-child',action='store_true');parser.add_argument('--parent-pid',type=int,default=0)
    parser.add_argument('--boot-id');parser.add_argument('--boot-number',type=int,default=0)
    args=parser.parse_args()
    try:
        policy_text=Path(args.policy).read_text() if args.policy else os.environ[args.policy_env]
        policy=read_bound_policy(policy_text,os.environ)
    except Exception as exc:
        if args.worker_child:raise
        return park('POLICY_INVALID:'+type(exc).__name__)
    if args.worker_child:return run_child(args,policy)
    # Reject stale session BEFORE connecting to Redis or constructing a worker.
    if not policy.time_allows(time.time()):return park('SESSION_CLOSED')
    try:
        import redis
        if not os.environ.get('REDIS_URL'):return park('REDIS_CONFIG_MISSING')
        client=create_budget_client(os.environ['REDIS_URL'])
        budget=RedisBudget(client)
        if args.initialize_budget:
            status,n=budget.initialize(policy)
            parent_log('BUDGET_INITIALIZATION',status=status,boots=n,policy_sha256=policy.digest)
            return 0 if status in ('INITIALIZED','EXISTS') else park(status)
        controller=ParentController(policy,budget)
        status=startup_claim(controller)
    except Exception as exc:return park('BUDGET_UNAVAILABLE:'+type(exc).__name__)
    if status=='TRANSIENT_STARTUP_EXIT':return EXIT_RESTART_REQUESTED
    if status!='ALLOW':return park(status)
    parent_log('GUARD_BOOT_CLAIMED',boot_id=controller.boot_id,boot_number=controller.boot_number,max_application_boots=MAX_RESTARTS+1)
    child_env=dict(os.environ);child_env['OPR_VALIDATED_CHILD_POLICY']=policy_text
    child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker-child','--policy-env','OPR_VALIDATED_CHILD_POLICY',
                            '--parent-pid',str(os.getpid()),'--boot-id',controller.boot_id,'--boot-number',str(controller.boot_number)],env=child_env)
    external=[False];stop_started=[None]
    def stop(signum,frame):
        external[0]=True
        if stop_started[0] is None:stop_started[0]=time.monotonic()
        if child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    # Poll without new application requests. SIGTERM gets the unchanged 60s tail.
    while child.poll() is None:
        if external[0]:
            if time.monotonic()-stop_started[0]>=60:child.kill()
        delay=1.0 if stop_started[0] is None else min(1.0,max(0.0,60-(time.monotonic()-stop_started[0])))
        time.sleep(delay)
    decision=controller.after_worker_exit(external[0])
    parent_log('GUARD_CHILD_EXIT',child_exit_code=child.returncode,decision=decision,boot_number=controller.boot_number)
    if decision=='EXTERNAL_STOP':return 0
    if decision=='RESTART_REQUESTED' or decision.startswith('BUDGET_UNAVAILABLE:'):
        # Last local date fence. Render may start a replacement later; its
        # preflight will refuse to start the application after session_end.
        if restart_cooldown(policy,guard_started,stopped=lambda:external[0]):return EXIT_RESTART_REQUESTED
        if external[0]:return 0
        decision='SESSION_CLOSED_BEFORE_PARENT_EXIT'
    return park(decision)

if __name__=='__main__':raise SystemExit(main())
