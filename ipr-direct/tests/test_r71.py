"""Offline R7.1 regressions; executes the actual nested scan/metrics code, unchanged AST."""
import ast,asyncio,contextlib,copy,io,json,threading,unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from ipr.wave import HeavyWindow,ScanDiagnostics
from ipr.api import Alpaca
from test_direct import OP

class Sim:
 def __init__(self,at=54.):self.at=at;self.sleeps=[]
 def clock(self):return self.at
 def wall(self):return OP+timedelta(seconds=self.at)
 def sleep(self,seconds):self.sleeps.append(seconds);self.at=round(self.at+seconds,6)

def nested(name,env):
 # Run exactly the shipped run.scan/run.metrics AST with synthetic outer dependencies.
 path=Path(__file__).resolve().parents[1]/'ipr/runtime.py'
 module=ast.parse(path.read_text());run=next(n for n in module.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='run')
 node=copy.deepcopy(next(n for n in run.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name))
 code=compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(path),'exec')
 exec(code,env);return env[name]

class R71Scan(unittest.TestCase):
 def scan_once(self,legacy=False,durations=(1.,25.,27.)):
  t=Sim();gate=HeavyWindow(t.wall,sleep=t.sleep,clock=t.clock)
  if legacy:gate.GATED_OPERATIONS=frozenset({'incremental','scan_rest'})
  times=iter(durations);sent=[]
  class Response:
   status=200
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def read(self,*a):t.sleep(next(times));return b'{}'
  def opener(*a,**kw):sent.append(t.at);return Response()
  api=Alpaca({'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'},opener=opener)
  api.before_request=gate.before_request;api.request_allowed=gate.request_allowed
  class Volumes:
   def __init__(self,*a):self.watermark=None
   def update(self,api,symbols,now):
    api.get('/v2/stocks/bars',{});self.watermark=now;return {}
  def snapshots(*a):
   api.get('/v2/stocks/snapshots',{});api.get('/v2/stocks/snapshots',{});return {}
  stop=NS(value=False);stop.is_set=lambda:stop.value
  async def pause(*a):stop.value=True
  needs=NS(pins=set());planner=Mock()
  r=NS(stop=stop,wall=t.wall,heavy_window=gate,a=NS(rest=NS(snapshot_envelopes=snapshots),bridge=NS(needs=lambda **kw:needs),barrier=NS(persisted_R=set())),c=NS(desired_bar=set()),planner=planner)
  state={'ready':True,'scan_ok':False,'history':{'features':{}}};diag=ScanDiagnostics()
  env=dict(asyncio=asyncio,time=NS(monotonic=t.clock),r=r,p=NS(_decision_lock=threading.RLock()),state=state,
    bundle={'market':{'open':OP,'close':OP+timedelta(minutes=390)},'session':'2026-10-28','calendar':[],'eligible':['X']},
    dt=lambda x:x,SessionVolumes=Volumes,api=api,news=NS(refresh=lambda x:None),floats=None,
    selection=NS(update=lambda *a,**kw:[]),save=lambda *a:None,redis=None,scan_diagnostics=diag,pause=pause,
    reason=lambda e:str(e),json=json)
  output=io.StringIO()
  with contextlib.redirect_stdout(output):asyncio.run(nested('scan',env)())
  return t,gate,diag,state,planner,[json.loads(x) for x in output.getvalue().splitlines()],sent
 def test_real_scan_crosses_55_through_10_no_gate_delay(self):
  t,g,d,state,planner,rows,sent=self.scan_once()
  self.assertEqual(sent,[54.,55.,80.]);self.assertEqual(t.at,107.)
  self.assertEqual(g.metrics()['wait_seconds_by_operation']['scan_rest'],0.)
  self.assertTrue(state['scan_ok']);self.assertEqual(planner.propose.call_count,1)
  self.assertEqual(d.snapshot(),{'last_success_duration_seconds':53.,'last_failure_duration_seconds':None,'last_watermark_age_seconds':53.})
  self.assertEqual(rows[-1]['stage'],'IPR_SCAN');self.assertEqual(rows[-1]['duration_seconds'],53.)
 def test_old_gate_same_scan_reproduces_stale_watermark(self):
  t,g,d,state,planner,rows,sent=self.scan_once(legacy=True)
  self.assertEqual(t.at,122.);self.assertEqual(sent,[54.,70.,95.])
  self.assertFalse(state['scan_ok']);planner.propose.assert_not_called()
  self.assertEqual(rows[-1]['reason'],'RTH_VOLUME_STALE')
  self.assertEqual(d.snapshot()['last_failure_duration_seconds'],68.)
  self.assertEqual(d.snapshot()['last_watermark_age_seconds'],68.)
  self.assertAlmostEqual(g.metrics()['wait_seconds_by_operation']['scan_rest'],15.)
 def test_over_sixty_without_gate_still_fails_closed(self):
  _,g,d,state,planner,rows,_=self.scan_once(durations=(1.,30.,30.))
  self.assertFalse(state['scan_ok']);planner.propose.assert_not_called()
  self.assertEqual(rows[-1]['reason'],'RTH_VOLUME_STALE');self.assertEqual(d.snapshot()['last_watermark_age_seconds'],61.)
  self.assertEqual(g.metrics()['wait_seconds_by_operation']['scan_rest'],0.)
 def test_exact_sixty_still_allowed(self):
  _,_,d,state,_,rows,_=self.scan_once(durations=(1.,30.,29.))
  self.assertTrue(state['scan_ok']);self.assertEqual(d.snapshot()['last_watermark_age_seconds'],60.)
 def test_runtime_metrics_exports_actual_counters(self):
  t,g,d,_,_,_,_=self.scan_once();stop=NS(value=False);stop.is_set=lambda:stop.value
  r=NS(stop=stop,wall=t.wall,ingress=None,a=NS(transport_trusted=False),heavy_window=g)
  async def pause(*a):stop.value=True
  env=dict(r=r,measure=NS(sample=lambda:{}),state={'ready':True,'scan_ok':True},selection=NS(metrics=lambda:{}),scan_diagnostics=d,json=json,pause=pause)
  out=io.StringIO()
  with contextlib.redirect_stdout(out):asyncio.run(nested('metrics',env)())
  row=json.loads(out.getvalue());self.assertEqual(row['scan']['last_success_duration_seconds'],53.)
  self.assertEqual(row['heavy_window']['wait_seconds_by_operation']['incremental'],0.)
  self.assertEqual(row['heavy_window']['wait_seconds_by_operation']['scan_rest'],0.)

class R71Gate(unittest.TestCase):
 def test_scan_starts_inside_window_without_wait_or_request_denial(self):
  for at in (0.,9.999,55.,59.999):
   t=Sim(at);g=HeavyWindow(t.wall,sleep=t.sleep,clock=t.clock)
   with g.operation('scan_rest'):g.before_request();self.assertTrue(g.request_allowed());self.assertTrue(g.snapshot()['scan_rest'])
   self.assertEqual(t.at,at);self.assertFalse(t.sleeps);self.assertEqual(g.metrics()['wait_seconds_by_operation']['scan_rest'],0.)
  g.stop=lambda:True
  with self.assertRaisesRegex(RuntimeError,'HEAVY_WORK_STOPPED'):
   with g.operation('scan_rest'):self.fail('must not start stopped scan')
  g.stop=lambda:False
  with g.operation('scan_rest'):
   g.stop=lambda:True
   with self.assertRaisesRegex(RuntimeError,'HEAVY_WORK_STOPPED'):g.before_request()
 def test_incremental_waits_at_start_and_between_pages_with_totals(self):
  t=Sim(55.);g=HeavyWindow(t.wall,sleep=t.sleep,clock=t.clock)
  with g.operation('incremental'):
   self.assertEqual(t.at,70.);t.at=115.;self.assertFalse(g.request_allowed());g.before_request();self.assertEqual(t.at,130.);self.assertTrue(g.request_allowed())
  self.assertEqual(g.metrics()['wait_seconds_by_operation']['incremental'],30.)
 def test_wait_totals_include_interrupted_gate_wait(self):
  t=Sim(55.);g=HeavyWindow(t.wall,stop=lambda:t.at>=56.,sleep=t.sleep,clock=t.clock)
  with self.assertRaisesRegex(RuntimeError,'HEAVY_WORK_STOPPED'):
   with g.operation('incremental'):self.fail('must not enter')
  self.assertEqual(g.metrics()['wait_seconds_by_operation']['incremental'],1.)
 def test_diagnostics_keep_last_success_failure_and_checked_age(self):
  d=ScanDiagnostics();self.assertTrue(all(x is None for x in d.snapshot().values()))
  d.finish(True,53.);d.finish(False,68.);d.watermark(OP+timedelta(seconds=68),OP)
  self.assertEqual(d.snapshot(),{'last_success_duration_seconds':53.,'last_failure_duration_seconds':68.,'last_watermark_age_seconds':68.})
  d.finish(False,2.) # e.g. network failure before reaching a new watermark check
  self.assertEqual(d.snapshot()['last_watermark_age_seconds'],68.)
  self.assertEqual(d.snapshot()['last_success_duration_seconds'],53.)
