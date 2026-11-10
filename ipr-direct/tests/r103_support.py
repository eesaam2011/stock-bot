"""Controlled offline harness for frozen production methods, not a market replay.

Synthetic opportunities/signals/receipt times are explicit. Commit sinks are
Python lists only, with no Redis client and no transport. Trading methods are
the unmodified reference_app implementation, including risk and monitoring.
"""
import json,threading,contextlib
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace as NS
from production_pipeline import ProductionDecisionPipeline
from signal_admission import SignalAdmission
from state_store import base_record
from ipr.data import dt

class Lock:
    def __init__(self):self.lock=threading.RLock()
    def operation(self,name):return contextlib.nullcontext()
    def __enter__(self):self.lock.acquire();return self
    def __exit__(self,*a):self.lock.release()

class Sinks:
    def __init__(self):self.entries=[];self.followups=[];self.terminals=[]
    def atomic_entry(self,*a):self.entries.append(json.loads(a[5]))
    def atomic_trade_event(self,*a):self.followups.append(json.loads(a[5]))
    def atomic_opportunity_terminal(self,*a):self.terminals.append(json.loads(a[-1]))

def pipeline(symbol,start,price,signals=True):
    p=object.__new__(ProductionDecisionPipeline);p._decision_lock=Lock()
    p.session=start.date().isoformat();p.worker_id='offline-fixture';p.lua=Sinks()
    p.leadership=NS(require_current=lambda:NS(leader_generation=1,worker_instance_id='offline-fixture'))
    p.signal_gate=SignalAdmission(p.session,{'open_utc':(start-timedelta(hours=1)).isoformat(),'close_utc':(start+timedelta(hours=1)).isoformat()})
    p._fixture_now=start;p.utc_now=lambda:p._fixture_now
    p._fixture_signals=signals
    p._get=lambda key,kind:{'bar_end_ts':start.isoformat(),'decision_available_ts':start.isoformat()} if p._fixture_signals else None
    opp=base_record('opportunity',p.session,symbol,'CONFLUENCE_VALID',start.isoformat())
    opp.update(e_id='e',b_id='b',delta_seconds=0,first_event='E',entry_trigger_ts=start.isoformat(),expiry=(start+timedelta(minutes=15)).isoformat(),terminal_reason=None,leader_generation=1,worker_instance_id=p.worker_id)
    p._entry_opportunities={symbol:opp};p._entry_uncertain=set();p._active_by_symbol={}
    p._structure_watch_symbols={symbol};p.halted=set();p.trades={};p.entry_trade_buffer_seconds=15
    p.bars={symbol:[((start-timedelta(minutes=1)).isoformat(),price*.98,start)]}
    p.terminal_counts=Counter();p.entry_exit_counts=Counter();p.entry_exit_counts_by_symbol=Counter()
    p.raw_trade_messages_received=0
    return p

def trade(symbol,when,price,conditions=('@',),tape='C',sequence=1):
    return dict(T='t',S=symbol,p=price,t=when.isoformat(),s=1,c=list(conditions),z=tape,_seq=sequence)

def feed(p,msg,allow):
    p._fixture_now=dt(msg['t'])
    return p.on_trade(msg['S'],msg,p._fixture_now,allow)
