"""R3 regression: stay trusted after initial recovery; admit deferred symbols only."""
import asyncio,copy,contextlib,io,json,threading,unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock
from test_direct import OP,bar
from test_recovery_regression import fixture
from dreview.core import Root,Controller,Needs
from r4b.adapter import EvidenceAdapter
from r4b.runtime import EvidenceRuntime
from dintegration.measure import Measurements

def setup(symbols=('X','Y','Z')):
 raw,lead,redis,src,bridge,case=fixture(set(symbols));p=case.proof
 p.connected(started=OP+timedelta(seconds=35));p.set_scope(set(symbols))
 c=Controller(Root('2026-10-28',frozenset(symbols),'a'*64,True));c.reconnect();c.set_desired(set(symbols),Needs(1));c.plan(0.)
 assert c.ack(c.connection,list(symbols),[],.1)
 now=[OP+timedelta(seconds=35)];mono=[0.];epochs=[];history={s:[] for s in symbols};calls=[]
 a=EvidenceAdapter(c,bridge,src,NS(),lambda:True,lambda *args:True,lambda:True,NS(append_epoch=epochs.append),Measurements(),proof=p,barrier=case,wall=lambda:now[0]);a.authenticated=True
 def warm(ss,at,minutes):
  calls.append({'symbols':sorted(ss),'trusted':a.transport_trusted,'minutes':minutes})
  return ({s:copy.deepcopy([b for b in history[s] if __import__('dintegration.stream_proof',fromlist=['instant']).instant(b['t'])+timedelta(minutes=1)<=at]) for s in ss},
          {s:[dict(bar(OP-timedelta(minutes=5)),_timeframe='native_5Min')] for s in ss})
 src.warmup=warm
 r=EvidenceRuntime(a,a.checkpoint,lead,None,lambda:None,NS(time_allows=lambda t:True),clock=lambda:mono[0],wall=lambda:now[0],sample=lambda:{})
 r.auth=True
 return NS(a=a,r=r,src=src,now=now,mono=mono,epochs=epochs,history=history,calls=calls)

def receive(f,s,minute):
 b=bar(OP+timedelta(minutes=minute));f.history[s].append(copy.deepcopy(b))
 f.r.received(dict(b,T='b',S=s),f.now[0])

def trusted():
 f=setup();f.now[0]=OP+timedelta(seconds=61);receive(f,'X',0);receive(f,'Z',0)
 assert f.a.recover(f.now[0]);f.r.wake.clear();return f

async def until(predicate):
 for _ in range(300):
  if predicate():return
  await asyncio.sleep(.005)
 raise AssertionError('worker did not reach expected state')

class R3Lifecycle(unittest.TestCase):
 def test_ACK_witness_trust_20_BARs_incremental_then_correction(self):
  async def go():
   f=setup();r=f.r;a=f.a;r.wake.set();task=asyncio.create_task(r.recovery_loop())
   try:
    await asyncio.sleep(.02);self.assertEqual(r.attempts,0);self.assertFalse(a.transport_trusted)
    f.now[0]=OP+timedelta(seconds=61);f.mono[0]=26;receive(f,'X',0);receive(f,'Z',0)
    await until(lambda:a.transport_trusted);self.assertEqual(r.attempts,1);self.assertEqual(a.recovering,{'Y'})
    previous=copy.deepcopy(a.c.proofs);first_floor=a.trusted_since;close=Mock(wraps=a.close_eligibility);a.close_eligibility=close
    for minute in range(1,11):
     f.now[0]=OP+timedelta(minutes=minute+1,seconds=1);f.mono[0]=26+60*minute
     receive(f,'X',minute);receive(f,'Z',minute)
     await asyncio.sleep(.002);self.assertTrue(a.transport_trusted)
    # Also status/ACK-like wake with no deferred witness must not trigger recovery.
    r.wake.set();await asyncio.sleep(.02)
    self.assertEqual(r.attempts,1);self.assertEqual(len(f.calls),1);close.assert_not_called()
    receive(f,'Y',10);await until(lambda:'Y' not in a.recovering)
    self.assertTrue(a.transport_trusted);self.assertEqual(r.attempts,1);self.assertEqual(r.incremental_attempts,1)
    self.assertEqual(f.calls[-1]['symbols'],['Y']);self.assertTrue(f.calls[-1]['trusted']);self.assertEqual(len(f.calls),2)
    self.assertEqual(a.trusted_since,first_floor)
    for s in ('X','Z'):self.assertEqual(a.c.proofs[s],previous[s])
    self.assertTrue(any(x.get('kind')=='B_INCREMENTAL_ADMISSION' for x in f.epochs));close.assert_not_called()
    a.bridge.p._entry_opportunities['Y']={'session':'2026-10-28','entry_trigger_ts':(f.now[0]-timedelta(seconds=1)).isoformat()}
    self.assertFalse(a._admissible_trade('Y',{},f.now[0]))
    a.bridge.p._entry_opportunities.clear()
    r.received({'T':'c','S':'X'},f.now[0]);self.assertFalse(a.transport_trusted);self.assertIn('X',a.barrier.persisted_R)
    print(json.dumps({'test':'R3_COMPLETE_LIFECYCLE','normal_BARs_after_trust':20,'full_recoveries':r.attempts,'incremental_batches':r.incremental_attempts,'warmup_symbol_sets':[v['symbols'] for v in f.calls],'trusted_during_incremental_fetch':f.calls[-1]['trusted'],'correction_revoked':not a.transport_trusted}))
   finally:
    r.stop.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
  asyncio.run(go())

class R3Incremental(unittest.TestCase):
 def deferred(self):
  f=trusted();f.now[0]+=timedelta(seconds=1);receive(f,'Y',0);f.r.wake.clear();return f
 def test_exact_OHLCV_mismatch_rejected_without_global_revoke(self):
  f=self.deferred();f.history['Y'][0]['v']=999
  with contextlib.redirect_stdout(io.StringIO()) as out:self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]))
  q=json.loads(out.getvalue());self.assertEqual(q['rejection_reasons'],{'REST_STREAM_BAR_VALUE_MISMATCH':1,'REJECTION_COOLDOWN':0});self.assertEqual((q['accepted'],q['rejected']),(0,1));self.assertGreaterEqual(q['fetch_seconds'],0)
  self.assertTrue(f.a.transport_trusted);self.assertIn('Y',f.a.recovering)
 def test_missing_REST_BAR_set_rejected(self):
  f=self.deferred();f.now[0]=OP+timedelta(minutes=3,seconds=1);f.history['Y'].append(bar(OP+timedelta(minutes=1)))
  self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]));self.assertTrue(f.a.transport_trusted);self.assertIn('Y',f.a.recovering)
 def test_no_witness_not_admitted(self):
  f=trusted();self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]));self.assertTrue(f.a.transport_trusted)
 def test_native5_incomplete_rejected(self):
  f=self.deferred();old=f.src.warmup
  def bad(*args):
   one,five=old(*args);five['Y']=[dict(bar(OP),_timeframe='native_5Min')];return one,five
  f.src.warmup=bad;self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]));self.assertTrue(f.a.transport_trusted)
 def test_source_outage_preserves_other_trust_and_deferred_symbol(self):
  f=self.deferred();f.src.warmup=Mock(side_effect=ConnectionError('fixture'))
  self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]));self.assertTrue(f.a.transport_trusted);self.assertIn('Y',f.a.recovering)
 def test_durable_write_before_symbol_attestation(self):
  f=self.deferred();original=f.a.checkpoint.append_epoch
  def append(v):
   if v.get('kind')=='B_INCREMENTAL_ADMISSION':self.assertFalse(f.a.c.proofs['Y']['continuity']);self.assertIn('Y',f.a.recovering)
   original(v)
  f.a.checkpoint.append_epoch=append;self.assertTrue(f.a.admit_incremental(['Y'],f.now[0]));self.assertTrue(f.a.c.proofs['Y']['continuity'])
 def test_journal_failure_fails_closed_without_admission(self):
  f=self.deferred();f.a.checkpoint.append_epoch=Mock(side_effect=ConnectionError('fixture'))
  with self.assertRaises(ConnectionError):f.a.admit_incremental(['Y'],f.now[0])
  self.assertFalse(f.a.transport_trusted);self.assertIn('Y',f.a.recovering);self.assertFalse(f.a.c.proofs['Y']['continuity'])
 def test_leader_or_clock_failure_remains_fatal(self):
  f=self.deferred();f.a.checks=Mock(side_effect=RuntimeError('fixture_leader'))
  with self.assertRaises(RuntimeError):f.a.admit_incremental(['Y'],f.now[0])
  self.assertFalse(f.a.transport_trusted);self.assertIn('Y',f.a.recovering)
 def test_open_case_needs_original_full_path(self):
  f=self.deferred();f.a.barrier.persisted_R.add('Y');self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]));self.assertTrue(f.a.transport_trusted);self.assertIn('Y',f.a.recovering)
 def test_batch_cap_enforced(self):
  f=self.deferred()
  with self.assertRaisesRegex(ValueError,'INCREMENTAL_BATCH_CAP'):f.a.admit_incremental([str(i) for i in range(101)],f.now[0])
  self.assertEqual(len(f.calls),1)
 def test_concurrent_epoch_change_or_revocation_never_admits(self):
  for mode in ('epoch','correction','disconnect'):
   with self.subTest(mode=mode):
    f=self.deferred();old=f.src.warmup
    def change(*args):
     result=old(*args)
     if mode=='epoch':f.a.c.revision+=1
     elif mode=='correction':f.r.received({'T':'c','S':'Y'},f.now[0])
     else:f.r.revoke('DISCONNECTED')
     return result
    f.src.warmup=change;self.assertFalse(f.a.admit_incremental(['Y'],f.now[0]));self.assertIn('Y',f.a.recovering);self.assertFalse(f.a.c.proofs['Y']['continuity'])
    if mode!='epoch':self.assertFalse(f.a.transport_trusted)
 def test_real_invalidations_still_revoke(self):
  for kind in ('error','c','x','u','invalid','gap'):
   with self.subTest(kind=kind):
    f=trusted()
    if kind=='gap':f.r.revoke('INGRESS_FAIL_CLOSED')
    elif kind=='invalid':
     with self.assertRaisesRegex(Exception,'INVALID_OR_CONFLICTING_BAR'):f.r.received(dict(bar(OP),T='b',S='X',v=-1),f.now[0])
    else:f.r.received({'T':kind,'S':'X'},f.now[0])
    self.assertFalse(f.a.transport_trusted)
 def test_spacing_coalesces_repeated_deferred_BARs(self):
  async def go():
   f=self.deferred();f.r.last_incremental=f.mono[0];task=asyncio.create_task(f.r.recovery_loop());f.r.wake.set()
   try:
    await asyncio.sleep(.02);self.assertEqual(f.r.incremental_attempts,0);self.assertEqual(len(f.calls),1);self.assertTrue(f.a.transport_trusted)
   finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
  asyncio.run(go())
 def test_exact_30_second_boundary_allows_admission(self):
  async def go():
   f=self.deferred();f.r.last_incremental=0.;f.mono[0]=30.;f.r.wake.set();task=asyncio.create_task(f.r.recovery_loop())
   try:
    await until(lambda:'Y' not in f.a.recovering);self.assertTrue(f.a.transport_trusted);self.assertEqual(f.r.incremental_attempts,1);self.assertEqual(f.r.attempts,0)
   finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
  asyncio.run(go())
 def test_correction_while_fetch_thread_blocked_cannot_revive_trust(self):
  async def go():
   f=self.deferred();entered=threading.Event();release=threading.Event();old=f.src.warmup
   def blocked(*args):
    entered.set()
    if not release.wait(3):raise AssertionError('fixture release timeout')
    return old(*args)
   f.src.warmup=blocked;task=asyncio.create_task(asyncio.to_thread(f.a.admit_incremental,['Y'],f.now[0]))
   try:
    await until(entered.is_set);self.assertTrue(f.a.transport_trusted)
    f.r.received({'T':'c','S':'Y'},f.now[0]);self.assertFalse(f.a.transport_trusted);release.set()
    self.assertFalse(await task);self.assertFalse(f.a.transport_trusted);self.assertIn('Y',f.a.recovering);self.assertFalse(f.a.c.proofs['Y']['continuity'])
   finally:release.set();await asyncio.gather(task,return_exceptions=True)
  asyncio.run(go())
 def test_scheduler_batch_100_rotates_to_other_symbols(self):
  async def go():
   f=setup(tuple('S%03d'%i for i in range(135)));f.now[0]=OP+timedelta(seconds=61)
   receive(f,'S000',0);self.assertTrue(f.a.recover(f.now[0]));f.r.wake.clear()
   for s in f.a.recovering:receive(f,s,0)
   batches=[]
   def attempt(ss,now):
    batches.append(list(ss));f.mono[0]+=31
    if len(batches)==2:f.r.stop.set()
   f.a.admit_incremental=attempt;f.r.wake.set();await f.r.recovery_loop()
   self.assertEqual(len(batches[0]),100);self.assertLessEqual(len(batches[1]),100);self.assertIn('S134',batches[1]);self.assertEqual(f.r.attempts,0)
  asyncio.run(go())
