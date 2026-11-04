import asyncio,contextlib,io,json,threading,time,unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock
from lua_redis import LuaRedis
from test_direct import OP
from r4a.lease_wait import wait_for_lease,acquire_without_budget,LEADER_KEY
from r4a.direct_parent import DirectProcessGuard
from r4a.redis_ports import leadership
from r4a.durable import PREFIX,SessionBudget
from restart_budget import SessionPolicy
from leadership import NotLeader
from ipr.wave import HeavyWindow,WaveDiagnostics
from dintegration.measure import Measurements
from r4a.ingress_observed import ObservedIngress
from ws_ingress import BoundedWSIngress,IngressBacklogError

class SimTime:
 def __init__(self):self.at=0.;self.callback=lambda:None
 def clock(self):return self.at
 def sleep(self,s):self.at=round(self.at+s,6);self.callback()

class LeaseTests(unittest.TestCase):
 def test_crash_replacement_six_seconds_later_no_claim_until_free_35(self):
  r=LuaRedis(now=OP.timestamp());old=leadership(r,'old');old.acquire();r.now+=6
  t=SimTime();start=r.now;out=[]
  t.callback=lambda:setattr(r,'now',start+t.at)
  self.assertTrue(wait_for_lease(r,lambda:None,clock=t.clock,sleep=t.sleep,emit=out.append))
  self.assertGreaterEqual(t.at,59) # remaining TTL24 + confirmed free35
  self.assertIsNone(r.hget(PREFIX+':budget:unused','boots'))
  self.assertFalse(any(x['budget_claimed'] for x in out));self.assertEqual(out[-1]['observed_free_seconds'],35)
 def test_overlap_old_runs_sixty_no_budget_is_burned(self):
  r=LuaRedis(now=OP.timestamp());old=leadership(r,'old');old.acquire();t=SimTime();start=r.now;released=[False]
  def step():
   r.now=start+t.at
   if t.at<60 and int(t.at)%10==0:old.renew()
   if t.at>=60 and not released[0]:old.release();released[0]=True
  t.callback=step
  with contextlib.redirect_stdout(io.StringIO()):wait_for_lease(r,lambda:None,clock=t.clock,sleep=t.sleep)
  self.assertEqual(t.at,95);new=leadership(r,'new');acquire_without_budget(new,r,lambda:None)
  p=SessionPolicy(OP.date().isoformat(),OP.date().isoformat()+'T00:00:00Z',OP.date().isoformat()+'T23:59:59Z')
  self.assertEqual(SessionBudget(r).claim(p,'new'),('INITIALIZED_BY_FIRST_CLAIM',1))
 def test_free_timer_resets_if_other_worker_claims(self):
  t=SimTime();r=NS(get=lambda k:'occupied' if 10<=t.at<20 else None)
  wait_for_lease(r,lambda:None,clock=t.clock,sleep=t.sleep,emit=lambda x:None)
  self.assertEqual(t.at,55)
 def test_redis_failure_is_not_free(self):
  with self.assertRaises(ConnectionError):wait_for_lease(NS(get=Mock(side_effect=ConnectionError())),lambda:None,emit=lambda x:None)
 def test_stop_during_wait_does_not_claim(self):
  t=SimTime();r=NS(get=lambda k:'old')
  self.assertFalse(wait_for_lease(r,lambda:None,stopped=lambda:t.at>=2,clock=t.clock,sleep=t.sleep,emit=lambda x:None))
 def test_read_acquire_race_rewaits_without_budget(self):
  t=SimTime();r=NS(get=lambda k:None);lead=NS(acquire=Mock(side_effect=[NotLeader('race'),None]))
  acquire_without_budget(lead,r,lambda:None,clock=t.clock,sleep=t.sleep,emit=lambda x:None)
  self.assertEqual(t.at,35);self.assertEqual(lead.acquire.call_count,2)
 def test_real_loss_remains_fatal_not_reacquired(self):
  r=LuaRedis();a=leadership(r,'a');a.acquire();r.now+=31;b=leadership(r,'b');b.acquire()
  with self.assertRaises(NotLeader):a.require_current()
  self.assertEqual(b.backend.current_owner(),'b')
 def guard(self,claim=True,fault=False,boots=0):
  r=LuaRedis(now=OP.timestamp());t=SimTime();start=r.now;t.callback=lambda:setattr(r,'now',start+t.at)
  day=OP.date().isoformat();p=SessionPolicy(day,day+'T00:00:00Z',day+'T23:59:59Z');events=[]
  for i in range(boots):SessionBudget(r).claim(p,'old'+str(i))
  def spawn(boot,n):
   self.assertIsNone(n);events.append((t.at,r.hget(PREFIX+':budget:'+p.key,'boots')))
   if claim:
    lead=leadership(r,'child');acquire_without_budget(lead,r,lambda:None)
    self.assertEqual(SessionBudget(r).claim(p,boot)[1],boots+1)
   return NS(poll=lambda:2 if fault else 0)
  g=DirectProcessGuard(r,p,lambda:None,spawn,lambda:{'limit_bytes':512*1024**2,'current_bytes':1,'anon_bytes':1},clock=t.clock,wall=lambda:r.now,sleep=t.sleep,boot_id='fixture')
  return g,r,t,events,p
 def test_direct_parent_claims_only_inside_child_after_wait(self):
  g,r,t,e,p=self.guard()
  with contextlib.redirect_stdout(io.StringIO()):q=g.run()
  self.assertEqual(e,[(35.,None)]);self.assertEqual(q['boot'],1);self.assertEqual(q['action'],'WORKER_EXIT');self.assertEqual(q['child_exit_code'],0)
  self.assertEqual(r.hget(PREFIX+':budget:'+p.key,'boots'),'1')
 def test_real_preclaim_fault_still_consumes_one(self):
  g,r,t,e,p=self.guard(claim=False,fault=True)
  with contextlib.redirect_stdout(io.StringIO()):q=g.run()
  self.assertEqual(q['boot'],1);self.assertEqual(q['child_exit_code'],2);self.assertEqual(r.hget(PREFIX+':budget:'+p.key,'boots'),'1')
 def test_real_OS_spawn_failure_is_still_counted(self):
  g,r,t,e,p=self.guard(claim=False,fault=True);g.spawn=Mock(side_effect=OSError('fixture'))
  with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(OSError):g.run()
  self.assertEqual(r.hget(PREFIX+':budget:'+p.key,'boots'),'1')
 def test_four_real_failures_still_park_no_fifth(self):
  g,r,t,e,p=self.guard(boots=4)
  self.assertEqual(g.run()['reason'],'BUDGET_EXHAUSTED');self.assertFalse(e);self.assertEqual(t.at,0)
 def test_compose_direct_atomic_lease_precedes_original_budget(self):
  # Actual compose code, injected original components; stop at bridge construction.
  from r4a.runtime import compose
  from dreview.core import Root
  r=LuaRedis(now=OP.timestamp());day=OP.date().isoformat();p=SessionPolicy(day,day+'T00:00:00Z',day+'T23:59:59Z');events=[]
  from r4a.redis_ports import leadership as make
  def factory(raw,w):
   lead=make(raw,w);old=lead.acquire
   lead.acquire=lambda:(events.append('acquire'),old())[-1];return lead
  original=SessionBudget.claim
  def claim(*args):events.append('claim');return original(*args)
  with patch('r4a.redis_ports.leadership',side_effect=factory),patch.object(SessionBudget,'claim',claim),patch('dintegration.bridge.OriginalBridge',side_effect=RuntimeError('STOP_AFTER_CLAIM')):
   with self.assertRaisesRegex(RuntimeError,'STOP_AFTER_CLAIM'):
    compose(redis=r,root=Root(day,frozenset({'X'}),'a'*64,True),worker='child',boot_id='b',policy=p,session_policy={},rest=None,recovery_factory=None,clock_check=lambda:True,root_check=lambda:True,filter_check=None,measure=None,socket_open=None,authorize=lambda:None,sample=lambda:{'limit_bytes':512*1024**2,'current_bytes':1,'anon_bytes':1},wall=lambda:OP,lease_before_budget=True)
  self.assertEqual(events,['acquire','claim'])

class WindowTests(unittest.TestCase):
 def gate(self,second):
  t=SimTime();t.at=second
  g=HeavyWindow(lambda:OP+timedelta(seconds=t.at),sleep=t.sleep)
  return t,g
 def test_exact_window_boundaries(self):
  for second,blocked in [(0,True),(9.999,True),(10,False),(54.999,False),(55,True),(59.999,True)]:
   _,g=self.gate(second);self.assertEqual(g.blocked(),blocked)
 def test_incremental_starts_after_wave(self):
  for kind in ('incremental',):
   for second in (0,5,55,59):
    t,g=self.gate(second)
    with g.operation(kind):
     self.assertFalse(g.blocked());self.assertTrue(g.snapshot()[kind]);self.assertGreaterEqual(t.at,10 if second<10 else 70)
    self.assertFalse(g.snapshot()[kind])
 def test_each_pagination_request_gated_after_waiting_api_lock(self):
  t,g=self.gate(54.);calls=[]
  with g.operation('incremental'):
   g.before_request();calls.append(t.at);t.at=55;g.before_request();calls.append(t.at)
  self.assertEqual(calls[0],54);self.assertGreaterEqual(calls[1],70)
 def test_ordinary_requests_not_globally_delayed(self):
  t,g=self.gate(0);g.before_request();self.assertEqual(t.at,0)
 def test_shutdown_unblocks_wait(self):
  t,g=self.gate(0);g.stop=lambda:t.at>=1
  with self.assertRaisesRegex(RuntimeError,'HEAVY_WORK_STOPPED'):
   with g.operation('incremental'):self.fail('must not enter')
 def test_api_gate_wait_is_outside_serialization_lock_then_rechecked(self):
  from ipr.api import Alpaca
  events=[]
  class Response:
   status=200
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def read(self,*a):events.append('read');return b'{}'
  api=Alpaca({'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'},opener=lambda *a,**k:(events.append('open'),Response())[-1])
  def before():
   self.assertFalse(api.lock.locked());events.append('gate')
  api.before_request=before
  api.request_allowed=Mock(side_effect=[False,True])
  api.get('/v2/stocks/snapshots',{});self.assertEqual(events,['gate','gate','open','read']);self.assertFalse(api.lock.locked())
 def test_runtime_incremental_uses_guard_in_real_loop(self):
  # The inherited scheduler executes our scope on its off-loop worker.
  from r4b.runtime import EvidenceRuntime
  from test_r4 import R4Tests
  async def go():
   f=R4Tests().deferred();r=EvidenceRuntime(f.a,f.a.checkpoint,f.a.bridge.leadership,None,lambda:None,NS(time_allows=lambda t:True),wall=lambda:f.now[0],sample=lambda:{})
   entries=[]
   class Gate:
    @contextlib.contextmanager
    def operation(self,name):entries.append(name);yield
   r.heavy_window=Gate();r.auth=True
   f.a.proof.observe({'T':'b','S':'Y',**f.history['Y'][-1]},f.now[0]);r.wake.set()
   task=asyncio.create_task(r.recovery_loop());await asyncio.sleep(.1);task.cancel();await asyncio.gather(task,return_exceptions=True)
   self.assertIn('incremental',entries);self.assertTrue(f.a.transport_trusted)
  asyncio.run(go())

class DiagnosticTests(unittest.TestCase):
 def fixture(self,logger=lambda *a,**kw:None):
  a=NS(c=NS(bar={'X','Y'}),recovering={'Y'},bridge=NS(p=NS(contention_probe=None,_decision_lock=threading.RLock())))
  m=Measurements();g=HeavyWindow(lambda:OP+timedelta(seconds=20));d=WaveDiagnostics(a,m,g)
  p=NS(_decision_lock=threading.RLock(),_entry_opportunities={},_active_by_symbol={},contention_probe=None,on_bar=lambda *a:None)
  return ObservedIngress(p,NS(on_stream_message=lambda *a:None),lambda:True,diagnostics=d,logger=logger),d,m
 def test_exact_nearest_rank_counts_and_duration(self):
  ingress,d,m=self.fixture();d.received('BAR',OP);d.received('BAR',OP)
  for n in range(1,21):d.finished(OP,n/1000,True)
  q=d.snapshot(ingress);row=next(iter(q['receipt_minutes'].values()))
  self.assertEqual(row['received_BAR'],2);self.assertEqual(row['processed_BAR'],20);self.assertEqual(row['accepted_symbols_at_first_receipt'],1)
  self.assertEqual(row['BAR_processing_seconds']['p95_nearest_rank'],.019);self.assertAlmostEqual(row['BAR_processing_seconds']['mean'],.0105)
 def test_bounds_evict_only_old_diagnostics_no_trust_changes(self):
  ingress,d,m=self.fixture()
  for n in range(9):d.received('BAR',OP+timedelta(minutes=n))
  self.assertEqual(len(d.minutes),6);self.assertEqual(d.admitted(),1)
 def test_sample_overflow_explicit_no_fake_percentile(self):
  ingress,d,m=self.fixture();d.SAMPLES=2
  for i in range(3):d.finished(OP,.1,True)
  r=next(iter(d.snapshot(ingress)['receipt_minutes'].values()));self.assertEqual(r['timing_overflow'],1);self.assertIsNone(r['BAR_processing_seconds']['p95_nearest_rank'])
 def test_original_queue_too_old_still_fails_at_five_not_extended(self):
  async def go():
   out=[];ingress,d,m=self.fixture(logger=lambda row,**kw:out.append(row));await ingress.begin()
   item=('BAR',{'S':'X'},OP,time.monotonic()-5.01)
   with self.assertRaisesRegex(IngressBacklogError,'INGRESS_QUEUE_TOO_OLD'):ingress._work(item)
   ingress._fail(IngressBacklogError('INGRESS_QUEUE_TOO_OLD'))
   self.assertEqual(out[-1]['stage'],'WS_INGRESS_FAIL_CLOSED');self.assertIn('wave',out[-1]);self.assertEqual(out[-1]['ws_ingress']['max_age_seconds'],5.)
   self.assertFalse(ingress.accepting);await ingress.end()
  asyncio.run(go())
 def test_backlog_original_watchdog_and_failure_snapshot_before_revoke(self):
  async def go():
   out=[];ingress,d,m=self.fixture(logger=lambda row,**kw:out.append(row));await ingress.begin()
   ingress.on_gap=lambda:d.adapter.recovering.update({'X','Y'})
   ingress.inflight=('BAR',{'S':'X'},OP,time.monotonic()-5.1)
   await asyncio.sleep(.3)
   self.assertTrue(ingress.failure.done());self.assertEqual(out[-1]['reason'],'INGRESS_BACKLOG_TIMEOUT');self.assertEqual(out[-1]['wave']['accepted_symbols_now'],1)
   ingress.inflight=None;await ingress.end()
  asyncio.run(go())
 def test_original_submit_process_and_metrics(self):
  async def go():
   ingress,d,m=self.fixture();await ingress.begin();ingress.submit('BAR',{'S':'X'},OP)
   await asyncio.wait_for(ingress.q.join(),1)
   s=ingress.snapshot();self.assertEqual(s['received']['BAR'],1);self.assertEqual(s['processed']['BAR'],1)
   row=next(iter(s['wave']['receipt_minutes'].values()));self.assertEqual(row['processed_BAR'],1);await ingress.end()
  asyncio.run(go())
 def test_inflight_and_loop_current_not_just_last_tick(self):
  ingress,d,m=self.fixture();ingress.inflight=('BAR',{},OP,time.monotonic()-3);ingress._element_started=time.monotonic()-2;m.loop_tick=time.monotonic()-4
  q=d.snapshot(ingress);self.assertGreater(q['oldest_age_seconds'],2.9);self.assertGreater(q['inflight_BAR_elapsed_seconds'],1.9);self.assertGreater(q['loop_lag_max_observed_seconds'],3.8)
 def test_stats_thread_safe(self):
  m=Measurements();threads=[threading.Thread(target=lambda:[m.observe('x',.1) for _ in range(500)]) for _ in range(4)]
  for t in threads:t.start()
  for t in threads:t.join()
  self.assertEqual(m.sample()['spans']['x']['count'],2000)

class LockMetricsTests(unittest.TestCase):
 def test_original_reentrant_serialization_and_named_hold_preserved(self):
  from r4a.decision_metrics import MeasuredDecisionLock
  from contention_probe import ObservedRLock,Probe
  raw=ObservedRLock(Probe('fixture'));lock=MeasuredDecisionLock(raw)
  with lock.operation('incremental_admission'),lock:
   self.assertTrue(raw._is_owned())
   with lock.operation('on_bar'),lock:self.assertTrue(raw._is_owned())
  q=lock.snapshot();self.assertEqual(q['incremental_admission']['count'],1);self.assertEqual(q['on_bar']['count'],1);self.assertFalse(raw._is_owned())
 def test_other_thread_cannot_enter_original_lock(self):
  from r4a.decision_metrics import MeasuredDecisionLock
  lock=MeasuredDecisionLock(threading.RLock());results=[]
  with lock:
   t=threading.Thread(target=lambda:results.append(lock.acquire(False)));t.start();t.join()
  self.assertEqual(results,[False])

class DirectOverlapIntegration(unittest.TestCase):
 def exercise(self,old_alive):
  r=LuaRedis(now=OP.timestamp());old=leadership(r,'old');old.acquire();t=SimTime();base=r.now;done=[False];counts=[]
  day=OP.date().isoformat();p=SessionPolicy(day,day+'T00:00:00Z',day+'T23:59:59Z')
  def sleep(seconds):
   t.at=round(t.at+seconds,6);r.now=base+t.at
   counts.append(r.hget(PREFIX+':budget:'+p.key,'boots'))
   if old_alive:
    if t.at<60 and int(t.at)%10==0:old.renew()
    if t.at>=60 and not done[0]:old.release();done[0]=True
  starts=[]
  def spawn(boot,number):
   starts.append(t.at);self.assertIsNone(number);self.assertIsNone(r.get(LEADER_KEY))
   lead=leadership(r,'new');acquire_without_budget(lead,r,lambda:None)
   SessionBudget(r).claim(p,boot)
   return NS(poll=lambda:2)
  g=DirectProcessGuard(r,p,lambda:None,spawn,lambda:{'limit_bytes':512*1024**2,'current_bytes':1,'anon_bytes':1},clock=t.clock,wall=lambda:r.now,sleep=sleep,boot_id='new')
  if not old_alive:r.now+=6;base=r.now
  with contextlib.redirect_stdout(io.StringIO()):result=g.run()
  self.assertEqual(starts,[95. if old_alive else 59.]);self.assertTrue(all(c is None for c in counts[:int(starts[0])]))
  self.assertEqual(result['boot'],1);self.assertEqual(r.hget(PREFIX+':budget:'+p.key,'boots'),'1')
 def test_parent_deploy_with_sixty_second_old_instance(self):self.exercise(True)
 def test_parent_replacement_six_seconds_after_crash(self):self.exercise(False)
