"""Offline R8 integration and observational-equivalence checks. No real services."""
import ast,asyncio,contextlib,copy,io,json,threading,time,unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from ipr.scan_health import ScanGate,BlockedTrades,ScanExclusions,RTHVolumeStale,SCAN_VALIDITY_SECONDS,blocked_context_valid
from ipr.api import Alpaca
from ipr.ranking import base_score,Selection
from test_r71 import Sim,nested
import test_r71 as prior
from test_direct import OP

def filter_actual(env):
 path=Path(__file__).resolve().parents[1]/'ipr/runtime.py';module=ast.parse(path.read_text())
 build=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='_build')
 node=copy.deepcopy(next(n for n in build.body if isinstance(n,ast.FunctionDef) and n.name=='final_filter'))
 exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(path),'exec'),env)
 return env['final_filter']

class GateTests(unittest.TestCase):
 def fixture(self):
  t=Sim(0);s={'scan_ok':False};logs=[];g=ScanGate(s,t.clock,t.wall,started_at=0,log=logs.append);return t,s,g,logs
 def test_named_constant_and_exact_boundary(self):
  t,s,g,l=self.fixture();self.assertEqual(SCAN_VALIDITY_SECONDS,150);g.success();t.at=150;self.assertTrue(g.expire());t.at=150.000001;self.assertFalse(g.expire());self.assertEqual(l[-1]['reason'],'SCAN_EXPIRED')
 def test_stale_retains_success_but_never_refreshes_its_age(self):
  t,s,g,l=self.fixture();g.success();t.at=140;self.assertTrue(g.failure(RTHVolumeStale('RTH_VOLUME_STALE')));self.assertEqual(g.last_success,0);self.assertEqual(len(l),1);t.at=151;self.assertFalse(g.failure(RTHVolumeStale('RTH_VOLUME_STALE')))
 def test_stale_without_success_stays_closed(self):
  t,s,g,l=self.fixture();self.assertFalse(g.failure(RTHVolumeStale('RTH_VOLUME_STALE')));self.assertEqual(l,[])
 def test_other_error_closes_immediately_including_lookalike(self):
  for e in (RuntimeError('fixture'),ValueError('RTH_VOLUME_STALE')):
   t,s,g,l=self.fixture();g.success();t.at=1;g.failure(e);self.assertFalse(s['scan_ok']);self.assertIsNone(g.last_success)
 def test_trust_loss_and_late_inflight_success_cannot_reopen(self):
  t,s,g,l=self.fixture();g.success();version=g.generation;g.invalidate('TRUST_LOST');self.assertFalse(g.success(version));self.assertFalse(g.success(allowed=False));self.assertFalse(s['scan_ok'])
 def test_accounting_transitions_exact_and_nonduplicated(self):
  t,s,g,l=self.fixture();t.at=10;g.success();t.at=20;g.success();t.at=30;g.invalidate('PREP_FAILED');g.invalidate('GUARD_STOP');t.at=50
  m=g.snapshot();self.assertEqual(m['true_seconds'],20);self.assertEqual(m['false_seconds'],30);self.assertEqual(m['availability_ratio'],.4);self.assertEqual(len(l),2)
 def test_actual_nested_scan_preserves_recent_success_on_stale(self):
  from ipr.scan_health import gate_for
  def seed(state,clock,wall):
   g=gate_for(state,clock,wall);g.success();return g
  with patch('ipr.scan_health.gate_for',seed):
   result=prior.R71Scan().scan_once(durations=(1.,60.,60.))
  self.assertTrue(result[3]['scan_ok']);self.assertTrue(result[5][-1]['previous_success_preserved']);result[4].propose.assert_not_called()
 def test_actual_nested_scan_unexpected_failure_does_not_preserve_success(self):
  from ipr.scan_health import gate_for
  def seed(state,clock,wall):
   g=gate_for(state,clock,wall);g.success();return g
  with patch('ipr.scan_health.gate_for',seed),patch('ipr.wave.HeavyWindow.before_request',side_effect=RuntimeError('fixture')):
   result=prior.R71Scan().scan_once()
  self.assertFalse(result[3]['scan_ok']);self.assertFalse(result[5][-1]['previous_success_preserved'])
 def test_actual_adapter_revocation_closes_gate_even_persistence_fails(self):
  from ipr.runtime import DirectEvidence
  t,s,g,l=self.fixture();g.success();obj=object.__new__(DirectEvidence);obj.scan_gate=g
  from r4b.adapter import EvidenceAdapter
  with patch.object(EvidenceAdapter,'revoke',side_effect=RuntimeError('fake Redis failure')):
   with self.assertRaises(RuntimeError):DirectEvidence.revoke(obj,'fixture')
  self.assertFalse(s['scan_ok'])
 def test_initial_warmup_is_not_a_loss_of_previously_absent_trust(self):
  from ipr.runtime import DirectEvidence
  from r4b.adapter import EvidenceAdapter
  t,s,g,l=self.fixture();g.success();obj=object.__new__(DirectEvidence);obj.scan_gate=g
  obj._LiveAdapter__trusted=False;obj.cancel_event=threading.Event()
  with patch.object(EvidenceAdapter,'close_eligibility',return_value=None):
   DirectEvidence.close_eligibility(obj,'RECOVERY_PENDING');self.assertTrue(s['scan_ok'])
   obj._LiveAdapter__trusted=True
   DirectEvidence.close_eligibility(obj,'fixture');self.assertFalse(s['scan_ok'])

class BlockedTests(unittest.TestCase):
 def fixture(self,n=500):
  t=Sim(0);symbols={'S'+str(i) for i in range(n)};state={'ready':True,'scan_ok':False};lines=[]
  state['_scan_gate']=ScanGate(state,t.clock,t.wall,log=lambda r:None);state['_blocked_trades']=BlockedTrades(symbols,log=lines.append)
  a=NS(transport_trusted=True,recovering=set(),bridge=NS(p=NS(_entry_opportunities={})),missed_opportunities=set(),trusted_since=OP,symbol_admitted_at={})
  c=NS(failed=False,bar=set(symbols),connection=1,proofs={s:{'history':True,'continuity':True,'reconciled':True,'connection':1} for s in symbols})
  runtime=NS(a=a,c=c);state['_runtime']=runtime
  f=filter_actual({'prepared':state,'bundle':{'eligible':symbols},'cls':NS(eligible=lambda s:True),'dt':lambda x:x,'blocked_context_valid':blocked_context_valid})
  return t,state,lines,runtime,f
 def test_500_symbols_thousands_messages_max50_logs_exact_unique(self):
  t,s,lines,r,f=self.fixture()
  for repeat in range(10):
   for symbol in sorted(r.c.bar):self.assertFalse(f(symbol,{'p':2.,'t':OP,'s':1,'c':['I']},OP))
  m=s['_blocked_trades'].snapshot();self.assertEqual(m['total_rejected_trade_messages'],5000);self.assertEqual(m['minutes'][0]['distinct_symbols'],500);self.assertEqual(len(lines),50);self.assertEqual(lines[0]['conditions'],['I']);self.assertEqual(lines[0]['size'],1)
 def test_six_minutes_bounded_and_first_per_symbol_per_minute(self):
  t,s,lines,r,f=self.fixture(1)
  for minute in range(10):
   now=OP+timedelta(minutes=minute)
   for repeat in range(100):f('S0',{'p':2.,'t':now},now)
  m=s['_blocked_trades'].snapshot();self.assertEqual(m['total_rejected_trade_messages'],1000);self.assertEqual(len(m['minutes']),6);self.assertEqual(len(lines),10)
 def test_no_count_for_other_filter_or_trust_rejections(self):
  t,s,lines,r,f=self.fixture(1)
  for msg in ({'p':.99,'t':OP},{'p':50.01,'t':OP},{'p':2,'t':OP-timedelta(seconds=61)}):self.assertFalse(f('S0',msg,OP))
  self.assertEqual(s['_blocked_trades'].total,0)
  for mutate in (lambda:r.c.bar.clear(),lambda:r.a.recovering.add('S0'),lambda:setattr(r.a,'transport_trusted',False)):
   t,s,lines,r,f=self.fixture(1);mutate();self.assertFalse(f('S0',{'p':2,'t':OP},OP));self.assertEqual(s['_blocked_trades'].snapshot()['total_rejected_trade_messages'],0)
 def test_no_count_for_known_missed_opportunity_floor(self):
  t,s,lines,r,f=self.fixture(1);r.a.bridge.p._entry_opportunities['S0']={'session':'fixture','entry_trigger_ts':(OP-timedelta(seconds=1)).isoformat()}
  self.assertFalse(f('S0',{'p':2,'t':OP},OP));self.assertEqual(s['_blocked_trades'].total,0)
 def test_open_gate_keeps_I_B_and_price_boundaries_unchanged(self):
  t,s,lines,r,f=self.fixture(1);s['_scan_gate'].success()
  for price in (1.,50.):
   for flag in ('I','B'):self.assertTrue(f('S0',{'p':price,'t':OP,'s':1,'c':[flag]},OP))
  self.assertEqual(s['_blocked_trades'].total,0)
 def test_telemetry_logger_failure_keeps_false_decision(self):
  t,s,lines,r,f=self.fixture(1);s['_blocked_trades'].log=lambda r:(_ for _ in ()).throw(RuntimeError('fixture'))
  self.assertFalse(f('S0',{'p':2,'t':OP},OP));self.assertEqual(s['_blocked_trades'].total,1)
 def test_blocked_events_observational_reference_truth_unchanged(self):
  t,s,lines,r,f=self.fixture(1)
  for ready in (True,False):
   for gate in (True,False):
    for price in (.99,1.,2.,50.,50.01):
     s['ready']=ready
     if gate:s['_scan_gate'].success()
     else:s['_scan_gate'].invalidate('SCAN_FAILED')
     self.assertEqual(f('S0',{'p':price,'t':OP,'s':1,'c':['B']},OP),ready and gate and 1<=price<=50)

class ExclusionTests(unittest.TestCase):
 def test_scoring_outputs_unchanged_with_diagnostics(self):
  floats=NS(value=lambda s:{'status':'UNKNOWN'});now=OP+timedelta(minutes=20)
  for price in (.99,1.,3.,50.,50.01):
   for age in (0,60,61):
    for history in (None,{'adv20':1000,'high':4.,'low':3.,'close':3.5}):
     for volume in (None,{'volume':100,'open':2.}):
      snap={'latestTrade':{'p':price,'t':(now-timedelta(seconds=age)).isoformat()}};d=ScanExclusions()
      self.assertEqual(base_score('X',snap,history,volume,now,OP,floats),base_score('X',snap,history,volume,now,OP,floats,d))
 def test_blocked_fill_diagnostic_does_not_change_selection(self):
  scores={str(i):{'score':i} for i in range(500)};sel=Selection();sel.current={'0','1'};sel.last=0
  target=sel.update(scores,set(),1,blocked=True);d=ScanExclusions();d.selection({'0','1'},scores,set(),target,True,False)
  self.assertEqual(set(target),{'0','1'});self.assertEqual(d.retained,2);self.assertEqual(d.unfilled,498);self.assertEqual(d.counts['ADDITIONS_BLOCKED_RECOVERY'],498);self.assertLessEqual(len(d.samples['ADDITIONS_BLOCKED_RECOVERY']),3)
 def test_reason_counts_samples_bounded(self):
  d=ScanExclusions()
  for i in range(500):d.add('SNAPSHOT_AGE_GT60',str(i))
  self.assertEqual(d.counts['SNAPSHOT_AGE_GT60'],500);self.assertEqual(len(d.samples['SNAPSHOT_AGE_GT60']),3)

class RestTimingTests(unittest.TestCase):
 def api(self):return Alpaca({'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'})
 def test_wait_separate_from_hold_and_operation_names(self):
  a=self.api();t=Sim(0);a.timing_clock=t.clock
  class Lock:
   def acquire(self):t.at+=2
   def release(self):pass
  a.lock=Lock()
  for operation in ('scan_rest','incremental','native','arbitrary'):
   with a.operation(operation):
    with a.serialized_request():t.at+=3
  m=a.lock_metrics()['operations'];self.assertEqual(set(m),{'scan_rest','incremental','native','other'})
  for row in m.values():self.assertEqual(row['count'],1);self.assertEqual(row['wait_mean_seconds'],2);self.assertEqual(row['hold_mean_seconds'],3)
 def test_heavy_gate_sleep_excluded_from_lock_wait(self):
  a=self.api();t=Sim(0);a.timing_clock=t.clock;a.before_request=lambda:setattr(t,'at',t.at+15)
  with a.operation('incremental'):
   with a.serialized_request():t.at+=1
  row=a.lock_metrics()['operations']['incremental'];self.assertEqual(row['wait_total_seconds'],0);self.assertEqual(row['hold_total_seconds'],1)
 def test_exception_releases_lock_and_preserves_exception(self):
  a=self.api()
  with self.assertRaisesRegex(RuntimeError,'fixture'):
   with a.serialized_request():raise RuntimeError('fixture')
  self.assertTrue(a.lock.acquire(False));a.lock.release();self.assertEqual(a.lock_metrics()['operations']['other']['count'],1)
 def test_actual_runtime_metrics_exports_new_fields(self):
  t=Sim();state={'ready':True,'scan_ok':False};state['_scan_gate']=ScanGate(state,t.clock,t.wall);state['_blocked_trades']=BlockedTrades({'X'})
  state['_blocked_trades'].record('X',{'p':2},t.wall());a=self.api()
  with a.operation('native'):
   with a.serialized_request():pass
  stop=NS(value=False);stop.is_set=lambda:stop.value
  async def pause(*args):stop.value=True
  r=NS(stop=stop,wall=t.wall,api=a,ingress=None,a=NS(transport_trusted=False),heavy_window=NS(metrics=lambda:{}))
  env={'r':r,'measure':NS(sample=lambda:{}),'state':state,'selection':NS(metrics=lambda:{}),'scan_diagnostics':NS(snapshot=lambda:{}),'json':json,'pause':pause}
  out=io.StringIO()
  with contextlib.redirect_stdout(out):asyncio.run(nested('metrics',env)())
  result=json.loads(out.getvalue());self.assertEqual(result['scan_gate_blocked']['total_rejected_trade_messages'],1);self.assertEqual(result['rest_lock']['operations']['native']['count'],1);self.assertIn('availability_ratio',result['scan_gate'])
