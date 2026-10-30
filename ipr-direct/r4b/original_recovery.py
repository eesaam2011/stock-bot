"""Routing only: every outcome/transition is from unchanged reference modules."""
from datetime import timedelta
import json,math
from state_store import validate_record
from itertools import groupby
from contextlib import nullcontext
from active_trade_recovery import ActiveTradeChronologicalReconciler,TERMINAL,parse_ts
from trade_monitor import MarketEvent,apply_event
from gap_recovery import recovery_path_from_ohlc
from trade_transition_commit import TradeTransitionWriter
from state_store import canonical_json,key_trade
class RecoveryUnavailable(RuntimeError):pass
class EpochRedis:
 def __init__(self,r,guard,lock=None):self.r=r;self.guard=guard;self.lock=lock
 def __getattr__(self,k):return getattr(self.r,k)
 def eval(self,*a):
  with (self.lock or nullcontext()):self.guard();return self.r.eval(*a)
class TradeBackend:
 def __init__(self,bridge,guard):self.bridge=bridge;self.guard=guard
 def commit(self,trade_key,expected_raw,new_raw,outbox_key,outbox_raw,crash=None):
  if crash is not None:raise RecoveryUnavailable('CRASH_ARGUMENT_FORBIDDEN')
  with (getattr(self.bridge,'recovery_control_lock',None) or nullcontext()):
   self.guard();self.bridge.leadership.require_current()
   return self.bridge.p.lua.atomic_trade_event(self.bridge.p.worker_id,trade_key,expected_raw,new_raw,outbox_key,outbox_raw)
class Inputs:
 def __init__(self,trades,bars):self.ts=trades;self.bs=bars
 def trades(self,*a):return self.ts
 def native_1m_gap(self,*a):return self.bs

RESUME_CAS_LUA="""
if redis.call('GET',KEYS[1])~=ARGV[1] or redis.call('GET',KEYS[2])~=ARGV[2] then return -10 end
if redis.call('GET',KEYS[3])~=ARGV[3] then return -20 end
redis.call('SET',KEYS[3],ARGV[4]);return 1
"""
def reconcile_trade(bridge,trade,now,guard,status_source=None):
 guard()
 if trade['state'] in TERMINAL:return {'ambiguous':False,'state':trade['state'],'trade':trade,'existing_terminal':True}
 if not callable(getattr(bridge.rest,'trades',None)) or not callable(getattr(bridge.rest,'native_1m_gap',None)):raise RecoveryUnavailable('RECOVERY_INPUT_MISSING_TRADES_OR_NATIVE1MIN')
 start=parse_ts(trade.get('updated_at') or trade.get('entry_alert_sent_at'))
 trades=bridge.rest.trades(trade['symbol'],start,now);bars=bridge.rest.native_1m_gap(trade['symbol'],start,now)
 if not isinstance(trades,list) or not isinstance(bars,list):raise RecoveryUnavailable('RECOVERY_INPUT_CONTAINER_INVALID')
 if any(parse_ts(b['t'])+timedelta(minutes=1)>now for b in bars):raise RecoveryUnavailable('INCOMPLETE_GAP_BAR')
 if any(any(isinstance(b.get(k),bool) or not isinstance(b.get(k),(int,float)) or not math.isfinite(b[k]) for k in ('h','l','c')) or b['h']<b['l'] for b in bars):raise RecoveryUnavailable('INVALID_GAP_OHLC')
 guard();r=EpochRedis(bridge.r,guard,getattr(bridge,'recovery_control_lock',None));inputs=Inputs(trades,bars)
 original=ActiveTradeChronologicalReconciler(inputs,r,bridge.p.worker_id,now_fn=lambda:now)
 if trade['state']=='HALTED_ACTIVE':
  from halt_reconciliation import HaltReconciler
  from transitions import validate_trade_transition
  from models import TradeState as StateTag
  if status_source is None or status_source.current(trade['symbol'])!='TRADING':return original.reconcile(trade)
  resumed=HaltReconciler(status_source,original).on_resume(trade)
  validate_trade_transition(StateTag(trade['state']),StateTag(resumed['state']))
  guard();token=bridge.leadership.require_current();lua=bridge.p.lua
  rc=r.eval(RESUME_CAS_LUA,3,lua.leader_key,lua.generation_key,key_trade(trade['trade_id']),token.worker_instance_id,str(token.leader_generation),canonical_json(trade),canonical_json(resumed))
  if int(rc)!=1:raise RecoveryUnavailable('RESUME_CANONICAL_CAS_FAILED')
  # Resume state is produced by the ORIGINAL HaltReconciler. Re-run the original
  # chronological path on that canonical expected state; no invented price/event.
  return reconcile_trade(bridge,resumed,now,guard,status_source)
 # The original chronological result has priority. Never replace a legal terminal
 # outcome already determined by the frozen engine with a later OHLC ambiguity.
 result=original.reconcile(trade)
 if result.get('state') in TERMINAL and not result.get('ambiguous'):return result
 if result.get('ambiguous') and result.get('reason')!='RECOVERY_PATH_AMBIGUOUS':return result
 # Determine only the existing OHLC ambiguity predicate, never synthesize TRADE prices.
 # Replay prior frozen events to choose the already-defined current T1/T2 state.
 events=original._events(trade,start,now);state=original._state(trade)
 ambiguous_at=None
 for b in sorted(bars,key=lambda b:parse_ts(b['t'])):
  bs=parse_ts(b['t']);be=bs+timedelta(minutes=1)
  if be>now:continue
  if any(k not in b for k in ('h','l')):raise RecoveryUnavailable('OHLC_INPUT_MISSING')
  state=original._state(trade)
  for e in events:
   if e.event_ts>=bs:break
   state,_=apply_event(state,e)
  if state.state in TERMINAL:break
  target=state.t1 if state.state=='ACTIVE_PRE_T1' else state.t2
  stop= float(b['l'])<=state.stop;hit=float(b['h'])>=target
  within=[x for x in trades if bs<=parse_ts(x['t'])<be]
  # Distinct timestamps that witness BOTH sides provide order; equal-ts ambiguity
  # remains the responsibility of the original chronological reconciler.
  stops=[parse_ts(x['t']) for x in within if float(x['p'])<=state.stop]
  targets=[parse_ts(x['t']) for x in within if float(x['p'])>=target]
  ordered=bool(stops and targets and set(stops).isdisjoint(targets))
  if recovery_path_from_ohlc(stop,hit,ordered)=='RECOVERY_PATH_AMBIGUOUS':ambiguous_at=be;break
 if ambiguous_at is not None:
  result={'ambiguous':True,'reason':'RECOVERY_PATH_AMBIGUOUS','event_ts':ambiguous_at.isoformat()}
 if result.get('ambiguous') and result.get('reason')=='RECOVERY_PATH_AMBIGUOUS':
  ts=parse_ts(result['event_ts']);guard()
  current=json.loads(bridge.r.get(key_trade(trade['trade_id'])));validate_record(current,'trade')
  if current['state'] in TERMINAL:return {'ambiguous':False,'state':current['state'],'trade':current,'existing_terminal':True}
  ns,event=apply_event(original._state(current),MarketEvent('RECOVERY_AMBIGUOUS',ts,0,None,True))
  guard()
  writer=TradeTransitionWriter(TradeBackend(bridge,guard),bridge.leadership)
  new,out=writer.commit(current,ns.state,event.value,ts,payload_extra={'recovered':True})
  return {'ambiguous':False,'state':new['state'],'trade':new,'legal_ambiguity_final':True,'outbox_event':out['event_type']}
 return result
