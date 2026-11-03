import unittest,asyncio,time,threading,json,io,contextlib,os
from types import SimpleNamespace as NS
from datetime import timedelta
from unittest.mock import patch,Mock
from dintegration.clock import ClockProof,ClockUnavailable
from dintegration.live import LiveAdapter
from dintegration.stream_proof import StreamProof
from dintegration.measure import Measurements
from ipr.sender import Sender
from ipr.diagnostics import exception_fields
from test_direct import OP,bar
from dreview.core import Root,Controller,Needs,Blocked
from r4a.guards import AckTrustDeadline,SafetyStop
from r4b.runtime import EvidenceRuntime
from r4b.adapter import EvidenceAdapter
from test_recovery_regression import fixture

class ClockTests(unittest.TestCase):
 def test_real_12_seconds_event_loop_stall_independent_refresh(self):
  calls=[]
  def server():
   calls.append(time.monotonic());v=time.time();return int(v),int((v%1)*1e6)
  c=ClockProof(server,emit=lambda x:None);self.assertTrue(c.refresh());c.start()
  async def go():
   start=time.monotonic();time.sleep(12.1)
   self.assertTrue(c.check());self.assertLess(c.snapshot()['last_success_age_seconds'],5.5)
   self.assertGreaterEqual(len(calls),4)
   print(json.dumps({'test':'REAL_ASYNCIO_STALL','seconds':time.monotonic()-start,'refresh_calls':len(calls),'last_success_age_seconds':c.snapshot()['last_success_age_seconds']}))
  try:asyncio.run(go())
  finally:c.close()
 def test_expiry_still_ten_and_failure_diagnostics(self):
  out=[];c=ClockProof(lambda:(100,0),emit=out.append,loop_lag=lambda:12.)
  with patch('dintegration.clock.time.time',side_effect=[100.,100.]),patch('dintegration.clock.time.monotonic',return_value=5.):self.assertTrue(c.refresh())
  with patch('dintegration.clock.time.monotonic',return_value=15.):self.assertFalse(c.check())
  self.assertEqual(out[-1]['loop_lag_seconds'],12.)
  self.assertEqual(out[-1]['last_success_age_seconds'],10.)
  self.assertIn('rtt_seconds',out[-1]);self.assertIn('uncertainty_seconds',out[-1])
 def test_uncertainty_limit_unchanged(self):
  c=ClockProof(lambda:(100,0),emit=lambda x:None)
  with patch('dintegration.clock.time.time',side_effect=[100.,102.]):self.assertFalse(c.refresh())
  self.assertFalse(c.check())
 def test_redis_failure_false_no_old_proof_revival(self):
  c=ClockProof(lambda:(_ for _ in ()).throw(ConnectionError('private')),emit=lambda x:None)
  with self.assertRaises(ConnectionError):c.refresh()
  self.assertFalse(c.check());self.assertEqual(c.snapshot()['refresh_error_type'],'builtins.ConnectionError')
 def test_loop_lag_current_includes_still_blocked_window(self):
  m=Measurements();m.loop_tick=100;m.loop_last=.01
  with patch('dintegration.measure.time.monotonic',return_value=112.1):self.assertAlmostEqual(m.current_loop_lag(),12.)

class SenderTests(unittest.TestCase):
 def sender(self,exc=None):
  s=Sender.__new__(Sender);s.r=NS(check_session=Mock(),leader=NS(require_current=Mock()),a=NS(checks=Mock(side_effect=exc)));s.store=NS(pending=Mock(return_value=[]));s.ledger=NS(start=Mock(),finish=Mock());return s
 def test_clock_failure_before_START_not_unknown(self):
  s=self.sender(ClockUnavailable('ROOT_OR_CLOCK_UNPROVEN'));s.step();s.ledger.start.assert_not_called();s.ledger.finish.assert_not_called();s.store.pending.assert_not_called()
 def test_sender_coroutine_survives_clock_failure(self):
  s=self.sender(ClockUnavailable('ROOT_OR_CLOCK_UNPROVEN'))
  async def go():
   s.r.stop=asyncio.Event();t=asyncio.create_task(s.run());await asyncio.sleep(.05);self.assertFalse(t.done());s.r.stop.set();await t
  asyncio.run(go());s.ledger.start.assert_not_called()
 def test_root_real_failure_remains_fatal(self):
  with self.assertRaises(Blocked):self.sender(Blocked('ROOT_OR_CLOCK_UNPROVEN')).step()
 def test_leader_loss_remains_fatal(self):
  s=self.sender(ClockUnavailable('ROOT_OR_CLOCK_UNPROVEN'));s.r.leader.require_current.side_effect=RuntimeError('LEASE_LOST')
  with self.assertRaisesRegex(RuntimeError,'LEASE_LOST'):s.step()
 def test_session_close_remains_fatal(self):
  s=self.sender();s.r.check_session.side_effect=ValueError('SESSION_CLOSED')
  with self.assertRaises(ValueError):s.step()
 def test_identity_conflict_remains_fatal(self):
  s=self.sender(ValueError('OUTBOX_IDENTITY_CONFLICT'))
  with self.assertRaises(ValueError):s.step()
 def test_adapter_distinguishes_clock_and_root(self):
  a=LiveAdapter.__new__(LiveAdapter);a.bridge=NS(leadership=NS(require_current=lambda:None));a.root_check=lambda:True;a.clock_check=lambda:False
  with self.assertRaises(ClockUnavailable):a.checks()
  a.root_check=lambda:False
  with self.assertRaises(Blocked) as e:a.checks()
  self.assertNotIsInstance(e.exception,ClockUnavailable)

class RecoveryTests(unittest.TestCase):
 def adapter(self):
  raw,l,redis,src,b,case=fixture({'X','Y'});proof=case.proof;proof.connected(started=OP+timedelta(seconds=35));proof.set_scope({'X','Y'})
  c=Controller(Root('2026-10-28',frozenset({'X','Y'}),'a'*64,True));c.reconnect();c.set_desired({'X','Y'},Needs(1));c.plan(0.);self.assertTrue(c.ack(c.connection,['X','Y'],[],.1))
  now=[OP+timedelta(seconds=35)];epochs=[];state=NS(append_epoch=epochs.append);measure=Measurements()
  a=EvidenceAdapter(c,b,src,NS(),lambda:True,lambda *a:True,lambda:True,state,measure,proof=proof,barrier=case,wall=lambda:now[0]);a.authenticated=True
  return a,src,now,epochs
 def test_first_ACK_no_witness_cannot_prove(self):
  a,src,now,_=self.adapter();e=a.proof.evidence('X',[bar(OP)],now[0]);self.assertFalse(e['proven']);self.assertEqual(e['reason'],'NO_COMPLETED_LIVE_BAR_WITNESS');self.assertFalse(a.recover(now[0]));self.assertEqual(a.report['reason'],'NO_SYMBOL_CONTINUITY_PROVEN')
 def test_new_scheduler_waits_then_real_adapter_proves_inside_T120(self):
  async def go():
   a,src,now,_=self.adapter();ack=now[0];elapsed=[0.];calls=[];old=src.warmup
   def warm(symbols,at,required):
    calls.append((symbols,required));one,five=old(symbols,at,required);elapsed[0]+=53.75194773636758;now[0]+=timedelta(seconds=53.75194773636758);return one,five
   src.warmup=warm
   # Exact requested native-history lengths are unchanged; fake source only.
   r=EvidenceRuntime(a,a.checkpoint,a.bridge.leadership,None,lambda:None,NS(time_allows=lambda t:True),clock=lambda:elapsed[0],wall=lambda:now[0],sample=lambda:{})
   r.auth=True;r.wake.set();t=asyncio.create_task(r.recovery_loop());await asyncio.sleep(.03);self.assertEqual(r.attempts,0);self.assertFalse(calls)
   now[0]=OP+timedelta(seconds=60.1);elapsed[0]=25.1;r.received(dict(bar(OP),T='b',S='X'),now[0]);await asyncio.sleep(.08)
   self.assertTrue(a.transport_trusted);self.assertEqual(r.attempts,1);self.assertEqual(calls[0][0],{'X'});self.assertEqual(calls[0][1],305);self.assertLess(elapsed[0],120)
   d=AckTrustDeadline(lambda:elapsed[0]);d.armed=0.;d.tick(a.transport_trusted);self.assertIsNone(d.armed)
   print(json.dumps({'test':'RECOVERY_SCHEDULE_SIMULATION','ACK_to_witness_seconds':25.1,'assumed_warmup_seconds':53.75194773636758,'ACK_to_trust_seconds':elapsed[0],'T120':120,'production_latency':'UNKNOWN'}))
   r.stop.set();r.wake.set();t.cancel();await asyncio.gather(t,return_exceptions=True)
  asyncio.run(go())
 def test_wrong_values_do_not_create_trust(self):
  a,src,now,_=self.adapter();now[0]=OP+timedelta(seconds=61);x=bar(OP);x['v']=999;a.proof.observe(dict(x,T='b',S='X'),now[0]);self.assertFalse(a.recover(now[0]));self.assertEqual(a.report['quarantine']['X'],'REST_STREAM_BAR_VALUE_MISMATCH')
 def test_no_minute_required_for_sparse_symbol(self):
  p=StreamProof();p.connected(started=OP);p.set_scope({'X'});b=bar(OP);now=OP+timedelta(minutes=5);p.observe(dict(b,T='b',S='X'),OP+timedelta(minutes=1));self.assertTrue(p.evidence('X',[b],now)['proven'])
 def test_mandatory_pin_is_in_warmup_even_without_live_witness(self):
  a,src,now,_=self.adapter();a.c.needs=NS(trades={'Y'},R=set(),U=set());calls=[];old=src.warmup
  src.warmup=lambda ss,*args:(calls.append(set(ss)) or old(ss,*args));a.recover(now[0]);self.assertEqual(calls,[{'Y'}])
 def test_future_receipt_not_in_attempt_asof(self):
  a,src,now,_=self.adapter();a.proof.observe(dict(bar(OP),T='b',S='X'),OP+timedelta(seconds=61));self.assertFalse(a.proof.witnessed(now[0]))
 def test_deadline_remains_120_if_no_BAR_arrives(self):
  at=[0.];d=AckTrustDeadline(lambda:at[0]);d.ack();at[0]=119.999;d.tick(False);at[0]=120.
  with self.assertRaisesRegex(SafetyStop,'WS_ACK_UNTRUSTED_TIMEOUT'):d.tick(False)

class DiagnosticTests(unittest.TestCase):
 def test_redaction_before_120_and_full_type(self):
  secret='fixture-private-value-not-a-real-key'
  with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':secret}):v=exception_fields(ValueError('token='+secret+' https://fixture.invalid/private '+('x'*200)))
  self.assertNotIn(secret,v['message_redacted_120']);self.assertNotIn('fixture.invalid',v['message_redacted_120']);self.assertLessEqual(len(v['message_redacted_120']),120);self.assertEqual(v['exception_type'],'builtins.ValueError')
 def test_recovery_quarantine_counts_use_report_not_stale_attribute(self):
  from ipr.runtime import DirectEvidence
  a=DirectEvidence.__new__(DirectEvidence);a.report={'reason':'NO_SYMBOL_CONTINUITY_PROVEN','quarantine':{str(i):'NO_COMPLETED_LIVE_BAR_WITNESS' for i in range(8)}};a.quarantine={};a.wall=lambda:OP;a.proof=NS(live_count=lambda *args:0,stored_count=lambda *args:0)
  with patch('r4b.adapter.EvidenceAdapter.recover',return_value=False),contextlib.redirect_stdout(io.StringIO()) as out:a.recover(OP)
  q=json.loads(out.getvalue());self.assertEqual(q['quarantined_symbols'],8);self.assertEqual(len(q['quarantine_sample']),5);self.assertEqual(q['quarantine_reason_counts'],{'NO_COMPLETED_LIVE_BAR_WITNESS':8})
 def test_phase_timing_on_exception(self):
  from dintegration.timing import phase
  with contextlib.redirect_stdout(io.StringIO()) as out:
   with self.assertRaises(ValueError):
    with phase('fixture'):raise ValueError('not printed')
  q=json.loads(out.getvalue());self.assertFalse(q['success']);self.assertGreaterEqual(q['seconds'],0)

class ParentAndTimingTests(unittest.TestCase):
 def test_parent_reports_actual_child_2_separately_from_restart_75(self):
  from r4a.parent import ProcessGuard
  g=ProcessGuard.__new__(ProcessGuard);g.controller=NS(boot_id='fixture',boot_number=2,policy=NS(),after_worker_exit=lambda external:'RESTART_REQUESTED');g.clock=lambda:0.;g.wall=lambda:0.;g.authorize=lambda:None;g.sample=lambda:{'limit_bytes':512*1024**2,'current_bytes':1,'anon_bytes':1};g.spawn=lambda *a:NS(poll=lambda:2);g.sleep=lambda *a:None;g.external_stop=False;g.reason=None
  with patch('r4a.parent.startup_claim',return_value='ALLOW'),patch('r4a.parent.restart_cooldown',return_value=True):q=g.run()
  self.assertEqual(q['exit_code'],75);self.assertEqual(q['child_exit_code'],2)
 def test_archive_timing_replay_and_restore_chain_unchanged(self):
  from r4a.durable import RedisState
  from r4b.store import ProofStore
  from lua_redis import LuaRedis
  from r4a.redis_ports import leadership
  r=LuaRedis();l=leadership(r,'w');l.acquire();state=RedisState(r,'2026-10-28','a'*64,'w',1)
  with contextlib.redirect_stdout(io.StringIO()) as out:
   state.restore();state.append_epoch({'case_schema':2,'add_R':['X']});store=ProofStore(state,StreamProof());store.load_archive()
  self.assertEqual(store.pending_R,{'X'})
  phases=[json.loads(x)['phase'] for x in out.getvalue().splitlines()]
  self.assertIn('state.restore.verify_chain',phases);self.assertIn('load_archive.replay',phases);self.assertIn('load_archive',phases)
 def test_first_ACK_witness_can_be_older_on_rebalance(self):
  # The restriction applies to first/new scope evidence, NOT every ACK forever.
  p=StreamProof();p.connected(started=OP);p.set_scope({'X'});p.observe(dict(bar(OP),T='b',S='X'),OP+timedelta(seconds=60.1));self.assertEqual(p.witnessed(OP+timedelta(seconds=61)),{'X'})
 def test_failed_clock_does_not_hide_root_mismatch(self):
  a=LiveAdapter.__new__(LiveAdapter);a.bridge=NS(leadership=NS(require_current=lambda:None));a.root_check=lambda:False;a.clock_check=lambda:False
  with self.assertRaises(Blocked) as e:a.checks()
  self.assertNotIsInstance(e.exception,ClockUnavailable)
