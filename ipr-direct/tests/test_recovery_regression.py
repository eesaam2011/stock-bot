"""Accepted B recovery regression cases, run against direct namespace/bridge."""
import unittest,copy,json
from datetime import timedelta
from types import SimpleNamespace
from test_direct import OP,NOW,bar
from lua_redis import LuaRedis
from r4a.redis_ports import leadership
from r4a.durable import PREFIX
from dintegration.persistence import NamespacedRedis
from dintegration.bridge import OriginalBridge
from redis_lua_production import ProductionRedisLua
from state_store import key_trade,key_outbox,key_opportunity,canonical_json,base_record
from r4b.cases import CaseBarrier
from dintegration.stream_proof import StreamProof
class CaseFixture:
 def trade(self,session='2026-10-28'):
  r=base_record('trade',session,'X','ACTIVE_PRE_T1',(OP-timedelta(minutes=5)).isoformat())
  r.update(trade_id='t',entry_alert_price=10,structure_low=9.5,structural_stop=9.5,risk_pct=5,t1=10.5,t2=11,monitoring_deadline=(NOW+timedelta(hours=2)).isoformat(),leader_generation=1,worker_instance_id='w');return r
basefixtures=SimpleNamespace(CaseTests=CaseFixture)
class Source:
 def __init__(self):self.ts=[];self.bs=[];self.calls=[];self.fail=False
 def trades(self,s,start,end):
  self.calls.append(('trades',s,start,end))
  if self.fail:raise ConnectionError('fixture_source_unavailable')
  return [copy.deepcopy(x) for x in self.ts if start<=__import__('active_trade_recovery').parse_ts(x['t'])<=end]
 def native_1m_gap(self,s,start,end):
  self.calls.append(('1Min',s,start,end))
  return [copy.deepcopy(x) for x in self.bs if start<=__import__('active_trade_recovery').parse_ts(x['t'])<=end]
 def native_5m(self,s,start,end):return [dict(bar(OP-timedelta(minutes=5)),_timeframe='native_5Min')]
 def bars_multi(self,symbols,start,end,tf,**kw):return {s:self.native_1m_gap(s,start,end) for s in symbols}
 def warmup(self,symbols,now,minutes):return ({s:[bar(OP)] for s in symbols},{s:self.native_5m(s,OP,now) for s in symbols})

def fixture(symbols={'X','Y','Z'}):
 raw=LuaRedis();lead=leadership(raw,'w');lead.acquire();r=NamespacedRedis(raw,PREFIX+':canonical:');source=Source()
 bridge=OriginalBridge(r,ProductionRedisLua(r),lead,'2026-10-28',symbols,source,'w',{'start_utc':OP.isoformat(),'end_utc':(OP+timedelta(minutes=390)).isoformat()})
 proof=StreamProof();proof.connected('original_tests',OP);proof.set_scope(symbols)
 case=CaseBarrier(bridge,proof);return raw,lead,r,source,bridge,case

class OriginalCaseRouting(unittest.TestCase):
 def add(self,r,state='ACTIVE_PRE_T1'):
  t=basefixtures.CaseTests().trade();t['state']=state;r.set(key_trade('t'),canonical_json(t));return t
 def test_A_original_T1_then_STOP_and_no_duplicate(self):
  raw,l,r,src,b,c=fixture();self.add(r);src.ts=[{'t':OP.isoformat(),'p':10.6},{'t':(OP+timedelta(seconds=1)).isoformat(),'p':9.4}]
  v=c.run(NOW,OP);self.assertTrue(v['reconciled']);self.assertEqual(json.loads(r.get(key_trade('t')))['state'],'CLOSED_STOP')
  self.assertEqual(json.loads(r.get(key_outbox('T1:t')))['event_type'],'T1');self.assertEqual(json.loads(r.get(key_outbox('STOP:t')))['event_type'],'STOP')
  before=copy.deepcopy(raw.state);self.assertTrue(c.run(NOW,OP)['reconciled']);self.assertEqual(raw.state,before)
 def test_equal_timestamp_ambiguity_original_FINAL(self):
  raw,l,r,src,b,c=fixture();self.add(r);src.ts=[{'t':OP.isoformat(),'p':11.1},{'t':OP.isoformat(),'p':9.4}]
  self.assertTrue(c.run(NOW,OP)['reconciled']);self.assertEqual(json.loads(r.get(key_trade('t')))['state'],'RECOVERY_PATH_AMBIGUOUS')
  out=json.loads(r.get(key_outbox('FINAL:t')));self.assertEqual((out['event_type'],out['payload']['event']),('FINAL','RECOVERY_PATH_AMBIGUOUS'))
 def test_OHLC_both_touched_without_order_original_FINAL(self):
  raw,l,r,src,b,c=fixture();self.add(r);src.bs=[bar(OP)]
  self.assertTrue(c.run(NOW,OP)['reconciled']);self.assertEqual(json.loads(r.get(key_trade('t')))['state'],'RECOVERY_PATH_AMBIGUOUS');self.assertIsNotNone(r.get(key_outbox('FINAL:t')))
 def test_OHLC_order_proven_STOP_wins_unchanged(self):
  raw,l,r,src,b,c=fixture();self.add(r);src.bs=[bar(OP)];src.ts=[{'t':OP.isoformat(),'p':9.4},{'t':(OP+timedelta(seconds=1)).isoformat(),'p':11.2}]
  self.assertTrue(c.run(NOW,OP)['reconciled']);self.assertEqual(json.loads(r.get(key_trade('t')))['state'],'CLOSED_STOP');self.assertIsNone(r.get(key_outbox('FINAL:t')))
 def test_A_nonterminal_original_result_allowed_without_stricter_contract(self):
  raw,l,r,src,b,c=fixture();self.add(r);self.assertTrue(c.run(NOW,OP)['reconciled']);self.assertIn('X',b.p._active_by_symbol)
 def test_closed_STOP_not_inverted_on_R(self):
  raw,l,r,src,b,c=fixture();self.add(r);src.ts=[{'t':OP.isoformat(),'p':9.4}];c.run(NOW,OP);before=r.get(key_trade('t'));c.persisted_R.add('X');src.ts=[{'t':OP.isoformat(),'p':20}]
  v=c.run(NOW,OP);self.assertTrue(v['reconciled']);self.assertEqual(r.get(key_trade('t')),before);self.assertNotIn('X',c.persisted_R)
 def test_expiry_original_FINAL(self):
  raw,l,r,src,b,c=fixture();t=self.add(r);t['monitoring_deadline']=(OP+timedelta(seconds=1)).isoformat();r.set(key_trade('t'),canonical_json(t))
  self.assertTrue(c.run(NOW,OP)['reconciled']);self.assertEqual(json.loads(r.get(key_trade('t')))['state'],'MONITORING_EXPIRED');self.assertIsNotNone(r.get(key_outbox('FINAL:t')))
 def test_missing_input_or_source_failure_retains_A_R(self):
  raw,l,r,src,b,c=fixture();self.add(r);before=r.get(key_trade('t'));src.fail=True;v=c.run(NOW,OP)
  self.assertFalse(v['reconciled']);self.assertEqual(r.get(key_trade('t')),before);self.assertIn('X',c.inventory()['A']);self.assertIn('X',c.inventory()['R'])
 def test_epoch_change_prevents_commit_retains_pin(self):
  raw,l,r,src,b,c=fixture();self.add(r);src.ts=[{'t':OP.isoformat(),'p':9.4}];before=r.get(key_trade('t'));calls=[]
  def guard():
   calls.append(1)
   if len(calls)>2:raise ValueError('EPOCH_CHANGED_FIXTURE')
  c.epoch_guard=guard;v=c.run(NOW,OP);self.assertFalse(v['reconciled']);self.assertEqual(r.get(key_trade('t')),before);self.assertIn('X',c.inventory()['A'])
 def test_halted_original_requires_status_and_stays_pinned(self):
  raw,l,r,src,b,c=fixture();self.add(r,'HALTED_ACTIVE');v=c.run(NOW,OP);self.assertFalse(v['reconciled']);self.assertIn('X',c.inventory()['A']);self.assertTrue(any('HALTED_ACTIVE' in x['reason'] for x in v['issues']))
 def test_U_known_no_commit_resolves_via_canonical(self):
  raw,l,r,src,b,c=fixture();o=base_record('opportunity','2026-10-28','X','CONFLUENCE_VALID',OP.isoformat());o.update(e_id='e',b_id='b',delta_seconds=0,first_event='E',entry_trigger_ts=OP.isoformat(),expiry=(OP+timedelta(minutes=15)).isoformat(),terminal_reason=None,leader_generation=1,worker_instance_id='w')
  r.set(key_opportunity('2026-10-28','X'),canonical_json(o));b.p._entry_uncertain.add('X');v=c.run(NOW,OP);self.assertTrue(v['reconciled']);self.assertNotIn('X',b.p._entry_uncertain);self.assertFalse(b.p._active_by_symbol)
 def test_U_missing_canonical_not_assumed_uncommitted(self):
  raw,l,r,src,b,c=fixture();b.p._entry_uncertain.add('X');v=c.run(NOW,OP);self.assertFalse(v['reconciled']);self.assertIn('X',b.p._entry_uncertain)
 def test_R_native_failure_keeps_pin(self):
  raw,l,r,src,b,c=fixture();c.persisted_R.add('X');src.native_5m=lambda *a:(_ for _ in ()).throw(ValueError('fixture_native_missing'));v=c.run(NOW,OP)
  self.assertFalse(v['reconciled']);self.assertIn('X',c.inventory()['R'])

class OriginalFirstTerminalPriority(unittest.TestCase):
 def test_prior_legal_BAR_exit_not_replaced_by_later_OHLC_conflict(self):
  raw,l,r,src,b,c=fixture();t=basefixtures.CaseTests().trade();t['state']='ACTIVE_POST_T1';r.set(key_trade('t'),canonical_json(t))
  first=bar(OP);first.update(h=10.1,l=9.8,c=9.9);later=bar(OP+timedelta(minutes=1));later.update(h=12,l=9,c=10)
  src.bs=[first,later];self.assertTrue(c.run(NOW,OP)['reconciled']);self.assertEqual(json.loads(r.get(key_trade('t')))['state'],'CLOSED_POST_T1_BREAKEVEN_EXIT');self.assertIsNone(r.get(key_outbox('FINAL:t')))
