import asyncio,copy,contextlib,io,json,unittest
from r9core.chain import ChainState,RECORD_BYTES,SEGMENT_BYTES,StateConflict,EvidenceRecordTooLarge,ChainFailure,previous_cases
from r9core.lifecycle import FirstFailure,wait_first,close_tasks
from r4a.redis_ports import leadership
from r4a.durable import PREFIX,canonical
from lua_redis import LuaRedis
class CountingRedis(LuaRedis):
 def __init__(self):super().__init__();self.read_fields=[];self.allkeys=[]
 def get(self,k):self.allkeys.append(k);return super().get(k)
 def hget(self,k,f):self.read_fields.append((k,f));self.allkeys.append(k);return super().hget(k,f)
class ChainTests(unittest.TestCase):
 def fixture(self,limit=16000):
  r=CountingRedis();l=leadership(r,'w');l.acquire();s=ChainState(r,'2026-11-05','a'*64,'w',1,segment_bytes=limit,emit=lambda *a,**k:None);s.restore();return r,l,s
 def test_round_trip_facts_and_cache_one_pass(self):
  r,l,s=self.fixture();s.append_epoch({'kind':'ADD','case_schema':2,'add_U':['X'],'add_R':['Y']});s.append_epoch({'kind':'RESOLVE','case_schema':2,'resolved_U':['X']})
  t=ChainState(r,s.session,s.root_hash,'w',1);t.restore();n=len(r.read_fields);t.restore();self.assertEqual(len(r.read_fields),n);self.assertEqual(t.archive['U'],[]);self.assertEqual(t.archive['R'],['Y']);self.assertFalse(t.restore()['transport_trusted'])
 def test_rotate_only_reads_active_fields_and_seals(self):
  r,l,s=self.fixture(5500)
  for i in range(18):s.append_epoch({'kind':'PAD','pad':'x'*1000,'add_R':['Y']})
  self.assertGreater(s.head['segment'],1);r.read_fields=[]
  t=ChainState(r,s.session,s.root_hash,'w',1,segment_bytes=5500);t.restore()
  self.assertTrue(all(k==s.key(s.head['segment']) for k,f in r.read_fields));self.assertEqual(t.archive['R'],['Y'])
 def test_missing_sealed_piece_fails_closed(self):
  r,l,s=self.fixture(3500)
  for i in range(5):s.append_epoch({'kind':'PAD','pad':'x'*800})
  r.state['hashes'].pop(s.key(1))
  with self.assertRaises(StateConflict):ChainState(r,s.session,s.root_hash,'w',1).restore()
 def test_corrupt_previous_seal_fails_closed(self):
  r,l,s=self.fixture(3500)
  for i in range(5):s.append_epoch({'kind':'PAD','pad':'x'*800})
  r.state['values'][s.seal_key(1)]=r.state['values'][s.seal_key(1)].replace('last_sha256','wrong_sha256')
  with self.assertRaises(StateConflict):ChainState(r,s.session,s.root_hash,'w',1).restore()
 def test_fencing_loss_before_commit_changes_nothing(self):
  r,l,s=self.fixture();s.append_epoch({'kind':'FIRST'});before=copy.deepcopy(r.state);old=r.eval
  def eval(script,n,*args):
   if script.startswith('\n-- chain2 commit'):
    r.state['values'][s.leader]='other'
   return old(script,n,*args)
  r.eval=eval
  with self.assertRaises(StateConflict):s.append_epoch({'kind':'NEXT'})
  self.assertEqual(r.state['hashes'],before['hashes']);self.assertEqual(r.get(s.head_key),before['values'][s.head_key])
 def test_record_cap_and_named_error_no_write(self):
  r,l,s=self.fixture();before=copy.deepcopy(r.state)
  with self.assertRaisesRegex(EvidenceRecordTooLarge,'EVIDENCE_RECORD_TOO_LARGE.*HUGE.*bytes='):s.append_epoch({'kind':'HUGE','payload':'x'*RECORD_BYTES})
  self.assertEqual(r.state,before)
 def test_waiver_once_and_both_dates_and_no_legacy_reads(self):
  r,l,s=self.fixture();out=io.StringIO();s.emit=lambda *a,**k:print(*a,**k)
  with contextlib.redirect_stdout(out):
   s.append_epoch({'kind':'ONE'});s.append_epoch({'kind':'TWO'})
   t=ChainState(r,s.session,s.root_hash,'w',1,emit=s.emit);t.restore();t.append_epoch({'kind':'THREE'})
  lines=[json.loads(x) for x in out.getvalue().splitlines()];self.assertEqual(len(lines),1);self.assertEqual(lines[0]['sessions'],['2026-11-03','2026-11-04']);self.assertFalse(any(':session:' in k for k in r.allkeys))
 def test_next_session_carries_chain2_only(self):
  r,l,s=self.fixture();s.append_epoch({'kind':'FACT','case_schema':2,'add_U':['X'],'add_R':['Y']});self.assertEqual(previous_cases(r,'2026-11-06'),({'X'},{'Y'}));self.assertFalse(any(':session:' in k for k in r.allkeys))
 def test_partial_append_is_detected(self):
  r,l,s=self.fixture();s.append_epoch({'kind':'OK'});r.state['hashes'][s.key(1)][str(s.seq+1)]='orphan'
  with self.assertRaises(StateConflict):ChainState(r,s.session,s.root_hash,'w',1).restore()
 def test_ambiguous_reply_restart_checks_committed_chain(self):
  r,l,s=self.fixture();s.append_epoch({'kind':'OK'});old=r.eval
  def eval(script,n,*args):
   if script.startswith('\n-- chain2 commit'):r.lose_reply=True
   return old(script,n,*args)
  r.eval=eval
  with self.assertRaises(ChainFailure):s.append_epoch({'kind':'UNKNOWN','add_R':['X']})
  t=ChainState(r,s.session,s.root_hash,'w',1);t.restore();self.assertEqual(t.archive['R'],['X'])
class ShutdownTests(unittest.TestCase):
 def test_failed_child_cleanup_cannot_be_hidden_by_normal_sibling(self):
  async def run():
   failure=FirstFailure();stop=asyncio.Event()
   async def explode():await asyncio.sleep(.001);raise RuntimeError('FIRST_CAUSE')
   async def slow():
    try:await asyncio.Event().wait()
    finally:await asyncio.sleep(.02)
   async def inner():
    tasks=[asyncio.create_task(explode()),asyncio.create_task(slow())]
    try:await wait_first(tasks,failure)
    finally:stop.set();await close_tasks(tasks,failure)
   async def normal():await stop.wait()
   tasks=[asyncio.create_task(inner()),asyncio.create_task(normal())]
   try:await wait_first(tasks,failure)
   finally:await close_tasks(tasks,failure)
  with self.assertRaisesRegex(RuntimeError,'FIRST_CAUSE'):asyncio.run(run())

class AdditionalTests(unittest.TestCase):
 fixture=ChainTests.fixture
 def test_oversize_full_recovery_not_swallowed(self):
  import test_r3
  from datetime import timedelta
  f=test_r3.setup();f.now[0]=test_r3.OP+timedelta(seconds=61)
  for symbol in f.history:test_r3.receive(f,symbol,0)
  def reject(e):raise EvidenceRecordTooLarge(e.get('kind','OTHER'),RECORD_BYTES+1)
  f.a.checkpoint.append_epoch=reject
  with self.assertRaises(EvidenceRecordTooLarge):f.a.recover(f.now[0])
  self.assertFalse(f.a.transport_trusted)
 def test_same_exception_invalidates_once_without_controller_snapshot(self):
  import test_r3
  from unittest.mock import Mock
  f=test_r3.trusted();f.a.checkpoint.append_epoch=Mock()
  for i in range(20):f.a.revoke('RepeatedClockFailure')
  self.assertEqual(f.a.checkpoint.append_epoch.call_count,1)
  e=f.a.checkpoint.append_epoch.call_args.args[0];self.assertEqual(e['kind'],'TRANSPORT_INVALIDATED');self.assertNotIn('controller',e);self.assertNotIn('epoch',e);self.assertFalse(f.a.transport_trusted);self.assertEqual(f.a.invalidation_counts['RepeatedClockFailure'],20)
 def test_full_audit_reads_sealed_payload_and_detects_mutation(self):
  from r9core.audit import full_verify
  r,l,s=self.fixture(3500)
  for i in range(9):s.append_epoch({'kind':'PAD','payload':'p'*700,'case_schema':2,'add_R':['Y']})
  out=full_verify(s);self.assertTrue(out['sealed_payloads_checked']);self.assertEqual(out['records'],s.seq)
  r.state['hashes'][s.key(1)]['2']=r.state['hashes'][s.key(1)]['2'].replace('pppp','qqqq')
  # Short boot does not read sealed bytes; full audit must reject them.
  t=ChainState(r,s.session,s.root_hash,'w',1);t.restore()
  with self.assertRaises(StateConflict):full_verify(t)
 def test_partial_rotation_not_published_and_fails_closed(self):
  r,l,s=self.fixture(3500);s.append_epoch({'kind':'PAD','payload':'p'*1000});old=r.eval
  def interrupted(script,n,*args):
   if script.startswith('\n-- chain2 commit') and args[n+3]=='rotate':
    script=script.replace("redis.call('SET',KEYS[4],ARGV[10])","error('SIMULATED_INTERRUPTED_COMMIT');redis.call('SET',KEYS[4],ARGV[10])")
   return old(script,n,*args)
  r.eval=interrupted
  with self.assertRaises(Exception):
   for i in range(6):s.append_epoch({'kind':'PAD','payload':'p'*1000})
  r.eval=old
  with self.assertRaises(StateConflict):ChainState(r,s.session,s.root_hash,'w',1).restore()
 def test_legacy_full_verifier_original_reader(self):
  from verify_evidence_archive import ExportReader
  from r4a.durable import RedisState
  r,l,s=self.fixture();legacy=RedisState(r,s.session,s.root_hash,'w',1);legacy.restore();legacy.append_epoch({'case_schema':2,'add_U':['X']})
  data={'strings':copy.deepcopy(r.state['values']),'hashes':copy.deepcopy(r.state['hashes'])};reader=ExportReader(data);old=RedisState(reader,s.session,s.root_hash,'read_only',0);old.restore();self.assertEqual(old.seq,1)
 def test_new_prepare_does_not_read_legacy_even_when_oversized_exists(self):
  from ipr.preparation import carried
  r,l,s=self.fixture();r.state['values'][PREFIX+':session:2026-11-04:checkpoint']='UNREADABLE_LEGACY';r.state['hashes'][PREFIX+':session:2026-11-04:epochs']={'1':'UNREADABLE'}
  r.allkeys=[];self.assertEqual(carried(r,'2026-11-05'),(set(),set()));self.assertFalse(any(':session:' in x for x in r.allkeys))
 def test_warning_does_not_stop_or_close_journal(self):
  from unittest.mock import patch
  r,l,s=self.fixture();rows=[];s.emit=lambda x,**kw:rows.append(json.loads(x))
  with patch('r9core.chain.SESSION_WARN_BYTES',500):
   s.append_epoch({'kind':'FIRST'});s.append_epoch({'kind':'SECOND'})
  self.assertEqual(sum(x['stage']=='EVIDENCE_SESSION_SIZE_WARNING' for x in rows),1);self.assertGreater(s.seq,2)

class ProductionBootstrap(unittest.TestCase):
 def test_empty_namespace_waives_only_authorized_boundary_day(self):
  from r9core.chain import validate_bootstrap
  r=CountingRedis();validate_bootstrap(r,'2026-11-05')
  with self.assertRaises(StateConflict):validate_bootstrap(r,'2026-11-06')
  self.assertFalse(any(':session:' in k for k in r.allkeys))
 def test_later_day_with_existing_chain2_history_is_allowed(self):
  from r9core.chain import validate_bootstrap
  r=CountingRedis();r.set(PREFIX+':chain2:latest','2026-11-05');r.set(PREFIX+':chain2:waiver',canonical({'stage':'U_R_CARRY_WAIVED','sessions':['2026-11-03','2026-11-04']}));validate_bootstrap(r,'2026-11-06')
 def test_no_missing_checkpoint_fallback(self):
  r=CountingRedis();r.set(PREFIX+':chain2:latest','2026-11-05')
  with self.assertRaises(StateConflict):previous_cases(r,'2026-11-06')

class ExtraIntegrity(unittest.TestCase):
 fixture=ChainTests.fixture
 def test_fencing_loss_during_rotation(self):
  r,l,s=self.fixture(3500);s.append_epoch({'kind':'PAD','pad':'x'*1100});before=copy.deepcopy(r.state);old=r.eval;rotated=[]
  def lose(script,n,*args):
   if script.startswith('\n-- chain2 commit') and args[n+3]=='rotate':r.state['values'][s.gen]='2';rotated.append(True)
   return old(script,n,*args)
  r.eval=lose
  with self.assertRaises(StateConflict):
   for i in range(6):s.append_epoch({'kind':'PAD','pad':'x'*1100})
  self.assertTrue(rotated);self.assertEqual(r.state['hashes'],before['hashes']);self.assertEqual(r.get(s.head_key),before['values'][s.head_key])
 def test_cached_root_and_leadership_checked(self):
  r,l,s=self.fixture();s.append_epoch({'kind':'ONE'});s.root_hash='b'*64
  with self.assertRaises(StateConflict):s.restore()
  s.root_hash='a'*64;r.state['values'][s.leader]='other'
  with self.assertRaises(StateConflict):s.restore()
 def test_session_kind_counters_survive_boot_but_boot_counts_reset(self):
  r,l,s=self.fixture();s.append_epoch({'kind':'X'});s.append_epoch({'kind':'X'})
  t=ChainState(r,s.session,s.root_hash,'w',1,emit=lambda *a,**k:None);t.restore()
  self.assertEqual(t.metrics()['count'],0);self.assertEqual(t.metrics()['by_kind_session']['X']['count'],2)
 def test_actual_resource_snapshot_has_stat_and_sqlite_fields(self):
  from dintegration.measure import Measurements
  x=Measurements().sample();self.assertIn('anon_bytes',x);self.assertIn('file_bytes',x);self.assertIn('shmem_bytes',x);self.assertIn('sqlite_total_bytes',x)

class ActualRuntimeFailure(unittest.TestCase):
 def test_actual_Runtime_run_preserves_watch_exception_through_outer_stop(self):
  from r4a.runtime import Runtime
  from r9core.lifecycle import FirstFailure
  from types import SimpleNamespace as NS
  class R(Runtime):
   def __init__(self):
    self.stop=asyncio.Event();self.initialized=asyncio.Event();self.failure=FirstFailure();self.memory=NS(check=lambda x:None);self.state=NS(restore=lambda:None);self.leader=NS(require_current=lambda:None)
   def revoke(self,x):pass
   def check_session(self):pass
   def sample(self):return {}
   def checkpoint(self,x):pass
   async def connections(self):
    try:await asyncio.Event().wait()
    finally:await asyncio.sleep(.02)
   async def _watch(self):await asyncio.sleep(.001);raise RuntimeError('ACTUAL_WATCH_FAILURE')
   async def _renew(self):await asyncio.Event().wait()
  async def run():
   r=R()
   async def normal():await r.stop.wait()
   tasks=[asyncio.create_task(r.run()),asyncio.create_task(normal())]
   try:await wait_first(tasks,r.failure)
   finally:await close_tasks(tasks,r.failure)
  with self.assertRaisesRegex(RuntimeError,'ACTUAL_WATCH_FAILURE'):asyncio.run(run())
 def test_missing_chain2_index_after_initialization_is_not_empty_history(self):
  r,l,s=ChainTests.fixture(self);s.append_epoch({'kind':'ONE'});r.state['values'].pop(PREFIX+':chain2:latest')
  with self.assertRaises(StateConflict):previous_cases(r,'2026-11-06')

class CaseIntegrity(unittest.TestCase):
 def test_missing_canonical_case_referenced_by_checkpoint_fails_closed(self):
  r,l,s=ChainTests.fixture(self);s.append_epoch({'kind':'CASE','case_refs':{'O':[],'A':['operational_priority_radar:v1:trade:TRADE:X']}})
  with self.assertRaisesRegex(StateConflict,'CANONICAL_CASE_MISSING'):ChainState(r,s.session,s.root_hash,'w',1).restore()

class InitialCommitIntegrity(unittest.TestCase):
 def test_partial_initialization_without_index_is_rejected(self):
  r,l,s=ChainTests.fixture(self);old=r.eval
  def interrupt(script,n,*args):
   if script.startswith('\n-- chain2 commit'):script=script.replace("redis.call('SET',KEYS[7],ARGV[12])","error('INTERRUPTED_INIT');redis.call('SET',KEYS[7],ARGV[12])")
   return old(script,n,*args)
  r.eval=interrupt
  with self.assertRaises(StateConflict):s.append_epoch({'kind':'FIRST'})
  r.eval=old
  with self.assertRaisesRegex(StateConflict,'INDEX_CONFLICT'):ChainState(r,s.session,s.root_hash,'w',1).restore()

class JournalLockOrdering(unittest.TestCase):
 def test_case_snapshot_precedes_journal_and_state_locks(self):
  from r4b.store import JournalState
  r,l,s=ChainTests.fixture(self);j=JournalState(s);calls=[]
  def refs():
   self.assertFalse(s._r4b_lock._is_owned());self.assertFalse(s.lock._is_owned());calls.append(True);return {'O':[],'A':[]}
  s.case_refs=refs;j.append_epoch({'kind':'ORDER'});self.assertEqual(len(calls),1)

class MetricUnknowns(unittest.TestCase):
 def test_sqlite_stat_failure_is_unknown_not_zero(self):
  from unittest.mock import patch
  from dintegration.measure import Measurements
  with patch('pathlib.Path.glob',side_effect=PermissionError('fixture')):s=Measurements().sample()
  self.assertIsNone(s['sqlite_total_bytes']);self.assertIsNone(s['sqlite_files']);self.assertEqual(s['sqlite_stat_error'],'PermissionError')

class ChildExitReport(unittest.TestCase):
 def test_normal_child_exit_prints_returned_reason_and_zero(self):
  import importlib,sys,io,types
  from contextlib import redirect_stdout
  from unittest.mock import patch,MagicMock
  main=importlib.import_module('ipr.main')
  redis=types.ModuleType('redis');redis.Redis=MagicMock()
  retry=types.ModuleType('redis.retry');retry.Retry=MagicMock()
  backoff=types.ModuleType('redis.backoff');backoff.NoBackoff=MagicMock()
  async def run(*a,**kw):return 'SIGNAL_SIGTERM'
  env={k:'fixture-only' for k in ('REDIS_URL','APCA_API_KEY_ID','APCA_API_SECRET_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID')}
  output=io.StringIO()
  with patch.dict(sys.modules,{'redis':redis,'redis.retry':retry,'redis.backoff':backoff}),patch.dict('os.environ',env,clear=True),patch.object(sys,'argv',['ipr.main','--child','--boot-id','fixture','--parent-pid','123','--session','2026-11-05']),patch.object(main,'arm_parent_death'),patch.object(main,'load',return_value={'session':'2026-11-05'}),patch.object(main,'Alpaca'),patch.object(main,'run',run),redirect_stdout(output):
   result=main.main()
  self.assertEqual(result,0)
  rows=[json.loads(x) for x in output.getvalue().splitlines()]
  self.assertEqual(rows[-1],{'stage':'IPR_CHILD_EXIT','exit_code':0,'reason':'SIGNAL_SIGTERM'})

class ConnectionInvalidation(unittest.TestCase):
 def test_repeated_connection_exception_does_not_checkpoint_controller(self):
  import asyncio,types
  from unittest.mock import MagicMock,patch
  from dintegration.live import LiveAdapter
  from r4a.runtime import Runtime
  class Adapter:
   revoke=LiveAdapter.revoke
   def __init__(self):
    self.invalidation_counts={};self._invalidation_open=False;self.c=types.SimpleNamespace(connection=1,revision=0);self.checkpoint=MagicMock()
   def close_eligibility(self,why):pass
  async def go():
   r=object.__new__(Runtime);r.a=Adapter();r.stop=asyncio.Event();r.checkpoint=MagicMock();count=[0]
   async def fail():
    count[0]+=1
    if count[0]==3:r.stop.set()
    raise ConnectionError('fixture')
   async def sleep(*a):pass
   r.connection=fail
   with patch('r4a.runtime.asyncio.sleep',sleep):await r.connections()
   r.checkpoint.assert_not_called();r.a.checkpoint.append_epoch.assert_called_once();event=r.a.checkpoint.append_epoch.call_args.args[0]
   self.assertEqual(event['kind'],'TRANSPORT_INVALIDATED');self.assertNotIn('controller',event);self.assertEqual(r.a.invalidation_counts['CONNECTION_FAILED'],3)
  asyncio.run(go())
 def test_chain2_failure_is_not_retried_as_network_disconnect(self):
  import asyncio
  from unittest.mock import MagicMock
  from r4a.runtime import Runtime
  from r9core.chain import ChainFailure
  async def go():
   r=object.__new__(Runtime);r.stop=asyncio.Event();r.a=MagicMock()
   async def fail():raise ChainFailure('CHAIN2_COMMIT_FAILED')
   r.connection=fail
   with self.assertRaisesRegex(ChainFailure,'CHAIN2_COMMIT_FAILED'):await r.connections()
   r.a.revoke.assert_not_called()
  asyncio.run(go())
