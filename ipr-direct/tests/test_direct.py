import unittest,json,tempfile,copy,time
from datetime import datetime,timezone,timedelta,date
from pathlib import Path
from types import SimpleNamespace as NS
from ipr.ranking import base_score,float_points,Selection
from ipr.data import rth,SessionVolumes,prepare_history,dt
from ipr.news_batch import BatchNews
from ipr.classifier import Classifier
from ipr.api import Alpaca,APIError
from ipr.preparation import save,load,carried
from ipr.sender import Ledger,Sender
from lua_redis import LuaRedis
from r4a.redis_ports import leadership
from r4a.durable import PREFIX
from dintegration.persistence import NamespacedRedis
from state_store import key_trade,key_outbox,base_record,canonical_json
from redis_lua_production import ProductionRedisLua
from active_trade_recovery import ActiveTradeChronologicalReconciler
OP=dt('2026-10-28T13:30:00Z');NOW=OP+timedelta(minutes=30)
CAL=[{'session':'2026-10-28','open':OP.isoformat(),'close':(OP+timedelta(minutes=390)).isoformat()}]
def bar(t,v=100):return {'t':t.isoformat(),'o':10.,'h':11.,'l':9.,'c':10.,'v':v}
class API:
 requests=1
 def __init__(self,pages):self.pages=pages
 def bar_pages(self,*a):
  for p in self.pages:
   if isinstance(p,Exception):raise p
   yield p
class Scores(unittest.TestCase):
 def score(self,ratio):
  return base_score('X',{'latestTrade':{'p':10,'t':NOW.isoformat()}},{'adv20':3900,'high':10,'low':9,'close':10},{'volume':300*ratio,'open':10},NOW,OP,NS(value=lambda s:{'status':'UNKNOWN'}))
 def test_log_activity_exact(self):
  for ratio,expected in [(1,0),(2,10),(4,20),(8,30),(16,30)]:self.assertAlmostEqual(self.score(ratio)['parts']['activity'],expected)
 def test_float_boundaries(self):
  for f,pts in [(1,20),(10e6,20),(10e6+1,15),(20e6,15),(30e6,10),(50e6,5),(50e6+1,0)]:self.assertEqual(float_points({'status':'VALID','float':f}),pts)
 def test_float_unknown(self):self.assertEqual(float_points({'status':'UNKNOWN'}),0)
 def test_zero_score_allowed(self):
  self.assertIsNotNone(self.score(0))
 def test_no_future_stale_or_outside_price(self):
  for p,t in [(0.99,NOW),(50.01,NOW),(10,NOW-timedelta(seconds=61)),(10,NOW+timedelta(seconds=1))]:
   self.assertIsNone(base_score('X',{'latestTrade':{'p':p,'t':t.isoformat()}},{'adv20':3900},{'volume':100,'open':10},NOW,OP,NS(value=lambda s:{})))
 def test_exact_price_bounds(self):
  for p in (1,50):self.assertIsNotNone(base_score('X',{'latestTrade':{'p':p,'t':NOW.isoformat()}},{'adv20':3900,'high':50,'low':1,'close':10},{'volume':100,'open':10},NOW,OP,NS(value=lambda s:{})))
 def test_pin_priority_and_500(self):
  sel=Selection();scores={str(i):{'score':100} for i in range(800)};self.assertIn('PIN',sel.update(scores,{'PIN'},1));self.assertEqual(len(sel.current),500)
 def test_no_pin_dropped_without_snapshot(self):
  sel=Selection();self.assertEqual(sel.update({}, {'A','O','U','R'},1),['A','O','R','U'])
 def test_501_pins_explicit_conflict(self):
  with self.assertRaisesRegex(ValueError,'NO_DROP'):Selection().update({},set(map(str,range(501))),1)
 def test_replacement_exact_five_and_alphabetical(self):
  s=Selection();s.current={'old'};pins={str(i) for i in range(499)}
  out=s.update({'old':{'score':10},'B':{'score':15},'A':{'score':15}},pins,1);self.assertIn('A',out);self.assertNotIn('old',out)
 def test_update_clock_120(self):
  s=Selection();s.current={'old'};s.last=1;pins=set(map(str,range(499)))
  self.assertIn('old',s.update({'old':{'score':0},'new':{'score':100}},pins,120));self.assertIn('new',s.update({'old':{'score':0},'new':{'score':100}},pins,121))
class Volumes(unittest.TestCase):
 def test_only_RTH_completed(self):
  cal={c['session']:c for c in CAL}
  for t in [OP-timedelta(minutes=1),OP+timedelta(minutes=390),NOW]:self.assertIsNone(rth(bar(t),cal,NOW))
  self.assertEqual(rth(bar(OP),cal,NOW),'2026-10-28')
 def test_replayed_and_revised_minute_not_double_counted(self):
  with tempfile.TemporaryDirectory() as td:
   v=SessionVolumes(td+'/v.sqlite',CAL,'2026-10-28');x=v.update(API([{'X':[bar(OP),bar(OP+timedelta(minutes=1))]}]),['X'],NOW);self.assertEqual(x['X']['volume'],200)
   x=v.update(API([{'X':[bar(OP,150),bar(OP+timedelta(minutes=1))]}]),['X'],NOW);self.assertEqual(x['X']['volume'],250)
 def test_failed_page_rolls_back(self):
  with tempfile.TemporaryDirectory() as td:
   v=SessionVolumes(td+'/v.sqlite',CAL,'2026-10-28')
   with self.assertRaises(ConnectionError):v.update(API([{'X':[bar(OP)]},ConnectionError()]),['X'],NOW)
   self.assertIsNone(v.watermark);self.assertEqual(v.db.execute('SELECT COUNT(*) FROM bars').fetchone()[0],0)
 def test_bad_ohlcv_rejected(self):
  b=bar(OP);b['v']=-1
  with self.assertRaises(ValueError):rth(b,{c['session']:c for c in CAL},NOW)
 def test_history_20_regular_sums_not_daily_snapshot(self):
  dates=[date(2026,9,1)+timedelta(days=i) for i in range(20)];cal=[];bars=[]
  for d in dates:
   op=dt(d.isoformat()+'T13:30:00Z');cal.append({'session':d.isoformat(),'open':op.isoformat(),'close':(op+timedelta(minutes=390)).isoformat()});bars.extend([bar(op-timedelta(minutes=1),9999),bar(op,100)])
  h=prepare_history(API([{'X':bars}]),['X'],cal,'2026-10-28',NOW)
  self.assertEqual(h['features']['X']['adv20'],100)
 def test_missing_history_symbol_not_filled(self):
  cal=[]
  for i in range(20):
   op=OP-timedelta(days=20-i);cal.append({'session':op.date().isoformat(),'open':op.isoformat(),'close':(op+timedelta(minutes=390)).isoformat()})
  self.assertEqual(prepare_history(API([]),['X'],cal,'2026-10-28',NOW)['features'],{})
class News(unittest.TestCase):
 def reader(self):
  calls=[];clock=[10000.]
  def post(url,**kw):
   cmd=kw['json'];calls.append(cmd)
   if cmd[0]=='HLEN':value=30
   else:value=[json.dumps({'checked_at':clock[0]-10,'producer':'elite_catalyst_radar','analysis':{'positive':True,'bonus':5},'articles':[]}) for s in cmd[2:]]
   return NS(status_code=200,json=lambda:{'result':value})
  return BatchNews({'UPSTASH_REDIS_REST_URL':'https://fixture.invalid','UPSTASH_REDIS_REST_TOKEN':'fixture'},post,clock=lambda:clock[0]),calls,clock
 def test_750_three_batches_no_HGET_no_write(self):
  n,c,t=self.reader();n.refresh(list(map(str,range(900))));self.assertEqual(len(c),3);self.assertTrue(all(x[:2]==['HMGET','market_radar:news'] and len(x)==252 for x in c));self.assertTrue(n.read('1')['positive'])
 def test_cache_strictly_less_than_60(self):
  n,c,t=self.reader();n.refresh(['X']);n.refresh(['X']);self.assertEqual(len(c),1);t[0]+=60;self.assertIsNone(n.raw('X'));n.refresh(['X']);self.assertEqual(len(c),2)
 def test_count_read_only(self):
  n,c,t=self.reader();self.assertEqual(n.count(),30);self.assertEqual(c,[['HLEN','market_radar:news']])
 def test_missing_fails_open_zero(self):self.assertEqual(BatchNews({},lambda *a,**k:None).read('X')['bonus'],0)
class Delivery(unittest.TestCase):
 def env(self):
  raw=LuaRedis();lead=leadership(raw,'w');lead.acquire();return raw,lead,Ledger(raw,lead)
 def test_same_trade_event_once_after_finish(self):
  r,l,d=self.env();key,x=d.start('trade1','ENTRY','a');self.assertEqual(x,'ALLOW');d.finish(key,'a','SENT');self.assertEqual(d.start('trade1','ENTRY','b')[1],'SENT')
 def test_same_symbol_different_trade_not_suppressed(self):
  r,l,d=self.env();self.assertEqual(d.start('trade1','T1','a')[1],'ALLOW');self.assertEqual(d.start('trade2','T1','b')[1],'ALLOW')
 def test_started_ambiguous_no_retry(self):
  r,l,d=self.env();d.start('trade','STOP','a');self.assertEqual(d.start('trade','STOP','b')[1],'UNKNOWN_FINAL')
 def test_old_leader_no_reservation(self):
  r,l,d=self.env();r.set(PREFIX+':canonical:operational_priority_radar:v1:runtime:leader','other')
  with self.assertRaises(Exception):d.start('trade','ENTRY','a')
 def test_original_recovery_T1_STOP_real_outbox_to_telegram(self):
  raw,lead,ledger=self.env();r=NamespacedRedis(raw,PREFIX+':canonical:');trade=base_record('trade','2026-10-28','X','ACTIVE_PRE_T1',OP.isoformat())
  trade.update(trade_id='t',entry_alert_price=10.,structure_low=9.6,structural_stop=9.5,risk_pct=.05,t1=10.5,t2=11.,monitoring_deadline=(OP+timedelta(hours=3)).isoformat(),leader_generation=lead.require_current().leader_generation,worker_instance_id='w')
  r.set(key_trade('t'),canonical_json(trade));src=NS(trades=lambda *a:[{'t':(OP+timedelta(seconds=1)).isoformat(),'p':10.6},{'t':(OP+timedelta(seconds=2)).isoformat(),'p':9.4}],native_1m_gap=lambda *a:[])
  rec=ActiveTradeChronologicalReconciler(src,r,'w',lambda:NOW);rec.reconcile(trade)
  self.assertEqual(json.loads(r.get(key_trade('t')))['state'],'CLOSED_STOP')
  runtime=NS(a=NS(bridge=NS(r=r),checks=lambda:None,transport_trusted=True,recovering=set(),trusted_since=OP),check_session=lambda:None,leader=lead,c=NS(bar={'X'},proofs={'X':dict(history=True,continuity=True,reconciled=True)}))
  messages=[];sender=Sender(runtime,raw,{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture'},NS(post=lambda text:messages.append(text) or 'SENT'))
  sender.step();sender.step();self.assertEqual(len(messages),2);self.assertTrue(any('T1' in x for x in messages));self.assertTrue(any('الوقف' in x for x in messages))
class Contracts(unittest.TestCase):
 def test_classifier_original_rejections(self):
  def a(name):return {'name':name,'exchange':'NASDAQ','status':'active','tradable':True,'id':'fixture'}
  c=Classifier({'ABC':a('ABC Common Stock'),'ADR':a('ABC American Depositary Shares'),'ETF':a('ABC ETF')});self.assertTrue(c.eligible('ABC'));self.assertFalse(c.eligible('ADR'));self.assertFalse(c.eligible('ETF'))
 def test_api_no_order_endpoint(self):
  api=Alpaca({'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'})
  with self.assertRaisesRegex(APIError,'FORBIDDEN'):api.call('paper-api.alpaca.markets','/v2/orders',{})
 def test_cached_prep_hash_verified(self):
  raw=LuaRedis();save(raw,'x',{'a':1});self.assertEqual(load(raw,'x'),{'a':1})
 def test_main_missing_env_before_connection(self):
  from ipr.main import credentials
  with self.assertRaises(ValueError):credentials({})
class MoreIntegration(unittest.TestCase):
 def test_composed_real_original_pipeline_no_manual_files(self):
  from unittest.mock import patch
  from ipr.runtime import build
  now=datetime.now(timezone.utc);day=now.date().isoformat();raw=LuaRedis(now=now.timestamp())
  op=now.replace(hour=13,minute=30,second=0,microsecond=0)
  bundle={'session':day,'assets':{'X':{'symbol':'X','name':'X Common Stock','exchange':'NASDAQ','status':'active','tradable':True,'id':'fixture'}},'symbols':['X'],'eligible':['X'],'calendar':[],'market':{'open':op.isoformat(),'close':(op+timedelta(minutes=390)).isoformat()},'new_entries_prepared':True}
  fake_sample=lambda:{'limit_bytes':512*1024**2,'current_bytes':10*1024**2,'anon_bytes':10*1024**2}
  with patch('ipr.runtime.cgroup_sample',fake_sample):r,state,clock,measure=build(bundle,NS(get=lambda *a:None),raw,{'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'},'boot')
  self.assertFalse(r.a.transport_trusted);self.assertFalse(state['ready']);self.assertEqual(r.boot_number,1);self.assertFalse(r.a.bridge.p.shadow)
  self.assertEqual(r.a.bridge.p.ec.required_history_minutes(),305)
 def test_stream_buffer_rolls_per_symbol_not_global_invalid(self):
  from dintegration.stream_proof import StreamProof
  p=StreamProof();p.connected(started=OP);p.set_scope({'X','Y'});rows=[]
  for i in range(20):
   b=bar(OP+timedelta(minutes=i));rows.append(b);p.observe(dict(b,T='b',S='X'),OP+timedelta(minutes=i+1))
  p.observe(dict(bar(OP+timedelta(minutes=19)),T='b',S='Y'),OP+timedelta(minutes=20))
  self.assertTrue(p.valid);self.assertEqual(len(p.rows['X']),8);self.assertEqual(len(p.rows['Y']),1)
  self.assertTrue(p.evidence('X',rows,OP+timedelta(minutes=20))['proven'])
  p.connected(started=NOW);self.assertFalse(p.evidence('X',rows,NOW)['proven'])
 def test_monotonic_ACK(self):
  from dreview.core import Root,Controller,Needs
  from r4a.clocked import ClockedControl
  c=Controller(Root('2026-10-28',frozenset({'X'}),'a'*64,True));c.reconnect();c.set_desired({'X'},Needs(1,bar=frozenset({'X'})))
  n=[1.];ctl=ClockedControl(c,lambda:n[0]);self.assertIsNotNone(ctl.plan());n[0]=2.;self.assertTrue(ctl.ack(c.connection,['X'],[]));ctl.tick();self.assertFalse(c.failed)
 def test_rolling_history_reuses_20_known_sessions_without_fetch(self):
  cal=[];prev={'X':{}}
  for i in range(20):
   op=OP-timedelta(days=20-i);day=op.date().isoformat();cal.append({'session':day,'open':op.isoformat(),'close':(op+timedelta(minutes=390)).isoformat()});prev['X'][day]={'v':100,'h':11,'l':9,'c':10,'last':op.isoformat()}
  class NoCalls:
   def bar_pages(self,*a):raise AssertionError('REFETCH')
  out=prepare_history(NoCalls(),['X'],cal,'2026-10-28',NOW,previous=prev);self.assertEqual(out['features']['X']['adv20'],100)
class ParentAndLifecycle(unittest.TestCase):
 def test_prepare_task_does_not_terminate_runtime_when_ready(self):
  # Actual scheduling function with fake runtime/data/HTTP; no market/socket execution.
  import asyncio
  from unittest.mock import patch
  from ipr import runtime as rt
  async def go():
   raw=LuaRedis();stopped=asyncio.Event();flags={'ready':False,'history':None,'scan_ok':False}
   p=NS(expire_entry_opportunities=lambda now:None)
   ended=[]
   async def life():await asyncio.sleep(.02);ended.append(True);stopped.set()
   r=NS(a=NS(bridge=NS(p=p),transport_trusted=False),boot_number=1,stop=stopped,ingress=None,wall=lambda:NOW,notify_cases_changed=lambda:None,revoke=lambda reason:None,run=life)
   class FakeSender:
    def __init__(self,*a):pass
    async def run(self):await stopped.wait()
   class M:
    def sample(self):return {}
    async def loop_lag(self,stop):await stop.wait()
   bundle={'session':'2026-10-28','eligible':[],'new_entries_prepared':True,'market':None,'calendar':[]}
   with patch.object(rt,'build',return_value=(r,flags,NS(refresh=lambda:True),M())),patch.object(rt,'Sender',FakeSender),patch.object(rt,'prepare_history',return_value={'session':'2026-10-28','features':{},'daily_aggregates':{}}),patch.object(rt.requests,'get',side_effect=AssertionError('NO_HTTP')),patch.object(rt.requests,'post',side_effect=AssertionError('NO_HTTP')):
    loop=asyncio.get_running_loop()
    with patch.object(loop,'add_signal_handler'):await rt.run(bundle,None,raw,{},'boot')
   self.assertTrue(stopped.is_set());self.assertEqual(ended,[True]);self.assertTrue(flags['ready'])
  asyncio.run(go())
 def test_preparation_cache_fails_corruption(self):
  raw=LuaRedis();raw.set(PREFIX+':prepared:x','not-base64')
  with self.assertRaises(Exception):load(raw,'x')
class AutomaticPreparation(unittest.TestCase):
 def test_closed_day_does_not_fetch_assets_and_calendar_cached(self):
  from ipr.preparation import prepare_root
  raw=LuaRedis();calls=[]
  class A:
   def calendar(self,*a):calls.append('calendar');return []
   def assets(self):raise AssertionError('CLOSED_DAY_ASSETS')
  self.assertIsNone(prepare_root(A(),raw,NOW));self.assertIsNone(prepare_root(A(),raw,NOW));self.assertEqual(calls,['calendar'])
 def test_root_from_actual_assets_saved_once_per_day(self):
  from ipr.preparation import prepare_root
  raw=LuaRedis();calls=[]
  class A:
   def calendar(self,*a):return CAL
   def assets(self):calls.append('assets');return {'X':{'name':'X Common Stock','exchange':'NASDAQ','status':'active','tradable':True,'class':'us_equity','id':'fixture'}}
  x=prepare_root(A(),raw,OP-timedelta(hours=3));y=prepare_root(A(),raw,NOW)
  self.assertEqual(x,y);self.assertEqual(calls,['assets']);self.assertTrue(x['new_entries_prepared']);self.assertEqual(x['symbols'],['X'])
 def test_no_scope_from_failed_assets_with_no_cases(self):
  from ipr.preparation import prepare_root
  raw=LuaRedis()
  class A:
   def calendar(self,*a):return CAL
   def assets(self):raise ConnectionError()
  self.assertIsNone(prepare_root(A(),raw,NOW))
 def test_historical_missing_session_not_synthesized(self):
  # Missing prior RTH bars remains ineligible; no dailyBar volume fallback.
  self.assertIsNone(base_score('X',{'latestTrade':{'p':10,'t':NOW.isoformat()}},None,{'volume':100,'open':10},NOW,OP,NS(value=lambda s:{})))
class CommittedFollowups(unittest.TestCase):
 def test_committed_T1_is_delivered_even_while_stream_recovers(self):
  raw=LuaRedis();lead=leadership(raw,'w');lead.acquire();r=NamespacedRedis(raw,PREFIX+':canonical:')
  trade=base_record('trade','2026-10-28','X','ACTIVE_POST_T1',OP.isoformat());trade.update(trade_id='t',entry_alert_price=10.,structure_low=9.5,structural_stop=9.5,risk_pct=5,t1=10.5,t2=11.,monitoring_deadline=(NOW+timedelta(hours=2)).isoformat(),leader_generation=1,worker_instance_id='w');r.set(key_trade('t'),canonical_json(trade))
  event=base_record('outbox','2026-10-28','X','PENDING',OP.isoformat());event.update(event_id='T1:t',event_type='T1',payload={'trade_id':'t','event':'T1','event_ts':OP.isoformat()},attempt_count=0,leader_generation=1);r.set(key_outbox('T1:t'),canonical_json(event))
  rt=NS(a=NS(bridge=NS(r=r),checks=lambda:None,transport_trusted=False,recovering={'X'},trusted_since=None),leader=lead,check_session=lambda:None,c=NS(bar=set(),proofs={}))
  sent=[];s=Sender(rt,raw,{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture'},NS(post=lambda t:sent.append(t) or 'SENT'));s.step();s.step();self.assertEqual(len(sent),1)
