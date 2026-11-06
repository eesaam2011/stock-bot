"""R9.1: execute the shipped nested scan, with synthetic I/O and clock only."""
import asyncio,contextlib,io,json,threading,unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock
import test_r71 as prior
from test_direct import OP
from ipr.data import dt
from ipr.ranking import base_score
from ipr.wave import HeavyWindow,ScanDiagnostics
from ipr.scan_health import SCAN_VALIDITY_SECONDS

class R91Watermark(unittest.TestCase):
 def actual_scan(self,watermark_age,price_age=0,news_delay=0):
  t=prior.Sim();gate=HeavyWindow(t.wall,sleep=t.sleep,clock=t.clock)
  class Volumes:
   def __init__(self,*args):self.watermark=None
   def update(self,api,symbols,now):
    self.watermark=now;t.sleep(watermark_age-news_delay)
    return {'X':{'volume':100,'open':2.}}
  def snapshots(*args):
   return {'X':{'data':{'latestTrade':{'p':2.,'t':(t.wall()-timedelta(seconds=price_age)).isoformat()}}}}
  stop=NS(value=False);stop.is_set=lambda:stop.value
  async def pause(*args):stop.value=True
  needs=NS(pins=set());planner=Mock();saved=[];diagnostics=ScanDiagnostics()
  runtime=NS(stop=stop,wall=t.wall,heavy_window=gate,
    a=NS(rest=NS(snapshot_envelopes=snapshots),bridge=NS(needs=lambda **kw:needs),barrier=NS(persisted_R=set())),
    c=NS(desired_bar=set()),planner=planner)
  state={'ready':True,'scan_ok':False,'history':{'features':{'X':{'adv20':1000.,'high':2.1,'low':1.9,'close':2.}}}}
  selected=[]
  def choose(scores,*args,**kw):selected[:]=sorted(scores);return list(selected)
  env=dict(asyncio=asyncio,time=NS(monotonic=t.clock),r=runtime,p=NS(_decision_lock=threading.RLock()),state=state,
   bundle={'market':{'open':OP,'close':OP+timedelta(minutes=390)},'session':'2026-11-05','calendar':[],'eligible':['X']},
   dt=dt,SessionVolumes=Volumes,api=NS(requests=0),news=NS(refresh=lambda symbols:t.sleep(news_delay),read=lambda symbol:{}),
   floats=NS(value=lambda symbol:{'status':'UNKNOWN'}),base_score=base_score,
   selection=NS(update=choose),save=lambda *args:saved.append(args),redis=None,scan_diagnostics=diagnostics,
   pause=pause,reason=lambda exc:str(exc),json=json)
  capture=io.StringIO()
  with contextlib.redirect_stdout(capture):asyncio.run(prior.nested('scan',env)())
  return state,planner,selected,saved,diagnostics.snapshot(),[json.loads(line)for line in capture.getvalue().splitlines()]
 def test_watermark100_succeeds_and_selects_fresh_symbol(self):
  state,planner,selected,saved,diag,rows=self.actual_scan(100)
  self.assertTrue(state['scan_ok']);self.assertEqual(selected,['X']);self.assertEqual(len(saved),1)
  self.assertEqual(planner.propose.call_count,1);self.assertEqual(diag['last_watermark_age_seconds'],100)
  self.assertEqual(rows[-1]['stage'],'IPR_SCAN')
 def test_watermark130_fails_before_planning_or_persistence(self):
  state,planner,selected,saved,diag,rows=self.actual_scan(130)
  self.assertFalse(state['scan_ok']);planner.propose.assert_not_called();self.assertEqual(saved,[])
  self.assertEqual(rows[-1]['reason'],'RTH_VOLUME_STALE');self.assertEqual(diag['last_watermark_age_seconds'],130)
 def test_watermark_exact120_succeeds(self):
  state,_,selected,_,diag,rows=self.actual_scan(120)
  self.assertTrue(state['scan_ok']);self.assertEqual(selected,['X']);self.assertEqual(diag['last_watermark_age_seconds'],120)
 def test_watermark120_plus_fraction_fails(self):
  state,planner,_,saved,_,rows=self.actual_scan(120.000001)
  self.assertFalse(state['scan_ok']);planner.propose.assert_not_called();self.assertEqual(saved,[])
  self.assertEqual(rows[-1]['reason'],'RTH_VOLUME_STALE')
 def test_price_exact60_still_selected(self):
  state,_,selected,_,_,_=self.actual_scan(100,price_age=60)
  self.assertTrue(state['scan_ok']);self.assertEqual(selected,['X'])
 def test_price61_rejected_by_original_initial_price_freshness(self):
  state,_,selected,_,_,rows=self.actual_scan(100,price_age=61)
  self.assertTrue(state['scan_ok']);self.assertEqual(selected,[])
  exclusion=next(row for row in rows if row.get('stage')=='SCAN_EXCLUSIONS')
  self.assertEqual(exclusion['counts']['LAST_TRADE_STALE'],1)
 def test_news_delay_makes_price61_rejected_by_final_snapshot_check(self):
  state,_,selected,_,diag,rows=self.actual_scan(100,price_age=59,news_delay=2)
  self.assertTrue(state['scan_ok']);self.assertEqual(selected,[]);self.assertEqual(diag['last_watermark_age_seconds'],100)
  exclusion=next(row for row in rows if row.get('stage')=='SCAN_EXCLUSIONS')
  self.assertEqual(exclusion['counts']['SNAPSHOT_AGE_GT60'],1)
 def test_scan_validity_stays150(self):self.assertEqual(SCAN_VALIDITY_SECONDS,150)
