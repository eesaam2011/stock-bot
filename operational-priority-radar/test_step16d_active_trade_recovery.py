import unittest,json
from types import SimpleNamespace
from datetime import datetime,timedelta,timezone
from active_trade_recovery import ActiveTradeChronologicalReconciler
from redis_lua_production import ACQUIRE_LEASE_LUA,ATOMIC_TRADE_EVENT_LUA,ATOMIC_TRADE_RECOVERY_LUA
from state_store import canonical_json,key_trade
UTC=timezone.utc;T=datetime(2026,1,1,15,tzinfo=UTC)
class Redis:
 def __init__(self):self.d={};self.g=1;self.d['operational_priority_radar:v1:runtime:leader']='W';self.d['operational_priority_radar:v1:runtime:leader_generation']='1'
 def get(self,k):return self.d.get(k)
 def eval(self,script,n,*a):
  if script==ATOMIC_TRADE_RECOVERY_LUA:
   keys=a[:n];v=a[n:];leader,gen,tk=keys[:3];outkeys=keys[3:]
   wid,expected,new,g=v[:4];outs=v[4:]
   if self.d.get(leader)!=wid:return -10
   if self.d.get(gen)!=g:return -11
   if self.d.get(tk)!=expected:return -20
   if len(outkeys)!=len(outs) or not 1<=len(outs)<=4:return -40
   if len(set(outkeys))!=len(outkeys):return -50
   if any(self.d.get(k) not in (None,raw) for k,raw in zip(outkeys,outs)):return -30
   self.d[tk]=new
   for k,raw in zip(outkeys,outs):self.d[k]=raw
   return 1
  if script==ATOMIC_TRADE_EVENT_LUA:
   keys=a[:n];v=a[n:];leader,gen,tk,ok=keys;wid,expected,new,out,g=v
   if self.d.get(leader)!=wid:return -10
   if self.d.get(gen)!=g:return -11
   if self.d.get(tk)!=expected:return -20
   if self.d.get(ok) not in (None,out):return -30
   self.d[tk]=new;self.d[ok]=out;return 1
  raise AssertionError
class REST:
 def __init__(self,trades=None,bars=None):self._t=trades or [];self._b=bars or []
 def trades(self,*a):return self._t
 def native_1m_gap(self,*a):return self._b
def trade(state='ACTIVE_PRE_T1'):
 return {'schema_version':1,'record_type':'trade','session':'S','symbol':'A','state':state,'created_at':T.isoformat(),'updated_at':T.isoformat(),'trade_id':'TR','entry_alert_price':10.,'structure_low':9.5,'structural_stop':9.,'risk_pct':10.,'t1':11.,'t2':12.,'monitoring_deadline':(T+timedelta(minutes=120)).isoformat(),'leader_generation':1,'worker_instance_id':'W'}
def setup(t,r):
 rd=Redis();rd.d[key_trade('TR')]=canonical_json(t)
 leader=SimpleNamespace(require_current=lambda:SimpleNamespace(worker_instance_id='W',leader_generation=1))
 return ActiveTradeChronologicalReconciler(r,rd,'W',lambda:T+timedelta(minutes=30),leadership=leader),rd
class TestStep16D(unittest.TestCase):
 def test_t1_then_t2_chronology_committed(self):
  t=trade();rec,rd=setup(t,REST([{'t':(T+timedelta(minutes=1)).isoformat(),'p':11.1},{'t':(T+timedelta(minutes=2)).isoformat(),'p':12.1}]))
  z=rec.reconcile(t);self.assertEqual(z['state'],'CLOSED_T2');self.assertEqual(json.loads(rd.d[key_trade('TR')])['state'],'CLOSED_T2')
 def test_stop_before_t1_wins(self):
  t=trade();rec,_=setup(t,REST([{'t':(T+timedelta(seconds=1)).isoformat(),'p':8.9},{'t':(T+timedelta(seconds=2)).isoformat(),'p':12.5}]))
  self.assertEqual(rec.reconcile(t)['state'],'CLOSED_STOP')
 def test_post_t1_structural_stop_still_active(self):
  t=trade('ACTIVE_POST_T1');rec,_=setup(t,REST([{'t':(T+timedelta(seconds=1)).isoformat(),'p':8.8}]))
  self.assertEqual(rec.reconcile(t)['state'],'CLOSED_STOP')
 def test_post_t1_bar_close_breakeven(self):
  t=trade('ACTIVE_POST_T1');bar_start=T+timedelta(minutes=1);rec,_=setup(t,REST([], [{'t':bar_start.isoformat(),'c':9.9}]))
  self.assertEqual(rec.reconcile(t)['state'],'CLOSED_POST_T1_BREAKEVEN_EXIT')
 def test_equal_timestamp_t2_vs_stop_is_ambiguous(self):
  t=trade();ts=(T+timedelta(seconds=1)).isoformat();rec,_=setup(t,REST([{'t':ts,'p':12.1},{'t':ts,'p':8.9}]))
  self.assertTrue(rec.reconcile(t)['ambiguous'])
 def test_halted_active_requires_status_reconciliation(self):
  t=trade('HALTED_ACTIVE');t['pre_halt_state']='ACTIVE_PRE_T1';rec,_=setup(t,REST())
  self.assertTrue(rec.reconcile(t)['ambiguous'])
 def test_monitoring_expiry_committed_without_market_event(self):
  t=trade();rec,rd=setup(t,REST());rec.now_fn=lambda:T+timedelta(minutes=121)
  self.assertEqual(rec.reconcile(t)['state'],'MONITORING_EXPIRED')
 def test_stop_outbox_records_observed_breach_not_fill(self):
  t=trade();rec,rd=setup(t,REST([{'t':(T+timedelta(seconds=1)).isoformat(),'p':8.7}]))
  rec.reconcile(t);outs=[json.loads(v) for k,v in rd.d.items() if ':outbox:' in k]
  self.assertEqual(outs[0]['payload']['first_observed_breach_price'],8.7);self.assertNotIn('fill_price',outs[0]['payload'])
if __name__=='__main__':unittest.main()
