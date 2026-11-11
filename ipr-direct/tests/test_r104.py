"""R10.4 transport lanes. All openers are local fakes; no service/Redis I/O."""
import asyncio,contextlib,io,json,threading,time,unittest,urllib.error,urllib.parse
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace as NS
from ipr import api as module
from ipr.api import Alpaca,APIError,_RequestTurn
from ipr.wave import HeavyWindow
from dintegration.rest import MarketREST
from test_r71 import Sim,nested
from r104_load import load_case

ENV={'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'}
class Cancelled(BaseException):pass
class Response:
 status=200
 def __init__(self,body=b'{}'):self.body=body
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def read(self,n):return self.body

class Lanes(unittest.TestCase):
 def setUp(self):self.flag=patch.object(module,'SCAN_LANE_ENABLED',True);self.flag.start();self.addCleanup(self.flag.stop)
 def api(self,opener=None):return Alpaca(ENV,opener=opener or (lambda *a,**kw:Response()))
 def wait(self,predicate):
  until=time.monotonic()+3
  while time.monotonic()<until:
   if predicate():return
   time.sleep(.001)
  self.fail('local fixture did not reach expected state')
 def join(self,*threads):
  for t in threads:t.join(3);self.assertFalse(t.is_alive(),'local fixture thread did not finish')
 def free(self,a):
  for lock in (a.lock,a.scan_lock):self.assertTrue(lock.acquire(False));lock.release()
  for turn in a.turns.values():
   with turn.condition:self.assertFalse(turn.busy);self.assertEqual(len(turn.queue),0)
  self.assertEqual(a.lock_metrics()['active_request_sections'],0)
 def test_flag_and_two_distinct_locks(self):
  a=self.api();self.assertIs(module.SCAN_LANE_ENABLED,True);self.assertIsNot(a.lock,a.scan_lock)
 def test_scan_passes_while_original_lock_is_held(self):
  a=self.api();done=threading.Event()
  def scan():
   with a.operation('scan_rest'),a.serialized_request():done.set()
  with a.lock:
   t=threading.Thread(target=scan,daemon=True);t.start();self.assertTrue(done.wait(2))
  self.join(t);self.free(a)
 def test_all_other_paths_stay_on_original_lock(self):
  for operation in ('native','incremental','other','unknown'):
   a=self.api();entered=threading.Event()
   def work():
    with a.operation(operation),a.serialized_request():entered.set()
   with a.lock:
    t=threading.Thread(target=work,daemon=True);t.start();self.wait(lambda:len(a.turns['shared'].queue)==0 and a.turns['shared'].busy)
    self.assertFalse(entered.is_set())
   self.join(t);self.assertTrue(entered.is_set());self.free(a)
 def test_scan_rest_threadlocal_fallback_is_routed(self):
  a=self.api();a.request_operation=lambda:'scan_rest'
  with a.lock,a.serialized_request():pass
  self.assertEqual(a.lock_metrics()['lanes']['scan']['count'],1)
 def test_operation_override_and_restore(self):
  a=self.api();a.request_operation=lambda:'scan_rest'
  with a.operation('native'):a.get('/v2/stocks/bars',{})
  a.get('/v2/stocks/bars',{})
  self.assertEqual(a.lock_metrics()['lanes']['shared']['count'],1);self.assertEqual(a.lock_metrics()['lanes']['scan']['count'],1)
 def test_disabled_restores_single_lock_and_never_enters_fifo(self):
  a=self.api();entered=threading.Event()
  def work():
   with a.operation('scan_rest'),a.serialized_request():entered.set()
  with patch.object(module,'SCAN_LANE_ENABLED',False),a.lock:
   t=threading.Thread(target=work,daemon=True);t.start();self.assertFalse(entered.wait(.03));self.assertFalse(a.turns['scan'].busy)
  self.join(t);self.assertTrue(entered.is_set());self.free(a)
 def test_total_HTTP_concurrency_at_most_two_and_one_per_lane(self):
  a=None;guard=threading.Lock();state={'active':0,'peak':0,'by':{'scan':0,'shared':0},'per_peak':{'scan':0,'shared':0}};errors=[]
  class R(Response):
   def __enter__(self):
    self.lane='scan' if a._operation()=='scan_rest' else 'shared'
    with guard:
     state['active']+=1;state['peak']=max(state['peak'],state['active']);state['by'][self.lane]+=1;state['per_peak'][self.lane]=max(state['per_peak'][self.lane],state['by'][self.lane])
    return self
   def read(self,n):time.sleep(.002);return b'{}'
   def __exit__(self,*args):
    with guard:state['active']-=1;state['by'][self.lane]-=1
  a=self.api(lambda *a,**kw:R())
  def work(op):
   try:
    with a.operation(op):
     for _ in range(8):a.get('/v2/stocks/bars',{})
   except BaseException as e:errors.append(e)
  threads=[threading.Thread(target=work,args=(op,),daemon=True) for op in ['scan_rest']*4+['native','incremental','other']*3]
  for t in threads:t.start()
  self.join(*threads);self.assertEqual(errors,[]);self.assertEqual(state['peak'],2);self.assertEqual(state['per_peak'],{'scan':1,'shared':1});self.assertEqual(a.requests,104);self.free(a)
 def test_FIFO_no_shared_path_starved_by_scan_or_new_waiters(self):
  a=self.api();order=[];stop=threading.Event()
  def scan():
   while not stop.is_set():
    with a.operation('scan_rest'),a.serialized_request():time.sleep(.0002)
  def work(op,n):
   with a.operation(op),a.serialized_request():order.append((op,n))
  scanner=threading.Thread(target=scan,daemon=True);scanner.start();threads=[]
  try:
   with a.operation('other'),a.serialized_request():
    for n in range(12):
     op=('native','incremental','other')[n%3];t=threading.Thread(target=work,args=(op,n),daemon=True);t.start();threads.append(t);self.wait(lambda:len(a.turns['shared'].queue)==len(threads))
   self.join(*threads);self.assertEqual(order,[(('native','incremental','other')[n%3],n) for n in range(12)])
  finally:stop.set();self.join(scanner)
  self.free(a)
 def test_FIFO_interrupted_waiter_does_not_leave_ticket(self):
  turn=_RequestTurn()
  with turn.turn():
   with patch.object(turn.condition,'wait',side_effect=Cancelled('cancelled')):
    with self.assertRaises(Cancelled):
     with turn.turn():pass
   self.assertEqual(len(turn.queue),0)
  with turn.turn():pass
  self.assertFalse(turn.busy)
 def test_exceptions_and_BaseException_release_both_lanes(self):
  for op in ('scan_rest','native'):
   for exc in (ValueError('application'),Cancelled('cancelled'),KeyboardInterrupt()):
    a=self.api()
    with self.assertRaises(type(exc)) as cm:
     with a.operation(op),a.serialized_request():raise exc
    self.assertIs(cm.exception,exc);self.free(a)
 def test_acquire_error_releases_FIFO_turn(self):
  a=self.api()
  class Lock:
   def acquire(self):raise Cancelled('before acquisition')
  a.scan_lock=Lock()
  with self.assertRaises(Cancelled):
   with a.operation('scan_rest'),a.serialized_request():pass
  self.assertFalse(a.turns['scan'].busy);self.assertEqual(len(a.turns['scan'].queue),0)
 def test_clock_error_after_acquisition_still_releases(self):
  a=self.api();times=iter([0.,Cancelled('clock'),2.])
  def clock():
   v=next(times)
   if isinstance(v,BaseException):raise v
   return v
  a.timing_clock=clock
  with self.assertRaises(Cancelled):
   with a.operation('scan_rest'),a.serialized_request():pass
  a.timing_clock=time.monotonic;self.free(a)
 def test_observer_and_counter_faults_do_not_hold_turn_or_mask_error(self):
  a=self.api();original=ValueError('application')
  with patch.object(a,'_timing',side_effect=RuntimeError('telemetry')):
   with a.observe_requests(lambda *a:(_ for _ in ()).throw(RuntimeError('observer'))):
    with self.assertRaises(ValueError) as cm:
     with a.operation('scan_rest'),a.serialized_request():raise original
  self.assertIs(cm.exception,original);self.free(a)
 def test_observer_runs_after_both_turn_and_lock_are_released(self):
  a=self.api();seen=[]
  def observe(*v):
   seen.append((a.turns['scan'].busy,a.scan_lock.locked()))
  with a.observe_requests(observe),a.operation('scan_rest'),a.serialized_request():pass
  self.assertEqual(seen,[(False,False)])
 def test_async_cancel_keeps_worker_slot_until_HTTP_body_ends(self):
  a=self.api();entered=threading.Event();release=threading.Event();finished=threading.Event()
  def work():
   try:
    with a.operation('scan_rest'),a.serialized_request():entered.set();release.wait(2)
   finally:finished.set()
  async def go():
   task=asyncio.create_task(asyncio.to_thread(work))
   while not entered.is_set():await asyncio.sleep(.001)
   task.cancel()
   with self.assertRaises(asyncio.CancelledError):await task
   self.assertFalse(a.scan_lock.acquire(False),'cannot release slot while worker still owns HTTP')
   with a.operation('native'),a.serialized_request():pass
   release.set()
   while not finished.is_set():await asyncio.sleep(.001)
  asyncio.run(go());self.free(a)
 def test_read_timeout_errors_release_without_retry(self):
  for op in ('scan_rest','native'):
   calls=[]
   def opener(*args,**kw):calls.append(kw);raise TimeoutError('synthetic')
   a=self.api(opener)
   with a.operation(op),self.assertRaisesRegex(APIError,'ALPACA_READ_FAILED'):a.get('/v2/stocks/bars',{})
   self.assertEqual(len(calls),1);self.assertEqual(calls[0]['timeout'],20);self.free(a)
 def test_read_and_JSON_errors_release(self):
  class Bad(Response):
   def read(self,n):raise TimeoutError('read')
  for response in [Bad(),Response(b'{bad')]:
   a=self.api(lambda *a,**kw:response)
   with a.operation('scan_rest'),self.assertRaisesRegex(APIError,'ALPACA_READ_FAILED'):a.get('/v2/stocks/bars',{})
   self.free(a)
 def test_HTTP429_has_one_attempt_and_explicit_lane_operation_count(self):
  for op,lane in [('scan_rest','scan'),('native','shared'),('incremental','shared'),('other','shared')]:
   calls=[]
   def opener(*a,**kw):calls.append(1);raise urllib.error.HTTPError('https://synthetic.invalid',429,'synthetic',{'Retry-After':'99'},None)
   a=self.api(opener)
   with a.operation(op),self.assertRaisesRegex(APIError,'^ALPACA_HTTP_429$'):a.get('/v2/stocks/bars',{})
   m=a.lock_metrics();self.assertEqual(len(calls),1);self.assertEqual(m['http_429_total'],1);self.assertEqual(m['lanes'][lane]['http_429_count'],1);self.assertEqual(m['http_429_by_operation'][op],1);self.free(a)
 def test_429_counter_failure_preserves_original_API_error(self):
  def opener(*a,**kw):raise urllib.error.HTTPError('https://synthetic.invalid',429,'synthetic',{},None)
  a=self.api(opener)
  class Broken(dict):
   def get(self,*a):raise RuntimeError('telemetry')
  a.http_429=Broken()
  with a.operation('scan_rest'),self.assertRaisesRegex(APIError,'^ALPACA_HTTP_429$'):a.get('/v2/stocks/bars',{})
  a.http_429={};self.free(a)
 def test_returned_429_status_keeps_HTTP_NOT_200_behavior(self):
  class RateLimited(Response):status=429
  a=self.api(lambda *a,**kw:RateLimited())
  with self.assertRaisesRegex(APIError,'^HTTP_NOT_200$'):a.get('/v2/stocks/bars',{})
  self.assertEqual(a.lock_metrics()['http_429_total'],1);self.free(a)
 def test_HTTP500_not_counted_as_429_and_no_retry(self):
  calls=[]
  def opener(*a,**kw):calls.append(1);raise urllib.error.HTTPError('https://synthetic.invalid',500,'synthetic',{},None)
  a=self.api(opener)
  with self.assertRaisesRegex(APIError,'^ALPACA_HTTP_500$'):a.get('/v2/stocks/bars',{})
  self.assertEqual(len(calls),1);self.assertEqual(a.lock_metrics()['http_429_total'],0);self.free(a)
 def test_429_immediate_log_bounded_per_lane_minute_but_all_errors_counted(self):
  def opener(*a,**kw):raise urllib.error.HTTPError('https://synthetic.invalid',429,'synthetic',{},None)
  a=self.api(opener);out=io.StringIO()
  with patch.object(module.time,'time',return_value=120),contextlib.redirect_stdout(out):
   for op in ['scan_rest']*8+['native']*8:
    with a.operation(op),self.assertRaises(APIError):a.get('/v2/stocks/bars',{})
  lines=[json.loads(s) for s in out.getvalue().splitlines()];self.assertEqual(len(lines),2);self.assertEqual({r['lane'] for r in lines},{'scan','shared'});self.assertEqual(a.lock_metrics()['http_429_total'],16)
  with patch.object(module.time,'time',return_value=180),contextlib.redirect_stdout(out):
   with self.assertRaises(APIError):a.get('/v2/stocks/bars',{})
  self.assertEqual(len(out.getvalue().splitlines()),3);self.free(a)
 def test_429_print_failure_never_masks_original_error_or_leaks_lock(self):
  def opener(*a,**kw):raise urllib.error.HTTPError('https://synthetic.invalid',429,'synthetic',{},None)
  a=self.api(opener)
  with patch('builtins.print',side_effect=OSError('log sink')):
   with a.operation('scan_rest'),self.assertRaisesRegex(APIError,'^ALPACA_HTTP_429$'):a.get('/v2/stocks/bars',{})
  self.assertEqual(a.lock_metrics()['http_429_total'],1);self.free(a)
 def test_429_clock_backwards_does_not_reopen_log_budget(self):
  a=self.api();out=io.StringIO()
  with contextlib.redirect_stdout(out):
   for epoch in (180,120,180,181,240):
    with patch.object(module.time,'time',return_value=epoch):a._record_429('scan_rest','scan')
  self.assertEqual(len(out.getvalue().splitlines()),2);self.assertEqual(a.lock_metrics()['http_429_total'],5)
 def test_429_after_failure_is_not_sticky_for_next_request(self):
  calls=[]
  def opener(*a,**kw):
   calls.append(1)
   if len(calls)==1:raise urllib.error.HTTPError('https://synthetic.invalid',429,'synthetic',{},None)
   return Response()
  a=self.api(opener)
  with self.assertRaises(APIError):a.get('/v2/stocks/bars',{})
  self.assertEqual(a.get('/v2/stocks/bars',{}),{});self.assertEqual(len(calls),2);self.assertEqual(a.lock_metrics()['http_429_total'],1);self.free(a)
 def test_metrics_additive_and_both_lane_wait_hold_exact(self):
  a=self.api();t=Sim(0);a.timing_clock=t.clock
  class Lock:
   def acquire(self):t.at+=2
   def release(self):pass
  a.lock=Lock();a.scan_lock=Lock()
  for op in ['scan_rest','native','incremental','other']:
   with a.operation(op),a.serialized_request():t.at+=3
  m=a.lock_metrics();self.assertEqual(set(m['operations']),{'scan_rest','native','incremental','other'})
  for lane,n in [('scan',1),('shared',3)]:
   self.assertEqual(m['lanes'][lane]['count'],n);self.assertEqual(m['lanes'][lane]['wait_total_seconds'],2*n);self.assertEqual(m['lanes'][lane]['hold_total_seconds'],3*n);self.assertEqual(m['lanes'][lane]['wait_max_seconds'],2);self.assertEqual(m['lanes'][lane]['hold_max_seconds'],3)
 def test_HeavyWindow_sleep_stays_outside_both_lanes_and_measurement(self):
  a=self.api();t=Sim(55);g=HeavyWindow(t.wall,sleep=t.sleep,clock=t.clock);a.timing_clock=t.clock;a.before_request=g.before_request;a.request_allowed=g.request_allowed;a.request_operation=lambda:getattr(g.local,'name',None)
  with g.operation('incremental'):
   with a.serialized_request():t.at+=1
  self.assertAlmostEqual(g.metrics()['wait_seconds_by_operation']['incremental'],15)
  self.assertEqual(a.lock_metrics()['operations']['incremental']['wait_total_seconds'],0)
  with g.operation('scan_rest'),a.serialized_request():pass
  self.assertEqual(a.lock_metrics()['lanes']['scan']['count'],1)
 def test_HeavyWindow_sleep_does_not_block_scan_while_incremental_waits(self):
  a=self.api();asleep=threading.Event();wake=threading.Event();blocked=threading.Event();finished=threading.Event()
  def hook():
   if a._operation()=='incremental':asleep.set();wake.wait(2)
  a.before_request=hook
  def inc():
   with a.operation('incremental'),a.serialized_request():finished.set()
  t=threading.Thread(target=inc,daemon=True);t.start();self.assertTrue(asleep.wait(2))
  try:
   with a.operation('scan_rest'),a.serialized_request():self.assertFalse(finished.is_set())
  finally:wake.set();self.join(t)
  self.free(a)
 def test_window_recheck_releases_then_reacquires_same_lane(self):
  a=self.api();checks=iter([False,True]);a.request_allowed=lambda:next(checks)
  with a.operation('incremental'),a.serialized_request():pass
  self.assertEqual(a.requests,0);self.assertEqual(a.lock_metrics()['lanes']['shared']['count'],2);self.free(a)
 def test_pagination_literal_params_and_pages_unchanged_both_modes(self):
  outputs=[]
  for enabled in (False,True):
   queries=[]
   def opener(req,**kw):
    q=urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query);queries.append(q)
    return Response(json.dumps({'bars':{'X':[]},'next_page_token':'literal' if len(queries)==1 else None}).encode())
   a=self.api(opener)
   with patch.object(module,'SCAN_LANE_ENABLED',enabled),a.operation('scan_rest'):data=list(a.bar_pages(['X'],'2026-11-10T14:30:00.123456Z','2026-11-10T14:31:00.654321Z'))
   outputs.append((queries,data));self.free(a)
  self.assertEqual(outputs[0],outputs[1]);self.assertEqual(outputs[1][0][1]['page_token'],['literal']);self.assertEqual(outputs[1][0][0]['feed'],['sip'])
 def test_pagination_cycle_still_fails_closed_without_retry(self):
  a=self.api(lambda *a,**kw:Response(b'{"bars":{},"next_page_token":"same"}'))
  with a.operation('scan_rest'),self.assertRaisesRegex(APIError,'PAGINATION_CYCLE'):list(a.bar_pages(['X'],'a','b'))
  self.assertEqual(a.requests,2);self.free(a)
 def test_snapshot_receipt_timestamps_and_native_rows_unchanged(self):
  results=[]
  for enabled in (False,True):
   a=self.api(lambda *a,**kw:Response(b'{"X":{"latestTrade":{"p":2}}}'));rest=MarketREST(a.get,NS(symbols={'X'}),None);at=datetime(2026,11,10,14,31,2,123456,tzinfo=timezone.utc)
   with patch.object(module,'SCAN_LANE_ENABLED',enabled),a.operation('scan_rest'):results.append(rest.snapshot_envelopes(['X'],lambda:at))
  self.assertEqual(results[0],results[1]);self.assertEqual(results[0]['X']['captured_at'],'2026-11-10T14:31:02.123456+00:00')
 def test_actual_runtime_metrics_exports_lanes_without_runtime_changes(self):
  from test_r8 import RestTimingTests
  # Existing actual nested-metrics regression remains authoritative; additionally inspect new fields.
  a=self.api();stop=NS(value=False);stop.is_set=lambda:stop.value
  async def pause(*a):stop.value=True
  r=NS(stop=stop,wall=lambda:datetime.now(timezone.utc),api=a,ingress=None,a=NS(transport_trusted=False),heavy_window=NS(metrics=lambda:{}));out=io.StringIO()
  env={'r':r,'measure':NS(sample=lambda:{}),'state':{'ready':True,'scan_ok':False},'selection':NS(metrics=lambda:{}),'scan_diagnostics':NS(snapshot=lambda:{}),'json':json,'pause':pause}
  with contextlib.redirect_stdout(out):asyncio.run(nested('metrics',env)())
  m=json.loads(out.getvalue())['rest_lock'];self.assertIn('operations',m);self.assertEqual(set(m['lanes']),{'scan','shared'});self.assertTrue(m['scan_lane_enabled'])
 def test_synthetic_load_measures_contention_without_live_promises(self):
  baseline=load_case(False);candidate=load_case(True)
  for result in (baseline,candidate):
   self.assertEqual(result['scan_requests'],98);self.assertEqual(result['HTTP_calls'],170);self.assertEqual(result['competing_progress'],{'native':24,'incremental':24,'other':24});self.assertEqual(result['active_HTTP_at_end'],0)
  self.assertEqual(baseline['peak_HTTP'],1);self.assertEqual(candidate['peak_HTTP'],2);self.assertGreater(baseline['scan_wait_seconds'],.10)
  self.assertLess(candidate['scan_wait_seconds'],baseline['scan_wait_seconds'])
  print('\nR104_LOAD_SIMULATION_JSON='+json.dumps({'baseline':baseline,'candidate':candidate},sort_keys=True),flush=True)

