import asyncio,contextlib,copy,gc,hashlib,io,json,threading,time,tracemalloc,unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
from test_direct import OP,bar
import test_r3
from dreview.core import Root,Controller,Needs
from r4a.durable import RedisState,PREFIX,canonical,sha
from r4a.redis_ports import leadership
from dintegration.persistence import StateConflict
from dintegration.stream_proof import StreamProof
from r4b.store import ProofStore,JournalState
from lua_redis import LuaRedis
from ipr.ranking import Selection
from ipr.observability import Diagnostics,RedisTelemetry,observed_phase,OBS,PauseObserver

class RecoveryPerformance(unittest.TestCase):
 def test_500_symbol_recover_loop_pause_and_1500_attest_memory(self):
  async def run():
   f=test_r3.setup(tuple('S%03d'%i for i in range(500)));f.now[0]=OP+timedelta(seconds=61)
   for symbol in sorted(f.history):
    test_r3.receive(f,symbol,0)
    f.history[symbol]=[bar(OP-timedelta(minutes=i)) for i in reversed(range(60))]
   original=f.src.warmup
   def realistic(*args):
    one,five=original(*args)
    # 60 native 1m and 61 native 5m per symbol, JSON parse included, no live API.
    five={s:[dict(bar(OP-timedelta(minutes=5*(i+1))),_timeframe='native_5Min') for i in reversed(range(61))] for s in one}
    raw=json.dumps([one,five]);time.sleep(.03)
    return tuple(json.loads(raw))
   f.src.warmup=realistic
   real_preload=f.a.bridge.preload;real_attest=f.a.c.attest;real_append=f.a.checkpoint.append_epoch
   def unlocked():
    self.assertFalse(f.a.control_lock._is_owned());self.assertFalse(f.a.bridge.p._decision_lock._is_owned())
   def preload(v):unlocked();return real_preload(v)
   def attest(*a,**kw):unlocked();return real_attest(*a,**kw)
   def append(v):unlocked();time.sleep(.02);return real_append(v)
   f.a.bridge.preload=preload;f.a.c.attest=attest;f.a.checkpoint.append_epoch=append
   delays=[];stop=False
   async def ticker():
    while not stop:
     at=time.monotonic();await asyncio.sleep(.005);delays.append(max(0,time.monotonic()-at-.005))
   tick=asyncio.create_task(ticker());await asyncio.sleep(.01)
   start=time.monotonic()
   try:self.assertTrue(await asyncio.to_thread(f.a.recover,f.now[0]))
   finally:stop=True;await tick
   elapsed=time.monotonic()-start
   self.assertEqual(len(f.a.bridge.p.br.history),500);self.assertEqual(len(f.a.c.proofs),500)
   self.assertTrue(all(len(h)==60 for h in f.a.bridge.p.br.history.values()))
   self.assertLess(max(delays),.5)
   c=f.a.c;c.attest=real_attest
   tracemalloc.start();gc.collect();base=tracemalloc.get_traced_memory()[0];memory=[]
   with patch.object(c,'epoch',side_effect=AssertionError('ATTEST_MUST_NOT_COPY_EPOCH')):
    for n in range(1500):
     c.attest('S%03d'%(n%500),history=True,continuity=True,reconciled=True)
     if n in (499,999,1499):gc.collect();memory.append(tracemalloc.get_traced_memory()[0]-base)
   tracemalloc.stop()
   self.assertEqual(len(c.journal),128);self.assertLess(max(memory),1024*1024)
   self.assertLess(memory[-1]-memory[0],256*1024)
   result={'test':'R7_RECOVER_500_AND_ATTEST_1500','symbols':500,'native1_rows_per_symbol':60,'native5_rows_per_symbol':61,'recover_seconds':elapsed,'heartbeat_samples':len(delays),'max_loop_pause_seconds':max(delays),'pause_limit_seconds':.5,'journal_entries':len(c.journal),'tracemalloc_delta_after_500_1000_1500':memory,'scope':'Synthetic real adapter/StreamProof/PackedHistory/Controller; no network or 0.5CPU cgroup claim'}
   print(json.dumps(result))
  asyncio.run(run())
 def test_correction_during_staged_preload_discards_private_history(self):
  f=test_r3.R3Incremental().deferred() if hasattr(test_r3,'R3Incremental') else None
  before={s:list(v) for s,v in f.a.bridge.p.br.history.items()};old=f.a.bridge.preload
  def preload(v):old(v);f.r.received({'T':'c','S':'Y'},f.now[0])
  f.a.bridge.preload=preload
  self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]))
  self.assertEqual({s:list(v) for s,v in f.a.bridge.p.br.history.items()},before)
  self.assertFalse(f.a.transport_trusted);self.assertIn('Y',f.a.recovering)
 def test_clock_late_failure_no_published_history(self):
  from dintegration.clock import ClockUnavailable
  f=test_r3.R3Incremental().deferred();before={s:list(v) for s,v in f.a.bridge.p.br.history.items()};old=f.a.bridge.preload
  def preload(v):old(v);f.a.checks=Mock(side_effect=ClockUnavailable('fixture'))
  f.a.bridge.preload=preload
  self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]));self.assertTrue(f.a.transport_trusted)
  self.assertEqual({s:list(v) for s,v in f.a.bridge.p.br.history.items()},before)

class LegacyArchive(unittest.TestCase):
 def test_old_full_epoch_chain_and_archive_read_unchanged(self):
  r=LuaRedis();lead=leadership(r,'w');lead.acquire();root=Root('2026-10-28',frozenset({'X'}),'a'*64,True)
  state=RedisState(r,root.session,root.identity(),'w',1);state.restore()
  proof=StreamProof();proof.connected('old',OP);proof.set_scope({'X'});proof.observe({'T':'b','S':'X',**bar(OP)},OP+timedelta(seconds=61))
  old=Controller(root);old.reconnect();old.set_desired({'X'},Needs(1));old.plan(0);old.ack(old.connection,['X'],[],.1);old.attest('X',history=True,continuity=True,reconciled=True)
  old_controller=old.epoch() # Complete legacy D_REVIEW_EPOCH_V1 payload, unchanged schema.
  # Literal legacy format: full controller epoch + full proof. No migration/writeback.
  state.append_epoch({'reason':'ACK_CONFIRMED','controller':old_controller})
  state.append_epoch({'kind':'B_STREAM_WITNESS','case_schema':2,'proof':proof.snapshot(),'add_R':['X'],'add_U':['X']})
  state.append_epoch({'kind':'B_CASE_RESOLUTION','case_schema':2,'proof':proof.snapshot(),'resolved_R':['X'],'resolved_U':['X']})
  before=copy.deepcopy(r.state)
  restarted=RedisState(r,root.session,root.identity(),'w',1);out=restarted.restore()
  self.assertEqual(out['checkpoint']['epoch']['kind'],'B_CASE_RESOLUTION');self.assertFalse(out['transport_trusted'])
  restored=StreamProof();store=ProofStore(JournalState(restarted),restored);self.assertTrue(store.load_archive())
  self.assertEqual(store.pending_R,set());self.assertEqual(store.pending_U,set());self.assertFalse(restored.valid)
  self.assertEqual(restored.archived,proof.snapshot());self.assertEqual(r.state,before)
 def test_old_chain_corruption_is_still_rejected(self):
  r=LuaRedis();lead=leadership(r,'w');lead.acquire();s=RedisState(r,'2026-10-28','a'*64,'w',1);s.restore();s.append_epoch({'legacy':True})
  k=s.keys[2];r.state['hashes'][k]['1']=r.state['hashes'][k]['1'].replace('true','false')
  with self.assertRaises(StateConflict):s.restore()

class SubscriptionIsolation(unittest.TestCase):
 def test_desired_persistence_failure_closes_previously_healthy_transport(self):
  f=test_r3.trusted();f.r.state.append_epoch=Mock(side_effect=ConnectionError('fixture'))
  with self.assertRaises(ConnectionError):f.r.set_desired(f.r.c.bar,Needs(2,O=frozenset({'X'})))
  self.assertFalse(f.a.transport_trusted)
 def test_added_TRADE_requires_fresh_first_trade(self):
  f=test_r3.trusted();f.a.c.proofs['X']['first_trade']=True
  f.r.set_desired(f.r.c.bar,Needs(2,O=frozenset({'X'})))
  self.assertFalse(f.a.c.proofs['X']['first_trade']);self.assertTrue(f.a.transport_trusted)
 def test_trade_need_while_ACK_pending_replans_without_global_recovery(self):
  f=test_r3.trusted();r=f.r;c=r.c;count=len(f.calls)
  r.planner.propose(Needs(2,O=frozenset({'X'})))
  self.assertEqual(r.control.plan()['trades'],['X'])
  result=r.planner.propose(Needs(3,O=frozenset({'X','Z'})))
  self.assertEqual(result['status'],'WAIT_PENDING_ACK_THEN_REPLAN');self.assertTrue(f.a.transport_trusted)
  self.assertTrue(r.control.ack(c.connection,sorted(c.bar),['X']))
  r.planner.after_ack();self.assertTrue(f.a.transport_trusted)
  self.assertEqual(r.control.plan()['trades'],['Z'])
  self.assertTrue(r.control.ack(c.connection,sorted(c.bar),['X','Z']))
  self.assertTrue(f.a.transport_trusted);self.assertEqual(len(f.calls),count)
 def test_Bar_delta_prunes_only_departed_diagnostic_maps(self):
  f=test_r3.trusted();f.a.quarantine['removed']='OLD';f.a.report['good']['removed']={};f.a.rejection_until['removed']=99
  f.r.set_desired(f.r.c.bar,Needs(2))
  self.assertNotIn('removed',f.a.quarantine);self.assertNotIn('removed',f.a.report['good']);self.assertNotIn('removed',f.a.rejection_until)
 def test_trade_only_keeps_trust_and_no_bar_warmup(self):
  async def run():
   f=test_r3.trusted();count=len(f.calls);r=f.r;before=copy.deepcopy(f.a.c.proofs)
   for rev,trades in [(2,{'X'}),(3,set())]:
    r.set_desired(r.c.bar,Needs(rev,O=frozenset(trades)))
    self.assertTrue(f.a.transport_trusted)
    plan=r.control.plan();self.assertEqual(plan['bars'],[])
    self.assertTrue(r.control.ack(r.c.connection,sorted(r.c.bar),sorted(trades)))
    task=asyncio.create_task(r.recovery_loop());r.wake.set();await asyncio.sleep(.03);task.cancel();await asyncio.gather(task,return_exceptions=True)
    self.assertTrue(f.a.transport_trusted);self.assertEqual(len(f.calls),count);self.assertEqual(r.attempts,0)
    self.assertEqual(f.a.c.proofs,before)
  asyncio.run(run())
 def test_BAR_delta_admits_only_new_symbol_preserves_existing_history(self):
  f=test_r3.trusted();r=f.r;c=r.c;c.root=Root(c.root.session,c.root.symbols|{'W'},'a'*64,True);f.history['W']=[bar(OP)]
  before=copy.deepcopy(list(f.a.bridge.p.br.history['X']));oldcalls=len(f.calls)
  r.set_desired({'X','Z','W'},Needs(2));self.assertTrue(f.a.transport_trusted)
  p=r.control.plan();self.assertEqual(p['bars'],['Y']);self.assertEqual(p['action'],'unsubscribe')
  self.assertTrue(r.control.ack(c.connection,['X','Z'],[]))
  p=r.control.plan();self.assertEqual(p['bars'],['W']);self.assertEqual(p['action'],'subscribe')
  self.assertTrue(r.control.ack(c.connection,['W','X','Z'],[]))
  f.a.proof.observe({'T':'b','S':'W',**bar(OP)},f.now[0])
  self.assertTrue(f.a.admit_incremental(['W'],f.now[0]));self.assertTrue(f.a.transport_trusted)
  self.assertEqual(len(f.calls),oldcalls+1);self.assertEqual(f.calls[-1]['symbols'],['W']);self.assertEqual(list(f.a.bridge.p.br.history['X']),before)
 def test_real_U_still_closes_trust(self):
  f=test_r3.trusted();f.r.set_desired(f.r.c.bar,Needs(2,U=frozenset({'X'})));self.assertFalse(f.a.transport_trusted)
 def test_20_post_trust_BARs_no_recovery(self):
  # Reuse the entire accepted R3 ACK->trust->20 BAR->incremental->correction scenario.
  test_r3.R3Lifecycle().test_ACK_witness_trust_20_BARs_incremental_then_correction()

class Turnover(unittest.TestCase):
 def test_133_replacements_100_then_33_after_sixty_invalid_immediate(self):
  s=Selection();old={'L%03d'%i for i in range(500)};s.current=old.copy()
  scores={x:{'score':10} for x in old};new={'N%03d'%i for i in range(133)};scores.update({x:{'score':20} for x in new})
  a=set(s.update(scores,set(),0));self.assertEqual(len(a&new),100);self.assertEqual(s.used,100)
  a=set(s.update(scores,set(),59.999));self.assertEqual(len(a&new),100)
  a=set(s.update(scores,set(),60));self.assertEqual(len(a&new),133);self.assertEqual(len(a),500)
  invalid=next(iter(a&old));scores.pop(invalid);a=set(s.update(scores,set(),61));self.assertNotIn(invalid,a)
 def test_ACK_or_recovery_blocks_optional_but_not_invalid_removal(self):
  s=Selection();s.current={'A','BAD'};s.last=None
  out=s.update({'A':{'score':1},'B':{'score':99}},set(),1,blocked=True)
  self.assertEqual(out,['A']);self.assertEqual(s.used,0)
 def test_pin_never_removed_for_missing_score_and_five_point_tie(self):
  s=Selection();s.current={'A'};pins={'P%03d'%i for i in range(499)}
  a=s.update({'A':{'score':10},'B':{'score':15},'C':{'score':15}},pins,0)
  self.assertIn('B',a);self.assertNotIn('C',a);self.assertTrue(pins<=set(a))
 def test_turnover_constants_single_source(self):
  from ipr.turnover import OPTIONAL_REPLACEMENTS,OPTIONAL_WINDOW_SECONDS
  self.assertEqual((OPTIONAL_REPLACEMENTS,OPTIONAL_WINDOW_SECONDS),(100,60.))

class AtomicLeaseAndTelemetry(unittest.TestCase):
 def test_ordinary_BAR_path_uses_two_MGET_not_four_GET(self):
  f=test_r3.trusted();d=Diagnostics();raw=f.a.bridge.r.r;client=RedisTelemetry(raw,d)
  f.a.bridge.r.r=client;f.a.bridge.leadership.backend.r.r=client
  f.a.bridge.p.signal_gate.schedule={'session':'2026-10-28','open_utc':OP.isoformat(),'close_utc':(OP+timedelta(minutes=390)).isoformat()}
  now=OP+timedelta(seconds=121)
  self.assertTrue(f.a.on_message({'T':'b','S':'X',**bar(OP+timedelta(minutes=1))},now))
  counts={}
  for row in d.snapshot()['REDIS_RTT']:counts[row['command']]=counts.get(row['command'],0)+row['count']
  self.assertEqual(counts,{'MGET':2})
 def test_one_MGET_no_cache_per_verification(self):
  r=LuaRedis();lead=leadership(r,'w');lead.acquire();orig=r.mget;r.mget=Mock(wraps=orig)
  lead.require_current();lead.require_current();self.assertEqual(r.mget.call_count,2)
 def test_fence_rejects_loss_between_MGET_and_journal_write(self):
  r=LuaRedis();lead=leadership(r,'w');lead.acquire();s=RedisState(r,'2026-10-28','a'*64,'w',1);s.restore()
  lead.require_current();lead.release();leadership(r,'other').acquire()
  with self.assertRaisesRegex(StateConflict,'FENCE_LOST'):s.append_epoch({'must':'not commit'})
  self.assertEqual(s.seq,0);self.assertIsNone(r.get(s.keys[0]))
 def test_metrics_no_keys_values_and_lock_wait_not_RTT(self):
  d=Diagnostics();r=RedisTelemetry(NS(get=lambda k:time.sleep(.005)),d)
  with d.phase('recover.fetch'):r.get('NEVER_LOG_THIS_KEY')
  out=d.snapshot();self.assertNotIn('NEVER_LOG_THIS_KEY',json.dumps(out));self.assertEqual(out['REDIS_RTT'][0]['command'],'GET');self.assertEqual(out['REDIS_RTT'][0]['count'],1)
  self.assertEqual(out['REDIS_RTT'][0]['operation'],'recover.fetch');self.assertGreaterEqual(out['REDIS_RTT'][0]['mean_seconds'],.005)
 def test_clock_attempt_start_end_heartbeat_failure_and_success(self):
  from dintegration.clock import ClockProof
  rows=[];c=ClockProof(lambda:(int(time.time()),int(time.time()%1*1e6)),emit=rows.append)
  c.refresh();self.assertEqual([x['stage'] for x in rows],['CLOCK_REFRESH_BEGIN','CLOCK_REFRESH_END']);self.assertFalse(c.heartbeat_snapshot()['in_progress'])
  c.server_time=Mock(side_effect=ConnectionError('DO_NOT_PRINT_VALUE'))
  with self.assertRaises(ConnectionError):c.refresh()
  self.assertEqual(rows[-1]['stage'],'CLOCK_REFRESH_END');self.assertFalse(rows[-1]['success']);self.assertNotIn('DO_NOT_PRINT_VALUE',json.dumps(rows))
 def test_pause_correlated_phases_not_cause_and_budgeted_probe(self):
  d=Diagnostics();d.loop_heartbeat=time.monotonic()-1;probe=NS(request=Mock());rows=[];w=PauseObserver(probe,rows.append,d);w.start()
  try:time.sleep(.25)
  finally:w.close()
  self.assertEqual(len(rows),1);self.assertEqual(rows[0]['cause'],'UNPROVEN');self.assertEqual(probe.request.call_count,1)

class HistoryCleanup(unittest.TestCase):
 def test_scope_prunes_stale_invalid_without_dropping_live_invalid(self):
  p=StreamProof();p.connected('test',OP);p.set_scope({'X','Y'});p.invalidate('X','CORRECTION_OR_CANCEL');p.invalidate('Y','INVALID_BAR')
  p.set_scope({'Y'});self.assertEqual(p.invalid,{'Y':'INVALID_BAR'});self.assertTrue(p.valid)
 def test_only_unused_history_removed_all_cases_and_inflight_retained(self):
  f=test_r3.trusted();b=f.a.bridge;p=b.p
  for name in ['unused','A','O','U','R','pending']:
   p.br.accumulate_only(name,bar(OP))
  p._active_by_symbol['A']={};p._entry_opportunities['O']={};p._entry_uncertain.add('U');f.a.barrier.persisted_R.add('R')
  f.a.c.inflight={'after_bar':{'pending'}}
  before=copy.deepcopy(b.r.r.state)
  removed=b.release_unused_history(f.a.c,f.a.recovering,f.a.barrier.persisted_R)
  self.assertEqual(removed,{'unused'});self.assertEqual(b.r.r.state,before)
  self.assertTrue({'A','O','U','R','pending'}<=p.br.history.keys())

class UnchangedSafety(unittest.TestCase):
 def test_frozen_and_R6_safety_files_byte_identical(self):
  from pathlib import Path
  root=Path(__file__).resolve().parents[1]
  expected=json.loads((root/'BASELINE_R6_IDENTITIES.json').read_text())
  self.assertEqual(len([p for p in expected if p.startswith('reference_app/')]),72)
  for path,h in expected.items():self.assertEqual(hashlib.sha256((root/path).read_bytes()).hexdigest(),h,path)
 def test_incremental_limits_and_global_deadline_unchanged(self):
  from r4b.runtime import EvidenceRuntime
  from r4b.adapter import EvidenceAdapter
  from restart_budget import UNTRUSTED_AFTER_ACK_SEC
  from r4a.lease_wait import QUIET_SECONDS
  self.assertEqual((EvidenceRuntime.INCREMENTAL_SPACING,EvidenceRuntime.INCREMENTAL_BATCH,EvidenceAdapter.INCREMENTAL_BATCH_CAP,EvidenceAdapter.INCREMENTAL_REJECT_COOLDOWN),(1.,100,100,30.))
  self.assertEqual(UNTRUSTED_AFTER_ACK_SEC,120);self.assertEqual(QUIET_SECONDS,35.)
