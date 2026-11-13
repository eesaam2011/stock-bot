"""R10.9 offline production-pipeline, transport and actual-loop contracts."""
import asyncio,contextlib,copy,hashlib,io,json,threading,unittest,urllib.error
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch
from ipr.api import APIError
from ipr import review_flags
from ipr.native_resilience import NativeResilience,resilience_for,watch_native_health
from dintegration.native_timing import NativeTiming
from dintegration.measure import Measurements
from r108_support import native_fixture,AT,POLICY,Response,packed
from r9core.chain import ChainFailure
from leadership import NotLeader
from test_r71 import nested

class NativeFailureTests(unittest.TestCase):
 def fixture(self,n=201,timing=True):
  s,h,a,r,p,store,b=native_fixture(n);t=NS(at=AT,mono=0.);logs=[];m=None
  if timing:
   m=NativeTiming(p,r,POLICY,wall=lambda:t.at,clock=lambda:t.mono,emit=lambda line:None);m.install(b);self.addCleanup(m.close)
  a.timing_clock=lambda:t.mono
  z=NativeResilience(a,b,p.session,clock=lambda:t.mono,wall=lambda:t.at,emit=logs.append);z.tick(True)
  return s,h,a,r,p,store,b,t,logs,z,m
 def work(self,f):
  s,h,a,r,p,store,b,t,logs,z,m=f
  with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):return z.work(s,t.at)
 def advance(self,t,s):t.mono+=s;t.at+=timedelta(seconds=s)
 def fail_on(self,f,number,error=None):
  s,h,a,r,p,store,b,t,logs,z,m=f;old=h.open;count=0
  def opener(req,timeout):
   nonlocal count
   count+=1;self.assertEqual(timeout,20)
   if count==number:self.advance(t,20.17);raise error or TimeoutError('fixture secret must not be logged')
   return old(req,timeout)
  # Preserve both installed opener observers, change only the HTTP fake's target.
  # Replacing h.open would not alter its bound method, so wrap the lowest
  # existing opener and delegate every successful request through it.
  old_installed=a.opener
  def outer(req,timeout):
   nonlocal count
   count+=1;self.assertEqual(timeout,20)
   if count==number:
    # Raise inside the actual Alpaca.call opener boundary; count via outer
    # resilience hook is tested separately with read failure below.
    self.advance(t,20.17);raise error or TimeoutError('fixture secret must not be logged')
   return old_installed(req,timeout)
  a.opener=outer
 def test_timeout_inside_Alpaca_is_nonfatal_no_partial_E_or_decision(self):
  f=self.fixture(500);s,h,a,r,p,store,b,t,logs,z,m=f;before={'existing':{'state':'MONITORING','stop':1.}}
  store.update(copy.deepcopy(before));evaluations=[];decisions=[];orig=p.ec.evaluate_with_telemetry
  p.ec.evaluate_with_telemetry=lambda *v:(evaluations.append(v) or orig(*v));p._confluence=lambda *v:decisions.append(v)
  # Last of the five physical batches fails, after four complete responses.
  self.fail_on(f,5);self.assertIsNone(self.work(f));self.assertEqual(store,before);self.assertEqual(evaluations,[]);self.assertEqual(decisions,[])
  line=next(x for x in logs if x['stage']=='R109_NATIVE_FAILED');self.assertEqual(line['exception_type'],'TimeoutError');self.assertAlmostEqual(line['cycle_seconds'],20.17);self.assertAlmostEqual(line['rest_hold_seconds'],20.17)
  self.assertNotIn('fixture secret',json.dumps(logs));self.assertEqual(z.snapshot()['total_failures_UTC_day'],1);self.assertEqual(z.snapshot()['observer_errors'],0)
 def test_real_http_attempt_count_and_rest_hold_on_read_timeout(self):
  f=self.fixture(1);s,h,a,r,p,store,b,t,logs,z,m=f
  old=a.opener
  def op(*args,**kw):
   response=old(*args,**kw)
   def read(*args):self.advance(t,20.17);raise TimeoutError('do not print')
   response.read=read;return response
  a.opener=op
  self.assertIsNone(self.work(f));row=z.last_failure;self.assertEqual(row['request_count'],1);self.assertAlmostEqual(row['rest_hold_seconds'],20.17);self.assertEqual(a.lock_metrics()['active_request_sections'],0)
 def test_pagination_failure_before_evaluation(self):
  f=self.fixture(1);s,h,a,r,p,store,b,t,logs,z,m=f;old=a.opener;calls=0
  def op(req,timeout):
   nonlocal calls
   calls+=1
   if calls==2:raise TimeoutError('page two')
   return Response(packed({'bars':{s[0]:[{'t':(AT-timedelta(minutes=5)).isoformat(),'o':10.,'h':11.,'l':9.,'c':10.,'v':10,'n':1,'vw':10.}]},'next_page_token':'second'}))
  a.opener=op;seen=[];p.ec.evaluate_with_telemetry=lambda *v:seen.append(v)
  self.assertIsNone(self.work(f));self.assertEqual(calls,2);self.assertEqual(seen,[]);self.assertEqual(store,{})
 def test_three_failures_drop_trust_first_success_restores(self):
  f=self.fixture(1);s,h,a,r,p,store,b,t,logs,z,m=f;h.error=TimeoutError('fixture')
  for i in range(3):self.assertIsNone(self.work(f));self.advance(t,5.)
  self.assertFalse(z.snapshot()['native_trusted']);self.assertEqual(z.consecutive,3)
  h.error=None;result=self.work(f);self.assertEqual(result['early_core_persisted'],1);self.assertTrue(z.trusted);self.assertEqual(z.consecutive,0);self.assertEqual(z.total,3)
  self.assertTrue(any(x.get('reason')=='COMPLETE_NATIVE_FETCH_VALIDATED' for x in logs));self.assertEqual(z.snapshot()['observer_errors'],0)
 def test_ninety_seconds_without_success_and_boundary(self):
  f=self.fixture(1);t=f[7];z=f[9]
  self.advance(t,89.999);z.tick(True);self.assertTrue(z.trusted)
  self.advance(t,.001);z.tick(True);self.assertFalse(z.trusted)
  self.work(f);self.assertTrue(z.trusted)
 def test_ten_minutes_continuous_failure_is_fatal(self):
  f=self.fixture(1);h=f[1];t=f[7];z=f[9];h.error=TimeoutError('fixture')
  self.work(f);self.advance(t,599.999);z.tick(True)
  self.advance(t,.001)
  with self.assertRaisesRegex(APIError,'ALPACA_READ_FAILED'):z.tick(True)
  self.assertEqual(f[8][-1]['stage'],'R109_NATIVE_FATAL')
 def test_first_success_breaks_continuous_failure_span(self):
  f=self.fixture(1);h=f[1];t=f[7];z=f[9];h.error=TimeoutError();self.work(f);self.advance(t,599.)
  h.error=None;self.work(f);self.assertIsNone(z.failure_since);self.advance(t,600.);z.tick(True);self.assertFalse(z.trusted)
 def test_success_arriving_after_600_cannot_emit_E(self):
  f=self.fixture(1);h=f[1];a=f[2];t=f[7];z=f[9];h.error=TimeoutError();self.work(f);h.error=None
  old=a.opener
  def op(*v,**kw):self.advance(t,600.);return old(*v,**kw)
  a.opener=op
  with self.assertRaises(APIError):self.work(f)
  self.assertEqual(f[5],{})
 def test_daily_totals_reset_consecutive_retained_until_success(self):
  f=self.fixture(1);f[1].error=TimeoutError();self.work(f);self.advance(f[7],86400.)
  snap=f[9].snapshot();self.assertEqual(snap['total_failures_UTC_day'],0);self.assertEqual(snap['consecutive_failures'],1)
 def test_non_transport_APIError_is_fatal(self):
  for err in (APIError('ALPACA_READ_FAILED'),APIError('RESPONSE_TOO_LARGE'),APIError('HTTP_NOT_200'),ValueError('bad JSON')):
   with self.subTest(err=type(err).__name__+str(err)):
    f=self.fixture(1,False);f[1].error=err
    with self.assertRaises(APIError):self.work(f)
    self.assertEqual(f[9].total,0)
 def test_http_429_no_new_retry_and_fatal(self):
  f=self.fixture(1,False);f[1].error=urllib.error.HTTPError('https://fixture',429,'rate',{},None)
  with self.assertRaisesRegex(APIError,'ALPACA_HTTP_429'):self.work(f)
  self.assertEqual(len(f[1].calls),1);self.assertEqual(f[2].http_429[('shared','native')],1);self.assertEqual(f[9].total,0)
 def test_redis_hook_timeout_not_misclassified_as_http(self):
  f=self.fixture(1,False)
  f[2].before_request=lambda:(_ for _ in ()).throw(TimeoutError('Redis fixture'))
  with self.assertRaises(APIError):self.work(f)
  self.assertEqual(f[9].total,0)
 def test_chain_leadership_persistence_and_safety_errors_remain_fatal(self):
  for err in (ChainFailure('CHAIN'),NotLeader('LEADER'),OSError('PERSISTENCE'),RuntimeError('SafetyStop'),TimeoutError('Redis')):
   with self.subTest(type=type(err).__name__):
    f=self.fixture(1,False);p=f[4];p.r.mget=lambda *v:(_ for _ in ()).throw(err)
    with self.assertRaises(type(err)) as cm:self.work(f)
    self.assertIs(cm.exception,err);self.assertEqual(f[9].total,0)
 def test_post_fetch_exception_cannot_be_swallowed(self):
  f=self.fixture(1);p=f[4];err=APIError('ALPACA_READ_FAILED');p.ec.evaluate_with_telemetry=lambda *v:(_ for _ in ()).throw(err)
  with self.assertRaises(APIError) as cm:self.work(f)
  self.assertIs(cm.exception,err);self.assertEqual(f[9].total,0)
 def test_bound_violation_fatal_before_any_request(self):
  f=self.fixture(501,False)
  with self.assertRaisesRegex(RuntimeError,'R109_NATIVE_ATOMIC_BATCH_BOUND'):self.work(f)
  self.assertEqual(f[1].calls,[]);self.assertEqual(f[5],{})
 def test_other_thread_recover_bars_does_not_change_health(self):
  f=self.fixture(1,False);s,h,a,r,p,store,b,t,logs,z,m=f;h.error=TimeoutError()
  with a.operation('incremental'):
   with self.assertRaises(APIError):r.bars_multi(s,AT-timedelta(minutes=10),AT,'5Min')
  self.assertEqual(z.total,0);self.assertEqual(logs,[])
 def test_native_timing_disabled_still_measures_failure_and_no_E(self):
  f=self.fixture(1,False);f[1].error=TimeoutError();self.assertIsNone(self.work(f));self.assertEqual(f[9].total,1);self.assertEqual(f[9].last_failure['request_count'],1)
 def test_no_duplicate_opportunity_after_retry(self):
  f=self.fixture(1);f[1].error=TimeoutError();self.work(f);f[1].error=None
  self.work(f);before=copy.deepcopy(f[5]);self.work(f);self.assertEqual(f[5],before);self.assertEqual(len(before),1)
 def test_observer_failure_cannot_replace_original_error(self):
  f=self.fixture(1,False);f[9].emit=lambda *v:(_ for _ in ()).throw(OSError('LOG'));f[1].error=TimeoutError()
  self.assertIsNone(self.work(f));self.assertEqual(f[9].total,1);self.assertEqual(f[9].observer_errors,1)
 def test_metrics_attached_and_disabled_is_original(self):
  f=self.fixture(1,False);s,h,a,r,p,store,b,t,logs,z,m=f;runtime=NS(a=NS(bridge=b));old=a.opener;oldbars=r.bars_multi
  with patch.object(review_flags,'NATIVE_FAILURE_NONFATAL_ENABLED',False):self.assertIsNone(resilience_for(runtime,a,{'session':p.session}))
  self.assertIs(a.opener,old);self.assertIs(r.bars_multi,oldbars)
  measure=Measurements();r.measure=measure;res=resilience_for(runtime,a,{'session':p.session});snap=measure.sample()['native_resilience'];self.assertEqual(snap['observer_errors'],0);self.assertTrue(snap['enabled'])
 def test_health_timer_marks_untrusted_while_native_is_blocked(self):
  f=self.fixture(1,False);s,h,a,rest,p,store,b,t,logs,z,m=f
  entered=threading.Event();release=threading.Event();old=a.opener
  def op(*v,**kw):
   entered.set()
   if not release.wait(3.):raise AssertionError('fixture synchronization timeout')
   return old(*v,**kw)
  a.opener=op;stop=NS(value=False);stop.is_set=lambda:stop.value
  runtime=NS(stop=stop,wall=lambda:t.at,a=NS(bridge=b),native_resilience=z)
  bundle={'market':{'open':(AT-timedelta(hours=1)).isoformat(),'close':(AT+timedelta(hours=1)).isoformat()}}
  pauses=[]
  async def timer_pause(stop,seconds):
   pauses.append(seconds);self.advance(t,seconds)
   if t.mono>=91.:
    self.assertFalse(z.snapshot()['native_trusted']);self.assertEqual(store,{})
    stop.value=True
   await asyncio.sleep(0)
  async def scenario():
   work=asyncio.create_task(asyncio.to_thread(self.work,f))
   while not entered.is_set():await asyncio.sleep(0)
   try:await watch_native_health(runtime,a,bundle,timer_pause)
   finally:release.set()
   return await work
  result=asyncio.run(scenario());self.assertEqual(result['early_core_persisted'],1);self.assertTrue(z.trusted);self.assertTrue(all(x==1. for x in pauses));self.assertEqual(z.observer_errors,0)
 def test_health_timer_fatal_reaches_original_IPR_FATAL_handler(self):
  from test_r93 import ChildTelemetry
  f=self.fixture(1,False);f[1].error=TimeoutError();self.work(f);t=f[7];z=f[9]
  runtime=NS(stop=NS(is_set=lambda:False),wall=lambda:t.at,a=NS(bridge=f[6]),native_resilience=z)
  bundle={'market':{'open':(AT-timedelta(hours=1)).isoformat(),'close':(AT+timedelta(hours=1)).isoformat()}}
  async def timer_pause(stop,seconds):self.advance(t,seconds)
  try:asyncio.run(watch_native_health(runtime,f[2],bundle,timer_pause))
  except APIError as err:
   code,rows=ChildTelemetry().invoke(err)
   self.assertEqual(code,2);self.assertEqual([x['stage'] for x in rows],['IPR_FATAL','IPR_CHILD_EXIT']);self.assertEqual(rows[0]['reason'],'ALPACA_READ_FAILED')
  else:self.fail('continuous failure did not return IPR_FATAL')
 def test_actual_native_loop_resumes_after_original_five_second_pause(self):
  f=self.fixture(1,False);s,h,a,r,p,store,b,t,logs,z,m=f;h.error=TimeoutError('fixture');stop=NS(value=False);stop.is_set=lambda:stop.value
  runtime=NS(stop=stop,wall=lambda:t.at,a=NS(bridge=b,control_lock=threading.Lock(),transport_trusted=True,recovering=set()),c=NS(bar=s,proofs={x:{'history':True,'continuity':True,'reconciled':True} for x in s}),native_resilience=z)
  pauses=[]
  async def pause(stop,seconds):
   pauses.append(seconds);self.advance(t,seconds);h.error=None
   if len(pauses)==2:stop.value=True
  async def to_thread(fn):return fn()
  env={'r':runtime,'api':a,'bundle':{'session':p.session,'market':{'open':AT-timedelta(hours=1),'close':AT+timedelta(hours=1)}},'state':{'ready':True,'scan_ok':True},'dt':lambda x:x,'asyncio':NS(to_thread=to_thread),'pause':pause}
  with contextlib.redirect_stdout(io.StringIO()):asyncio.run(nested('native',env)())
  self.assertEqual(pauses,[5.,5.]);self.assertEqual(z.total,1);self.assertEqual(z.successes,1);self.assertEqual(len(store),1)
 def test_flag_false_actual_native_loop_keeps_fatal_error(self):
  f=self.fixture(1,False);s,h,a,r,p,store,b,t,logs,z,m=f;h.error=TimeoutError();stop=NS(is_set=lambda:False);runtime=NS(stop=stop,wall=lambda:AT,a=NS(bridge=b,control_lock=threading.Lock(),transport_trusted=True,recovering=set()),c=NS(bar=s,proofs={x:{'history':True,'continuity':True,'reconciled':True} for x in s}))
  async def to_thread(fn):return fn()
  async def pause(*v):self.fail('fatal original path must not pause')
  env={'r':runtime,'api':a,'bundle':{'session':p.session,'market':{'open':AT-timedelta(hours=1),'close':AT+timedelta(hours=1)}},'state':{'ready':True,'scan_ok':True},'dt':lambda x:x,'asyncio':NS(to_thread=to_thread),'pause':pause}
  with patch.object(review_flags,'NATIVE_FAILURE_NONFATAL_ENABLED',False),contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaises(APIError):asyncio.run(nested('native',env)())

if __name__=='__main__':unittest.main()
