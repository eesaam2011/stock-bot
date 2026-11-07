"""R9.3: offline observability regressions; no external service calls."""
import asyncio,copy,io,json,sys,types,unittest
from contextlib import redirect_stdout
from unittest.mock import patch,MagicMock
from datetime import timedelta
from ipr import main as parent
from ipr.diagnostics import source_error_fields
from ipr.preparation import prepare_root
from r4a.runtime import Runtime
from r4a.guards import SafetyStop
from test_direct import NOW,CAL,OP
from lua_redis import LuaRedis

class ParentTelemetry(unittest.TestCase):
 def capture(self,fn):
  out=io.StringIO()
  with redirect_stdout(out):fn()
  return [json.loads(s) for s in out.getvalue().splitlines()]
 def test_ok_counts_eligible_not_active_union_and_preserves_bundle(self):
  b={'session':'2026-11-07','eligible':['X'],'symbols':['X','Y'],'new_entries_prepared':False};before=copy.deepcopy(b)
  rows=self.capture(lambda:parent.ParentLogs().prepared(b,b['session'],None))
  self.assertEqual(rows,[{'stage':'PREP_ROOT_OK','session':b['session'],'eligible_symbols':1,'new_entries_prepared':False}]);self.assertEqual(b,before)
 def test_none_once_per_UTC_day_with_repeated_polling(self):
  p=parent.ParentLogs()
  def go():
   for _ in range(2880):p.prepared(None,'2026-11-07','NO_SESSION_AND_NO_ACTIVE')
   p.prepared(None,'2026-11-08','NO_SESSION_AND_NO_ACTIVE')
  rows=self.capture(go);self.assertEqual(len(rows),2);self.assertEqual([r['session'] for r in rows],['2026-11-07','2026-11-08'])
 def test_idle_hourly_without_changing_callers_polling(self):
  p=parent.ParentLogs()
  rows=self.capture(lambda:[p.idle('2026-11-07',None,t) for t in range(7201)])
  self.assertEqual(len(rows),3);self.assertEqual(len(rows[0]['fingerprint']),12);self.assertEqual(len({r['fingerprint'] for r in rows}),1)
 def test_idle_reports_parked_day_without_changing_it(self):
  p=parent.ParentLogs();rows=self.capture(lambda:[p.idle('2026-11-07','2026-11-06',0),p.idle('2026-11-08','2026-11-07',3600)])
  self.assertEqual(rows[0]['parked_day'],'2026-11-06');self.assertNotEqual(rows[0]['fingerprint'],rows[1]['fingerprint'])
 def test_new_logs_swallow_print_failure(self):
  with patch('builtins.print',side_effect=OSError('fixture')):
   p=parent.ParentLogs();p.idle('2026-11-07',None,0);p.prepared(None,'2026-11-07','NO_SESSION_AND_NO_ACTIVE');p.prepared({'session':'x','eligible':[],'new_entries_prepared':False},'x',None)
 def test_guard_pause_preserves_every_sleep_duration_even_when_log_fails(self):
  with patch.object(parent.time,'sleep') as sleep,patch('builtins.print',side_effect=OSError('fixture')):
   p=parent.ParentLogs()
   for seconds in [.25,1,30]:p.pause(seconds,None)
  self.assertEqual([c.args[0] for c in sleep.call_args_list],[.25,1,30])

class PreparationTelemetry(unittest.TestCase):
 def test_closed_day_reason_and_no_changed_redis_effects(self):
  class API:
   def calendar(self,*a):return []
   def assets(self):raise AssertionError('must not fetch')
  a,b=LuaRedis(),LuaRedis();seen=[]
  self.assertIsNone(prepare_root(API(),a,NOW));self.assertIsNone(prepare_root(API(),b,NOW,observe_none=seen.append))
  self.assertEqual(a.state,b.state);self.assertEqual(seen,['NO_SESSION_AND_NO_ACTIVE'])
 def test_empty_eligible_is_not_falsely_labeled_no_session(self):
  class API:
   def calendar(self,*a):return CAL
   def assets(self):return {}
  seen=[];self.assertIsNone(prepare_root(API(),LuaRedis(),NOW,observe_none=seen.append));self.assertEqual(seen,['NO_ELIGIBLE_AND_NO_ACTIVE'])
 def test_callback_exception_keeps_original_none(self):
  class API:
   def calendar(self,*a):return []
  def fail(*a):raise RuntimeError('telemetry only')
  self.assertIsNone(prepare_root(API(),LuaRedis(),NOW,observe_none=fail))
 def test_success_and_cache_identical_with_observer(self):
  class API:
   def calendar(self,*a):return CAL
   def assets(self):return {'X':{'name':'X Common Stock','exchange':'NASDAQ','status':'active','tradable':True,'class':'us_equity','id':'fixture'}}
  a,b=LuaRedis(),LuaRedis();seen=[]
  first=prepare_root(API(),a,NOW);observed=prepare_root(API(),b,NOW,observe_none=seen.append)
  self.assertEqual(first,observed);self.assertEqual(a.state,b.state);self.assertEqual(prepare_root(API(),b,NOW,observe_none=seen.append),first);self.assertEqual(seen,[])

class ChildTelemetry(unittest.TestCase):
 def invoke(self,exc):
  redis=types.ModuleType('redis');redis.Redis=MagicMock();retry=types.ModuleType('redis.retry');retry.Retry=MagicMock();backoff=types.ModuleType('redis.backoff');backoff.NoBackoff=MagicMock()
  async def run(*a,**kw):raise exc
  env={k:'fixture-only' for k in ('REDIS_URL','APCA_API_KEY_ID','APCA_API_SECRET_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID')};out=io.StringIO()
  with patch.dict(sys.modules,{'redis':redis,'redis.retry':retry,'redis.backoff':backoff}),patch.dict('os.environ',env,clear=True),patch.object(sys,'argv',['ipr.main','--child','--boot-id','fixture','--parent-pid','123','--session','2026-11-06']),patch.object(parent,'arm_parent_death'),patch.object(parent,'load',return_value={'session':'2026-11-06'}),patch.object(parent,'Alpaca'),patch.object(parent,'run',run),redirect_stdout(out):result=parent.main()
  return result,[json.loads(s) for s in out.getvalue().splitlines()]
 def test_session_closed_note_precedes_unchanged_fatal_and_exit2(self):
  code,rows=self.invoke(ValueError('SESSION_CLOSED'));self.assertEqual(code,2)
  self.assertEqual([r['stage'] for r in rows],['CHILD_SESSION_CLOSED_EXPECTED','IPR_FATAL','IPR_CHILD_EXIT']);self.assertEqual(rows[0]['session'],'2026-11-06');self.assertIn('at',rows[0]);self.assertEqual(rows[-1]['exit_code'],2);self.assertEqual(rows[-1]['reason'],'SESSION_CLOSED')
 def test_other_failure_not_marked_expected(self):
  code,rows=self.invoke(ValueError('PREP_ROOT_MISSING'));self.assertEqual(code,2);self.assertEqual([r['stage'] for r in rows],['IPR_FATAL','IPR_CHILD_EXIT'])
 def test_same_text_different_exception_type_not_marked_expected(self):
  code,rows=self.invoke(RuntimeError('SESSION_CLOSED'));self.assertEqual(code,2);self.assertNotIn('CHILD_SESSION_CLOSED_EXPECTED',[r['stage'] for r in rows])

class SourceTelemetry(unittest.TestCase):
 def test_only_code_and_bounded_sanitized_message_copied(self):
  msg={'code':407,'msg':'x'*500,'api_key':'must-not-copy','extra':{'secret':'must-not-copy'}};before=copy.deepcopy(msg);out=source_error_fields(msg)
  self.assertEqual(set(out),{'code','msg'});self.assertEqual(out['code'],407);self.assertEqual(len(out['msg']),120);self.assertEqual(msg,before)
 def test_secrets_urls_bearer_and_chat_redacted(self):
  with patch.dict('os.environ',{'APCA_API_SECRET_KEY':'synthetic-secret'},clear=True):
   text=source_error_fields({'code':'407','msg':'synthetic-secret https://example.invalid/token postgres://user:pass@host db chat_id=123456789 bearer abcdef api_key=forbidden'})['msg']
  for secret in ['synthetic-secret','example.invalid','user:pass','123456789','abcdef','forbidden']:self.assertNotIn(secret,text)
 def test_invalid_code_not_echoed(self):
  for code in ['secret-value',{'token':'x'},True,'9'*100]:self.assertIsNone(source_error_fields({'code':code})['code'])
 def test_each_credential_removed_before_truncation_and_nontext_not_echoed(self):
  for text in ['key=fixture_secret',"{'token': 'fixture_secret'}",'chat_id=fixture_secret','Bearer fixture_secret','postgres://fixture_secret']:
   self.assertNotIn('fixture_secret',source_error_fields({'msg':text})['msg'])
  self.assertEqual(source_error_fields({'msg':{'extra':'fixture_secret'}})['msg'],'[REDACTED_NON_TEXT]')
 def read_error(self,print_fails=False):
  rt=object.__new__(Runtime);rt.check_session=MagicMock();rt.wall=MagicMock();rt.auth=False
  async def ws():yield json.dumps([{'T':'error','code':407,'msg':'fixture source error'}])
  rt.ws=ws();out=io.StringIO()
  with redirect_stdout(out):
   if print_fails:
    with patch('builtins.print',side_effect=OSError('fixture')):
     with self.assertRaisesRegex(SafetyStop,'SOURCE_ERROR'):asyncio.run(rt._reader())
   else:
    with self.assertRaisesRegex(SafetyStop,'SOURCE_ERROR'):asyncio.run(rt._reader())
  return out.getvalue()
 def test_real_reader_logs_once_and_still_raises_same_SafetyStop(self):
  rows=[json.loads(x) for x in self.read_error().splitlines()];self.assertEqual(rows,[{'stage':'SOURCE_ERROR','code':407,'msg':'fixture source error'}])
 def test_log_failure_does_not_mask_source_SafetyStop(self):self.read_error(True)
