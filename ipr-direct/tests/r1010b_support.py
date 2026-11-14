"""Networkless R10.10b fixtures. Deterministic signal predicates, original routing.

SIP replies are snapshots consistent with the BAR witnesses available at attempt
start. This isolates cache publication races, not SIP arrival/revision behavior.
"""
import asyncio
import copy
import fnmatch
import math
import threading
import time
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace as NS

from early_core_engine import EarlyCoreCrossing
from signal_admission import SignalAdmission
from r4a.runtime import PipelinePort, RecoveryPort
from ws_ingress import BoundedWSIngress
from test_direct import OP, bar
from test_r3 import setup, receive
from r4b.recovery_io import install
from state_store import base_record, canonical_json, key_trade, key_opportunity, key_base_ready


def fixture(n=500, b_symbols=()):
    symbols=['X'] + ['S%03d' % i for i in range(1,n)]
    f=setup(symbols)
    f.a.proof.generation='r1010b-fixed-generation'
    f.now[0]=OP+timedelta(minutes=5,seconds=1)
    f.a.bridge.p.signal_gate=SignalAdmission(f.a.bridge.p.session,
        {'open_utc':OP.isoformat(),'close_utc':(OP+timedelta(minutes=390)).isoformat()})
    for s in symbols:
        receive(f,s,3)
    install(f.a)
    if not f.a.recover(f.now[0]):raise AssertionError(f.a.report)
    p=f.a.bridge.p
    f.b_engine_calls=[]
    def base(s,b,at):
        f.b_engine_calls.append(s)
        return {'base_ready':s in set(b_symbols),
            'features':dict(opportunity=1.,failure_pressure=0.,demand_efficiency=1.,
                            price_acceptance=1.,volume_acceleration=1.),
            'diagnostics':dict(price=10.,vwap=10.,demand_efficiency=1.,price_acceptance=1.,volume_acceleration=1.)}
    p.br.on_completed_native_1m=base
    for s in b_symbols:
        f.a.bridge.event('BAR',dict(bar(OP+timedelta(minutes=4)),T='b',S=s),f.now[0],True)
        if f.a.bridge.r.get(key_base_ready(p.session,s)) is None:
            raise AssertionError('fixture must persist an actual original B record')
    f.symbols=symbols
    return f


class NativeOverlap:
    """One native cycle admitted while trusted, delayed inside its original REST seam."""
    def __init__(self,f,targets=('S001',)):
        self.f=f;self.targets=set(targets)
        self.entered=threading.Event();self.release=threading.Event();self.done=threading.Event()
        self.errors=[];self.ec_calls=[]
        p=f.a.bridge.p
        f.native_original_bars=f.src.bars_multi
        def fetch(symbols,*args,**kw):
            self.entered.set()
            if not self.release.wait(90):raise AssertionError('native fixture release timeout')
            return {s:[dict(bar(OP),_timeframe='native_5Min')] for s in symbols}
        f.src.bars_multi=fetch
        def evaluate(symbol,rows,at):
            self.ec_calls.append(symbol)
            crossing=None
            if symbol in self.targets:
                crossing=EarlyCoreCrossing(symbol,p.session,OP,OP+timedelta(minutes=5),at,at,3.,1.)
            return {},crossing
        p.ec=NS(required_history_minutes=lambda:305,evaluate_with_telemetry=evaluate)
        def work():
            try:self.result=f.a.bridge.native_cycle(f.symbols,f.now[0],True)
            except BaseException as exc:self.errors.append(exc)
            finally:self.done.set()
        self.thread=threading.Thread(target=work,name='inflight-native-fixture')

    def start(self):
        if not self.f.a.transport_trusted:raise AssertionError('must admit BEFORE trust loss')
        self.thread.start()
        if not self.entered.wait(3):raise AssertionError('native did not enter fetch')

    def close(self):
        self.release.set();self.thread.join(5)
        if self.thread.is_alive():raise AssertionError('native still running')
        if self.errors:raise self.errors[0]


class NativeWaves:
    """One pre-admitted cycle, ten staggered original E/confluence completions."""
    def __init__(self,f):
        self.f=f;self.targets=['S%03d'%i for i in range(1,11)]
        self.enter=[threading.Event() for _ in self.targets]
        self.release=[threading.Event() for _ in self.targets]
        self.published=[threading.Event() for _ in self.targets]
        self.abort=False;self.errors=[]
        p=f.a.bridge.p
        f.src.bars_multi=lambda symbols,*a,**k:{s:[dict(bar(OP),_timeframe='native_5Min')] for s in symbols}
        def evaluate(symbol,rows,at):
            if symbol not in self.targets:return {},None
            i=self.targets.index(symbol);self.enter[i].set()
            if not self.release[i].wait(30):raise AssertionError('wave release timeout')
            return {},None if self.abort else EarlyCoreCrossing(symbol,p.session,OP,OP+timedelta(minutes=5),at,at,3.,1.)
        p.ec=NS(required_history_minutes=lambda:305,evaluate_with_telemetry=evaluate)
        old=p._confluence
        def confluence(symbol,*args):
            try:return old(symbol,*args)
            finally:
                if symbol in self.targets:self.published[self.targets.index(symbol)].set()
        p._confluence=confluence
        def run():
            try:self.result=f.a.bridge.native_cycle(f.symbols,f.now[0],True)
            except BaseException as exc:self.errors.append(exc)
        self.thread=threading.Thread(target=run,name='one-native-ten-waves')
    def start(self):
        if not self.f.a.transport_trusted:raise AssertionError('not pre-admitted')
        self.thread.start()
        if not self.enter[0].wait(3):raise AssertionError('wave not entered')
    def next(self,i):
        if not self.enter[i].wait(3):raise AssertionError('next wave not entered')
        self.release[i].set()
        if not self.published[i].wait(3):raise AssertionError('confluence did not return')
    def close(self):
        self.abort=True
        for event in self.release:event.set()
        self.thread.join(5)
        if self.thread.is_alive():raise AssertionError('wave thread alive')
        if self.errors:raise self.errors[0]


async def flow(duration,overlap=False):
    f=fixture(b_symbols=('S001',) if overlap else ())
    native=NativeOverlap(f) if overlap else None
    if native:native.start()
    warm_entered=threading.Event()
    old_warm=f.src.warmup
    attempt_starts=[]
    def warm(*args):
        snapshot=copy.deepcopy(old_warm(*args))
        attempt_starts.append(time.monotonic())
        warm_entered.set()
        time.sleep(duration)
        return snapshot
    f.src.warmup=warm
    start=time.monotonic();logical=f.now[0]
    baseline=f.a.measure.recovery_io.snapshot()
    ingress=BoundedWSIngress(PipelinePort(f.a),RecoveryPort(f.a),lambda:f.a.transport_trusted)
    await ingress.begin()
    stop=asyncio.Event();delivered=0;native_released=False
    attempts=[]
    async def traffic():
        nonlocal delivered,native_released
        # Steady decoded callbacks at 430 BAR/minute across the 500-symbol scope.
        while not stop.is_set():
            elapsed=time.monotonic()-start
            f.now[0]=logical+timedelta(seconds=elapsed)
            target=math.floor(elapsed*430/60)+1
            while delivered<target:
                symbol=f.symbols[delivered%500]
                minute=4+delivered//500
                row=dict(bar(OP+timedelta(minutes=minute)),T='b',S=symbol)
                f.history[symbol].append({k:v for k,v in row.items() if k not in ('T','S')})
                f.r.received(row,f.now[0])
                ingress.submit('BAR',row,f.now[0])
                delivered+=1
            if native and warm_entered.is_set() and elapsed>=duration/2 and not native_released:
                native.release.set();native_released=True
            await asyncio.sleep(.01)
    task=asyncio.create_task(traffic())
    try:
        for _ in range(3):
            f.now[0]=logical+timedelta(seconds=time.monotonic()-start)
            at=time.monotonic()
            ok=await asyncio.to_thread(f.a.recover,f.now[0])
            attempts.append({'success':ok,'reason':f.a.report.get('reason'),
                'elapsed_seconds':time.monotonic()-at})
            if ok:break
            # Scheduler's minimum START spacing; long attempts add no extra second.
            await asyncio.sleep(max(0,1-(time.monotonic()-at)))
        elapsed=time.monotonic()-start
    finally:
        stop.set();await task
        if native:native.close()
        await ingress.end()
    m=f.a.measure.recovery_io.snapshot()
    before=baseline['cancellations']
    cancels={k:v-before.get(k,0) for k,v in m['cancellations'].items() if v>before.get(k,0)}
    return {'hold_seconds':duration,'mode':'pre_admitted_native' if overlap else 'gated_BAR_only',
        'scope_symbols':500,'requested_BAR_per_minute':430,'received_BAR':delivered,
        'processed_BAR':ingress.processed['BAR'],'queue_high_water':ingress.high_water,
        'max_queue_delay_seconds':ingress.max_delay,'fail_closed_count':ingress.failure_count,
        'attempts':attempts,'attempt_count':len(attempts),'cancellations':cancels,
        'trusted_at_end':f.a.transport_trusted,'untrusted_seconds':elapsed,
        'E_crossings':getattr(native,'result',{}).get('early_core_persisted',0) if native else 0,
        'new_B_engine_calls_during_untrusted':len(f.b_engine_calls)-(1 if overlap else 0),
        'native_engine_calls':len(native.ec_calls) if native else 0,
        'new_opportunity_observed': bool(native and f.a.bridge.r.get(key_opportunity(f.a.bridge.p.session,'S001'))),
        'limits':'SIP snapshot consistent at attempt start; no full case_loop/control ACK or real signal threshold model'}


class Wire:
    """Logical command observer over an in-memory subset; never a Redis client."""
    def __init__(self,raw,keys,latency=0.,real=False):
        self.raw=raw;self.keys=sorted(keys);self.latency=latency;self.real=real
        self.counts=Counter();self.logical_seconds=0.;self.leader_mget=0
        self.originals={name:getattr(raw,name) for name in ('scan','mget','get','eval')}
        raw.scan=self.scan;raw.mget=self.mget;raw.get=self.get;raw.eval=self.eval
    def record(self,name):
        self.counts[name]+=1;self.logical_seconds+=self.latency
        if self.real:time.sleep(self.latency)
    def scan(self,cursor=0,match='*',count=500):
        self.record('SCAN');start=int(cursor);end=min(start+count,len(self.keys))
        rows=[k for k in self.keys[start:end] if fnmatch.fnmatchcase(k,match)]
        return end if end<len(self.keys) else 0,rows
    def mget(self,keys):
        self.record('MGET')
        if len(keys)==2 and any(k.endswith(':runtime:leader') for k in keys):self.leader_mget+=1
        return [self.raw.state['values'].get(k) for k in keys]
    def get(self,key):self.record('GET');return self.raw.state['values'].get(key)
    def eval(self,*args):
        self.record('EVAL');return self.originals['eval'](*args)


def cost_profile(total_keys=29,trades=27,opps=0,latency=0.,real=False,flag=True,recovery_impl=None):
    from ipr import review_flags
    from unittest.mock import patch
    f=fixture(n=2)
    if recovery_impl is not None:
        f.a.bridge.p.expire_entry_opportunities=f.a._recovery_io.original_timer
        del f.a._recovery_io
        recovery_impl.install(f.a)
    raw=f.a.bridge.r.r;prefix=f.a.bridge.r.prefix
    for i in range(trades):
        row=base_record('trade','2026-10-27','H%04d'%i,'CLOSED_STOP',OP.isoformat())
        row.update(trade_id='historical-%d'%i,monitoring_deadline=OP.isoformat(),
            entry_alert_price=10.,structure_low=9.5,structural_stop=9.5,risk_pct=5.,t1=10.5,t2=11.,
            leader_generation=1,worker_instance_id='w')
        raw.state['values'][prefix+key_trade(row['trade_id'])]=canonical_json(row)
    for i in range(opps):
        row=base_record('opportunity','2026-10-27','O%04d'%i,'OPPORTUNITY_EXPIRED',OP.isoformat())
        row.update(e_id=None,b_id=None,entry_trigger_ts=None,delta_seconds=None,first_event='E',
            expiry=OP.isoformat(),terminal_reason='OPPORTUNITY_EXPIRED',leader_generation=1,worker_instance_id='w')
        raw.state['values'][prefix+key_opportunity(row['session'],row['symbol'])]=canonical_json(row)
    while len(raw.state['values'])<total_keys:
        raw.state['values']['unrelated-key-%d'%len(raw.state['values'])]='fixture'
    wire=Wire(raw,raw.state['values'],latency,real)
    checks=[0];require=[0]
    old_checks=f.a.checks;old_require=f.a.bridge.leadership.require_current
    def check():checks[0]+=1;return old_checks()
    def leader():require[0]+=1;return old_require()
    f.a.checks=check;f.a.bridge.leadership.require_current=leader
    old_count=f.a.measure.recovery_io.count
    start=time.monotonic()
    with patch.object(review_flags,'RECOVERY_IO_OUTSIDE_LOCK_ENABLED',flag):
        success=(recovery_impl.recover(f.a,f.now[0]) if recovery_impl is not None else f.a.recover(f.now[0]))
    elapsed=time.monotonic()-start
    metrics=f.a.measure.recovery_io.snapshot()
    row=metrics['last_recoveries'][-1] if metrics['recoveries']>old_count else None
    return {'flag':flag,'keyspace':len(wire.keys),'trade_keys':trades,'opportunity_keys':opps,
        'SCAN_COUNT':500,'MGET_batch':500,'commands':dict(wire.counts),
        'leader_identity_MGET':wire.leader_mget,'checks_calls':checks[0],
        'require_current_calls':require[0],'guard_calls':row.get('guard_calls') if row else None,
        'success':success,'elapsed_wall_seconds':elapsed,'injected_seconds_per_command':latency,
        'sum_command_delay_seconds':wire.logical_seconds,'delay_mode':'real_sleep' if real else 'arithmetic_only',
        'assumptions':'fixed keyspace; SCAN visits exactly COUNT keys incl nonmatches; valid lease renewed independently; no live cases/REST delay'}
