"""R10.8 independent flags, literal outbox contract, timing fault isolation."""
import ast,asyncio,contextlib,io,json,threading,time,unittest,urllib.error
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch
from redis_outbox_store import RedisOutboxStore
from ipr.outbox_mget import MGETOutboxStore,outbox_store
from ipr import review_flags
from dintegration.native_timing import NativeTiming,native_guard,native_phase,observe_boundaries
from ipr.api import APIError
from r108_support import Box,box,item,packed,AT,POLICY,native_fixture

class Outbox(unittest.TestCase):
 def parity(self,b):
  import copy
  a=copy.deepcopy(b);c=copy.deepcopy(b);old=RedisOutboxStore(a).pending();new=MGETOutboxStore(c).pending();self.assertEqual(packed(old),packed(new));return a,c,new
 def test_empty(self):a,b,x=self.parity(box(0));self.assertEqual(x,[]);self.assertEqual(b.calls,[('SCAN',0,'operational_priority_radar:v1:outbox:*',100)])
 def test_one(self):self.parity(box(1))
 def test_27_GET_become_one_MGET(self):
  a,b,x=self.parity(box(27));self.assertEqual(sum(v[0]=='GET' for v in a.calls),27);self.assertEqual(sum(v[0]=='MGET' for v in b.calls),1);self.assertFalse(any(v[0]=='GET' for v in b.calls))
 def test_150_in_two_chunks(self):
  a,b,x=self.parity(box(150));self.assertEqual([len(v[1]) for v in b.calls if v[0]=='MGET'],[100,50])
 def test_mixed_SENT_PENDING(self):a,b,x=self.parity(box(27,True));self.assertEqual(len(x),14)
 def test_missing_empty_and_false_JSON_handling(self):
  b=box(2);ks=sorted(b.rows);b.rows[ks[0]]='';b.pages=[ks+['operational_priority_radar:v1:outbox:missing']];self.parity(b)
 def test_corrupt_JSON_same_exception_text_and_position(self):
  for raw in ['{bad','null','[1]','3']:
   errors=[]
   for cls in (RedisOutboxStore,MGETOutboxStore):
    b=box(3);b.rows[sorted(b.rows)[1]]=raw
    try:cls(b).pending()
    except Exception as e:errors.append((type(e),str(e)))
   self.assertEqual(len(errors),2);self.assertEqual(errors[0],errors[1])
 def test_deleted_after_SCAN_before_read(self):
  for cls in (RedisOutboxStore,MGETOutboxStore):
   b=box(27);deleted=sorted(b.rows)[7]
   b.after_scan=lambda b,n:b.rows.pop(deleted,None)
   result=cls(b).pending();self.assertEqual(len(result),26);self.assertNotIn('E007',[r['event_id'] for r in result])
 def test_pagination_cursor_pattern_and_custom_count_preserved(self):
  b=box(150);ks=sorted(b.rows);b.pages=[ks[:40],[],ks[40:]]
  import copy
  a=copy.deepcopy(b);c=copy.deepcopy(b);self.assertEqual(packed(RedisOutboxStore(a).pending(7)),packed(MGETOutboxStore(c).pending(7)));self.assertEqual([x for x in a.calls if x[0]=='SCAN'],[x for x in c.calls if x[0]=='SCAN'])
 def test_scan_duplicates_not_deduplicated(self):
  b=box(2);ks=sorted(b.rows);b.pages=[[ks[0]],[ks[0],ks[1]]];a,c,x=self.parity(b);self.assertEqual(len(x),3)
 def test_exact_sort_and_ties_stable(self):
  a,b,x=self.parity(box(27));self.assertEqual(x,sorted(x,key=lambda v:(v.get('updated_at',''),v.get('event_id',''))))
 def test_original_mark_attempt_method_literal_inheritance(self):
  self.assertIs(MGETOutboxStore.mark_attempt,RedisOutboxStore.mark_attempt)
  import copy
  b=box(2);a=copy.deepcopy(b);c=copy.deepcopy(b)
  for cls,db in ((RedisOutboxStore,a),(MGETOutboxStore,c)):
   k='operational_priority_radar:v1:outbox:E000';db.rows[k]=json.dumps(item(0));self.assertTrue(cls(db).mark_attempt('E000',True));self.assertFalse(cls(db).mark_attempt('missing',True))
  self.assertEqual(a.rows,c.rows);self.assertEqual(a.calls,c.calls)
 def test_transport_error_identity_propagates(self):
  for cls in (RedisOutboxStore,MGETOutboxStore):
   b=box(3);error=ConnectionError('fixture');b.error=error
   with self.assertRaises(ConnectionError) as cm:cls(b).pending()
   self.assertIs(cm.exception,error)
 def test_SCAN_error_not_swallowed(self):
  for cls in (RedisOutboxStore,MGETOutboxStore):
   b=box(3);err=RuntimeError('scan fixture');b.scan=lambda **kw:(_ for _ in ()).throw(err)
   with self.assertRaises(RuntimeError) as cm:cls(b).pending()
   self.assertIs(cm.exception,err)
 def test_large_reply_no_truncation(self):
  b=box(3)
  for k in b.rows:b.rows[k]=json.dumps({**item(1),'large':'x'*400000})
  self.parity(b)
 def test_new_record_written_before_next_scan_page_seen_both(self):
  for cls in (RedisOutboxStore,MGETOutboxStore):
   b=box(2);ks=sorted(b.rows);new='operational_priority_radar:v1:outbox:new';b.pages=[[ks[0]],[ks[1],new]]
   def write(b,n):
    if n==0:
     t=threading.Thread(target=lambda:b.rows.__setitem__(new,json.dumps(item(3))));t.start();t.join()
   b.after_scan=write;self.assertEqual(len(cls(b).pending()),3)
 def test_new_key_after_final_SCAN_is_seen_next_poll(self):
  for cls in (RedisOutboxStore,MGETOutboxStore):
   b=box(1);new='operational_priority_radar:v1:outbox:new';b.after_scan=lambda b,n:b.rows.__setitem__(new,json.dumps(item(9)))
   self.assertEqual(len(cls(b).pending()),1);self.assertEqual(len(cls(b).pending()),2)
 def test_no_TTL_or_cache_or_record_skipping(self):
  b=box(27);s=MGETOutboxStore(b);s.pending();s.pending();self.assertEqual(sum(x[0]=='MGET' for x in b.calls),2);self.assertFalse(any(x[0] in ('SET','EXPIRE','DEL') for x in b.calls))
 def test_arbitrary_mid_GET_deletion_is_NOT_atomic_snapshot_equivalence(self):
  sequential=box(2);keys=sorted(sequential.rows)
  def deletion(b,key):
   if key==keys[1]:b.rows.pop(key,None)
  sequential.before_read=deletion;left=RedisOutboxStore(sequential).pending();right=MGETOutboxStore(box(2)).pending();self.assertEqual(len(left),1);self.assertEqual(len(right),2)

class NativeMeasurement(unittest.TestCase):
 def setup_monitor(self,n=201,wall=lambda:AT,clock=time.monotonic):
  s,h,a,r,p,store,b=native_fixture(n);lines=[];m=NativeTiming(p,r,POLICY,wall=wall,clock=clock,emit=lines.append);m.install(b);self.addCleanup(m.close);return s,h,a,r,p,store,b,m,lines
 def test_same_inputs_requests_and_decisions_all_four_flag_combinations(self):
  result=[]
  for timing in (False,True):
   for outbox in (False,True):
    with patch.object(review_flags,'NATIVE_TIMING_ENABLED',timing),patch.object(review_flags,'OUTBOX_MGET_ENABLED',outbox):
     s,h,a,r,p,store,b=native_fixture();lines=[];m=None
     if timing:m=NativeTiming(p,r,POLICY,wall=lambda:AT,emit=lines.append);m.install(b)
     clock=p.utc_now
     with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):b.native_cycle(s,AT,True)
     if m:m.q.join();m.close();self.assertEqual(m.last['request_count'],3)
     self.assertIs(p.utc_now,clock);self.assertEqual(h.peak,1);self.assertEqual(len(h.calls),3)
     result.append((packed(store),h.calls,packed(outbox_store(box(27)).pending())))
  self.assertTrue(all(x==result[0] for x in result))
 def test_cycle_line_and_FIRST_E_and_body_bytes_exact(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor()
  with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):b.native_cycle(s,AT,True)
  m.q.join();rows=[json.loads(x) for x in lines];cycle=next(x for x in rows if x['stage']=='R108_NATIVE_CYCLE');first=next(x for x in rows if x['stage']=='R106_FIRST_E_TIMING')
  self.assertEqual(cycle['completion'],'COMPLETED');self.assertEqual(cycle['pending_symbols'],201);self.assertEqual(cycle['request_count'],3);self.assertEqual(cycle['response_bytes'],sum(len(r.data) for r in h.responses));self.assertEqual(cycle['rest_acquisitions'],3);self.assertEqual(first['bar_close_to_post_FIRST_E_seconds'],1.)
  self.assertEqual(cycle['sip_first_receipt_lag_seconds'],1.);self.assertEqual(cycle['sip_target_matches'],201)
 def test_no_FIRST_E_still_cycle_line(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor();p.ec.evaluate_with_telemetry=lambda *v:({},None)
  with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):b.native_cycle(s,AT,True)
  m.q.join();self.assertEqual([json.loads(x)['stage'] for x in lines],['R108_NATIVE_CYCLE'])
 def test_HTTP_error_same_identity_and_cycle_failure_type(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor();error=APIError('fixture');h.error=error
  with a.operation('native'):
   with self.assertRaises(APIError) as cm:b.native_cycle(s,AT,True)
  m.q.join();self.assertIs(cm.exception,error);self.assertEqual(m.last['completion'],'FAILED');self.assertEqual(m.last['failure_type'],'APIError')
 def test_429_no_retry_added(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor();h.error=urllib.error.HTTPError('fixture',429,'fixture',{},None)
  with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaises(APIError):b.native_cycle(s,AT,True)
  self.assertEqual(len(h.calls),1);self.assertEqual(a.lock_metrics()['http_429_total'],1)
 def test_no_redis_command_from_observer(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor();calls=[]
  p.r.mget=lambda keys:(calls.append(list(keys)) or [None for _ in keys])
  with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):b.native_cycle(s,AT,True)
  self.assertEqual(len(calls),1)
 def test_R103_request_observer_composed_once(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor();seen=[]
  with a.operation('native'),a.observe_requests(lambda *x:seen.append(x)),contextlib.redirect_stdout(io.StringIO()):b.native_cycle(s,AT,True)
  self.assertEqual(len(seen),3)
 def test_observer_failures_preserve_original_application_error(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor();err=ValueError('original');p.ec.evaluate_with_telemetry=lambda *v:(_ for _ in ()).throw(err);m.clock=lambda:(_ for _ in ()).throw(SystemExit('observer'))
  with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaises(ValueError) as cm:b.native_cycle(s,AT,True)
  self.assertIs(cm.exception,err)
 def test_absent_latest_SIP_bar_not_invented(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor();h.gap=True;p.ec.evaluate_with_telemetry=lambda *v:({},None)
  with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):b.native_cycle(s,AT,True)
  self.assertIsNone(m.last['sip_first_seen_utc']);self.assertEqual(m.last['sip_target_matches'],0)
 def test_same_original_read_size_and_timeout(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor()
  with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):b.native_cycle(s,AT,True)
  self.assertTrue(all(t==20 for q,t in h.calls));self.assertTrue(all(resp.read_calls==[32*1024*1024+1] for resp in h.responses))
 def test_disabled_uses_original_native_without_observation(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor()
  with patch.object(review_flags,'NATIVE_TIMING_ENABLED',False),a.operation('native'),contextlib.redirect_stdout(io.StringIO()):b.native_cycle(s,AT,True)
  self.assertEqual(m.cycles,0);self.assertEqual(len(h.calls),3)
 def test_non_native_recovery_fetch_untouched(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor();r.bars_multi(s,AT-timedelta(minutes=305),AT,'5Min')
  self.assertEqual(m.cycles,0);self.assertEqual(m.q.qsize(),0);self.assertEqual(h.peak,1)
 def test_no_duplicate_E_from_observation(self):
  s,h,a,r,p,store,b,m,lines=self.setup_monitor();out=io.StringIO()
  with a.operation('native'),contextlib.redirect_stdout(out):b.native_cycle(s,AT,True);b.native_cycle(s,AT,True)
  self.assertEqual(len(store),1);self.assertEqual(out.getvalue().count("'stage': 'SHADOW_FIRST_E'"),1)

class Boundaries(unittest.TestCase):
 def monitor(self,at):
  s,h,a,r,p,store,b=native_fixture(1);t=NS(at=at,mono=0.);lines=[];m=NativeTiming(p,r,POLICY,wall=lambda:t.at,clock=lambda:t.mono,emit=lines.append);self.addCleanup(m.close);return m,t,lines
 def advance(self,m,t,at):
  while t.at<at:t.at+=timedelta(seconds=1);t.mono+=1;m.boundary_tick(t.at)
 def boundary(self):return AT.replace(second=0)
 def result(self,m,lines):m.q.join();return next(json.loads(s) for s in lines if json.loads(s).get('bar_end')==self.boundary().isoformat())
 def test_cycle_start_exact_15_seconds_included(self):
  end=self.boundary();m,t,lines=self.monitor(end-timedelta(seconds=1));self.advance(m,t,end+timedelta(seconds=15))
  with m.cycle(t.at,['X']):pass
  self.advance(m,t,end+timedelta(seconds=16));self.assertEqual(self.result(m,lines)['outcome'],'STARTED_WITHIN_15S')
 def test_previous_cycle_spans_window_even_with_sampler_gap(self):
  end=self.boundary();m,t,lines=self.monitor(end-timedelta(seconds=2))
  with m.cycle(t.at,['X']):t.at=end+timedelta(seconds=16);t.mono=18;m.boundary_tick(t.at)
  row=self.result(m,lines);self.assertEqual(row['outcome'],'NO_START_OBSERVED');self.assertIn('PREVIOUS_CYCLE_RUNNING',row['observed_blockers'])
 def test_actual_ready_scan_trusted_guard_failures_counted(self):
  end=self.boundary();m,t,lines=self.monitor(end-timedelta(seconds=1));self.advance(m,t,end)
  for name in ['ready','scan_ok','trusted']:m.guard(name,False,t.at)
  self.advance(m,t,end+timedelta(seconds=16));row=self.result(m,lines);self.assertEqual(row['outcome'],'NO_START_OBSERVED');self.assertTrue({'READY_FALSE','SCAN_OK_FALSE','TRUSTED_FALSE'}<=set(row['observed_blockers']));self.assertEqual(m.reason_counts['SCAN_OK_FALSE'],1)
 def test_pause_only_first_five_seconds(self):
  end=self.boundary();m,t,lines=self.monitor(end-timedelta(seconds=1));m.phase('PAUSE');self.advance(m,t,end+timedelta(seconds=16));row=self.result(m,lines);self.assertIn('PAUSE_5S',row['observed_blockers']);self.assertIn('UNKNOWN_SCHEDULER_DELAY',row['observed_blockers'])
 def test_unmeasured_reason_stays_UNKNOWN(self):
  end=self.boundary();m,t,lines=self.monitor(end-timedelta(seconds=1));t.at=end+timedelta(seconds=16);m.boundary_tick(t.at);row=self.result(m,lines);self.assertEqual(row['outcome'],'UNKNOWN');self.assertEqual(row['observed_blockers'],[])
 def test_late_attachment_cannot_claim_prior_absence(self):
  end=self.boundary();m,t,lines=self.monitor(end+timedelta(seconds=8));self.advance(m,t,end+timedelta(seconds=16));self.assertEqual(self.result(m,lines)['outcome'],'UNKNOWN')
 def test_78_RTH_boundaries_no_expansion(self):
  m,t,lines=self.monitor(AT);self.assertEqual(len(m.boundaries),78);self.assertEqual(m.boundaries[0]['end'].isoformat(),'2026-11-10T14:35:00+00:00');self.assertEqual(m.boundaries[-1]['end'].isoformat(),'2026-11-10T21:00:00+00:00')
 def test_outside_RTH_at_final_close(self):
  close=AT.replace(hour=21,minute=0,second=0);m,t,lines=self.monitor(close-timedelta(seconds=1));self.advance(m,t,close);m.guard('rth',False,t.at);self.advance(m,t,close+timedelta(seconds=16));m.q.join();row=next(json.loads(x) for x in lines if json.loads(x)['bar_end']==close.isoformat());self.assertIn('OUTSIDE_RTH',row['observed_blockers'])
 def test_daemon_failure_cannot_stop_application(self):
  stop=NS(value=False);stop.is_set=lambda:stop.value
  async def fail(*v):raise ValueError('observation')
  async def pause(*v):stop.value=True
  m=NS(boundary_loop=fail,errors=0);asyncio.run(observe_boundaries(m,NS(stop=stop),pause));self.assertEqual(m.errors,1)
 def test_guard_fault_keeps_original_value(self):
  m=NS(safe=lambda *v:(_ for _ in ()).throw(SystemExit()));r=NS(a=NS(bridge=NS(native_timing=m)))
  self.assertIs(native_guard(r,'ready',False,AT),False);self.assertIs(native_guard(r,'ready',True,AT),True);native_phase(r,'CHECK')

class BoundedObservation(unittest.TestCase):
 def test_daily_line_and_byte_caps(self):
  s,h,a,r,p,store,b=native_fixture(1);lines=[];m=NativeTiming(p,r,POLICY,emit=lines.append);m.MAX_LINES=2;self.addCleanup(m.close)
  for n in range(10):m.enqueue({'stage':'R108_NATIVE_CYCLE','n':n})
  m.q.join();self.assertEqual(len(lines),2);self.assertGreaterEqual(m.dropped,8);self.assertLessEqual(m.bytes,m.MAX_DAY_BYTES)
 def test_slow_writer_does_not_block_producer(self):
  s,h,a,r,p,store,b=native_fixture(1);entered=threading.Event();release=threading.Event()
  def slow(line):entered.set();release.wait(2)
  m=NativeTiming(p,r,POLICY,emit=slow);m.enqueue({'n':0});self.assertTrue(entered.wait(1));done=threading.Event()
  def produce():
   for n in range(100):m.enqueue({'n':n})
   done.set()
  t=threading.Thread(target=produce);t.start()
  try:self.assertTrue(done.wait(1));self.assertLessEqual(m.q.qsize(),32);self.assertGreater(m.dropped,0)
  finally:release.set();t.join();m.q.join();m.close()
 def test_oversized_log_dropped(self):
  s,h,a,r,p,store,b=native_fixture(1);lines=[];m=NativeTiming(p,r,POLICY,emit=lines.append);m.enqueue({'x':'x'*5000});m.q.join();m.close();self.assertEqual(lines,[]);self.assertEqual(m.dropped,1)

class SenderEquivalence(unittest.TestCase):
 def run_sender(self,timing,grouped,malformed=False):
  from ipr.sender import Sender
  from r103_support import pipeline,trade,feed
  from test_direct import OP
  from state_store import base_record,key_trade,key_outbox
  p=pipeline('X',OP,10)
  with contextlib.redirect_stdout(io.StringIO()):feed(p,trade('X',OP,10),True)
  record=p._active_by_symbol['X'];tid=record['trade_id'];eid='ENTRY:'+tid.removeprefix('TRADE:')
  event=base_record('outbox',record['session'],'X','PENDING',OP.isoformat());event.update(event_id=eid,event_type='ENTRY',payload={'trade_id':tid},attempt_count=0,leader_generation=1)
  db=Box({key_trade(tid):json.dumps(record),key_outbox(eid):'{bad' if malformed else json.dumps(event)});actions=[];messages=[];token=NS(worker_instance_id='fixture',leader_generation=1)
  leader=NS(require_current=lambda:(actions.append('leader') or token))
  runtime=NS(check_session=lambda:actions.append('session'),leader=leader,c=NS(proofs={'X':{'history':True,'continuity':True,'reconciled':True}},bar={'X'}),a=NS(bridge=NS(r=db),checks=lambda:actions.append('checks'),transport_trusted=True,recovering=set(),trusted_since=OP-timedelta(seconds=1)))
  ledger=NS(start=lambda *v:(actions.append(('start',v[0],v[1])) or ('identity','ALLOW')),finish=lambda *v:actions.append(('finish',v[0],v[-1])))
  out=io.StringIO();error=None
  with patch.object(review_flags,'NATIVE_TIMING_ENABLED',timing),patch.object(review_flags,'OUTBOX_MGET_ENABLED',grouped),patch('ipr.sender.Ledger',return_value=ledger),contextlib.redirect_stdout(out):
   sender=Sender(runtime,db,{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture'},transport=NS(post=lambda text:(messages.append(text) or 'SENT')))
   for _ in range(2):
    try:sender.step()
    except Exception as e:error=(type(e),str(e));break
  return actions,messages,sender.seen,out.getvalue(),error,type(sender.store),db.calls
 def test_Sender_four_combinations_same_delivery_seen_ledger(self):
  cases=[self.run_sender(t,o) for t in (False,True) for o in (False,True)];self.assertTrue(all(c[:5]==cases[0][:5] for c in cases));self.assertEqual(len(cases[0][1]),1);self.assertEqual(cases[0][5],RedisOutboxStore);self.assertEqual(cases[1][5],MGETOutboxStore)
 def test_Sender_corrupt_JSON_same_failure(self):
  a=self.run_sender(False,False,True);b=self.run_sender(True,True,True);self.assertEqual(a[:5],b[:5]);self.assertEqual(a[1],[]);self.assertIs(a[4][0],json.JSONDecodeError)
 def test_Sender_only_store_construction_changed(self):
  import hashlib
  p=Path(__file__).resolve().parents[1]/'ipr/sender.py';s=p.read_text().replace("self.store=__import__('ipr.outbox_mget',fromlist=['outbox_store']).outbox_store(runtime.a.bridge.r)",'self.store=RedisOutboxStore(runtime.a.bridge.r)')
  self.assertEqual(hashlib.sha256(s.encode()).hexdigest(),'3019a89bbf5cf44ade1b7d0ab6cc10b06f11fa3b2481a28cdd8bb51026d8b88b')

class FrozenContracts(unittest.TestCase):
 def test_reference_72_and_API_REST_parent_chain_unchanged(self):
  import hashlib
  root=Path(__file__).resolve().parents[1]
  for name in ['r104_reference_sha256.json','r104_unchanged_sha256.json']:
   for rel,h in json.loads((root/'tests/fixtures'/name).read_text()).items():self.assertEqual(hashlib.sha256((root/rel).read_bytes()).hexdigest(),h,rel)
  self.assertFalse((root/'ipr/native_fast.py').exists());self.assertFalse((root/'dintegration/native_clock.py').exists())
 def test_actual_native_schedule_short_circuit_and_pause_unchanged(self):
  from test_r71 import nested
  import itertools
  root=Path(__file__).resolve().parents[1]
  async def run(mode,ready,scan,trusted,market,enabled):
   seen=[];stop=NS(value=False);stop.is_set=lambda:stop.value
   async def to_thread(fn):return fn()
   async def pause(stop,seconds):seen.append(('pause',seconds));stop.value=True
   bridge=NS(native_cycle=lambda s,now,allow:seen.append(('native',s,now.isoformat(),allow)))
   r=NS(stop=stop,wall=lambda:AT,a=NS(control_lock=threading.Lock(),recovering={'Y'},transport_trusted=trusted,bridge=bridge),c=NS(bar=['X','Y'],proofs={'X':{'history':True,'continuity':True,'reconciled':True},'Y':{'history':True,'continuity':True,'reconciled':True}}))
   env={'r':r,'state':{'ready':ready,'scan_ok':scan},'bundle':{'market':market},'dt':lambda x:x,'api':NS(operation=lambda n:contextlib.nullcontext()),'asyncio':NS(to_thread=to_thread),'pause':pause}
   if mode=='R104':exec((root/'tests/fixtures/r104_native_function.py').read_text(),env);fn=env['native']
   else:fn=nested('native',env)
   with patch.object(review_flags,'NATIVE_TIMING_ENABLED',enabled):await fn()
   return seen
  for ready,scan,trusted in itertools.product((False,True),repeat=3):
   for market in (None,{'open':AT-timedelta(hours=1),'close':AT+timedelta(hours=1)},{'open':AT+timedelta(hours=1),'close':AT+timedelta(hours=2)}):
    original=asyncio.run(run('R104',ready,scan,trusted,market,False))
    for flag in (False,True):self.assertEqual(asyncio.run(run('R108',ready,scan,trusted,market,flag)),original)
 def test_empty_pending_count_zero_not_UNKNOWN(self):
  s,h,a,r,p,store,b=native_fixture(1);lines=[];m=NativeTiming(p,r,POLICY,wall=lambda:AT,emit=lines.append);m.install(b)
  try:
   with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):b.native_cycle(s,AT,True);b.native_cycle(s,AT,True)
   self.assertEqual(m.last['pending_symbols'],0);self.assertEqual(m.last['request_count'],0)
  finally:m.close()
 def test_failed_MGET_in_original_poll_preserved(self):
  s,h,a,r,p,store,b=native_fixture();lines=[];m=NativeTiming(p,r,POLICY,wall=lambda:AT,emit=lines.append);m.install(b);err=RuntimeError('fixture read')
  p.r.mget=lambda *v:(_ for _ in ()).throw(err)
  try:
   with self.assertRaises(RuntimeError) as cm:b.native_cycle(s,AT,True)
   self.assertIs(cm.exception,err);self.assertEqual(m.last['completion'],'FAILED');self.assertEqual(m.last['request_count'],0)
  finally:m.close()
 def test_original_handled_persistence_error_remains_handled(self):
  s,h,a,r,p,store,b=native_fixture(1);p.ew.persist_first_e=lambda *v:(_ for _ in ()).throw(ValueError('fixture persist'));lines=[];m=NativeTiming(p,r,POLICY,wall=lambda:AT,emit=lines.append);m.install(b)
  try:
   with a.operation('native'),contextlib.redirect_stdout(io.StringIO()):result=b.native_cycle(s,AT,True)
   self.assertEqual(result['early_core_persist_errors'],1);self.assertEqual(m.last['completion'],'COMPLETED');self.assertEqual(m.last['persistence_error_type'],'ValueError')
  finally:m.close()
 def test_previous_cycle_completed_before_late_sampler_retains_proof(self):
  s,h,a,r,p,store,b=native_fixture(1);end=AT.replace(second=0);t=NS(at=end-timedelta(seconds=2),mono=0);lines=[];m=NativeTiming(p,r,POLICY,wall=lambda:t.at,clock=lambda:t.mono,emit=lines.append)
  try:
   with m.cycle(t.at,['X']):t.at=end+timedelta(seconds=16);t.mono=18
   t.at+=timedelta(seconds=1);m.boundary_tick(t.at);m.q.join();row=next(json.loads(x) for x in lines if json.loads(x)['stage']=='R108_NATIVE_BOUNDARY' and json.loads(x)['bar_end']==end.isoformat());self.assertEqual(row['outcome'],'NO_START_OBSERVED');self.assertIn('PREVIOUS_CYCLE_RUNNING',row['observed_blockers'])
  finally:m.close()
