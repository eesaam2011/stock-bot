"""Saved fault-class replay with compressed traffic. No live client or socket.

Recorded cancellations are injected at original semantic seams (warmup or
evidence append), not reconstructed at exact historical socket/timer instants.
The timing models are arithmetic on command counts, not a real-time simulator.
"""
import contextlib,copy,hashlib,io,json,math,types
from collections import Counter
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from ipr import review_flags
from r4b import recovery_io as rio
from r1010b_support import fixture
from test_boundary_enumeration import AuditWire,terminal_trade,terminal_opportunity
from test_direct import OP,bar
from state_store import canonical_json

FIX=Path(__file__).parent/'fixtures'

def baseline_module():
    module=types.ModuleType('r4b._saved_e35_replay');module.__package__='r4b'
    exec(compile((FIX/'e35_recovery_io_baseline.txt').read_text(),rio.__file__,'exec'),module.__dict__)
    return module

def replay(day,variant='on',traffic=True):
    schedule=[x for x in json.loads((FIX/'boundary_schedule_18_19_20.json').read_text())['attempts'] if x['day']==day]
    rows=[];state_rows=[];bars=0
    with patch.object(review_flags,'RECOVERY_BOUNDARY_ENUMERATION_ENABLED',variant=='on'):
        with contextlib.redirect_stdout(io.StringIO()):f=fixture(500)
        raw=f.a.bridge.r.r;prefix=f.a.bridge.r.prefix
        for i in range(18):k,v=terminal_trade(i);raw.state['values'][prefix+k]=v
        for i in range(633):k,v=terminal_opportunity(i);raw.state['values'][prefix+k]=v
        wire=AuditWire(f)
        initialwarm=f.src.warmup;initialappend=f.a.checkpoint.append_epoch
        source=[None]
        def warm(*args):
            nonlocal bars
            value=initialwarm(*args)
            # Preserve the original queue/expiry implementation; one existing
            # timer call drives both conditional canonical checkpoints.
            f.a.bridge.p.expire_entry_opportunities(f.now[0])
            if traffic:
                for _ in range(round(float(source[0]['duration_seconds'])*430/60)):
                    symbol=f.symbols[bars%500]
                    message=dict(bar(OP+timedelta(minutes=4+bars//500)),T='b',S=symbol)
                    f.history[symbol].append({k:v for k,v in message.items() if k not in ('T','S')})
                    at=f.now[0]+timedelta(seconds=bars*60/430)
                    # Reader/proof callbacks only. Calling the whole pipeline
                    # synchronously here would run it on the recovery thread
                    # and invent a control-lock overlap, unlike live ingress.
                    f.r.received(message,at);bars+=1
            if source[0]['reason']=='RECOVERY_CANCELLED':f.a.close_eligibility('SUBSCRIPTION_CHANGE_REQUIRES_RECOVERY')
            return value
        def append(event):
            result=initialappend(event)
            if event.get('kind')=='B_EVIDENCE_READY':
                if source[0]['reason']=='RECOVERY_TIMER_CHANGED':f.a.bridge.p.expire_entry_opportunities(f.now[0])
                if source[0]['reason']=='RECOVERY_HOT_STATE_CHANGED':f.a.bridge.p.halted.add('Y')
            return result
        f.src.warmup=warm;f.a.checkpoint.append_epoch=append
        impl=baseline_module() if variant=='base' else rio
        for item in schedule:
            source[0]=item
            # Separate loss of eligibility from source success/failure schedule.
            f.a.close_eligibility('RECORDED_REPLAY_TRUST_LOST')
            before=wire.counts.copy();z0=wire.zero;n0=wire.nonzero
            with contextlib.redirect_stdout(io.StringIO()):result=impl.recover(f.a,f.now[0])
            counts=wire.counts-before
            snapshot=f.a.measure.recovery_io.snapshot()['last_recoveries'][-1]
            actual_reason='' if result else f.a.report.get('reason')
            expected=item['success'] in (True,'True','true')
            if result!=expected or (not result and actual_reason!=item['reason']):
                raise AssertionError(('REPLAY_RESULT_MISMATCH',day,item['attempt_sequence'],actual_reason))
            sc=snapshot.get('io',{}).get('redis.scan',{})
            mg=snapshot.get('io',{}).get('redis.mget',{})
            b=item['non_scan_residual_seconds']
            b=float(b) if b!='UNKNOWN' else float(item['duration_seconds'])
            rows.append({'day':day,'attempt':item['attempt_sequence'],'variant':variant,'source_log_id':item['log_id'],
              'source_duration_seconds':float(item['duration_seconds']),'source_SCAN_count':item['scan_count'],
              'success':result,'reason':actual_reason,'SCAN_commands':counts['SCAN'],'SCAN_ZERO':wire.zero-z0,'SCAN_NONZERO':wire.nonzero-n0,
              'canonical_MGET_commands':mg.get('count',0),'all_client_MGET_commands':counts['MGET'],'all_client_GET_commands':counts['GET'],'all_client_EVAL_commands':counts['EVAL'],
              'guard_calls':snapshot['guard_calls'],'checks_calls':snapshot['checks_calls'],
              'model_B_seconds':b,'model_2s_ZERO_003_NONZERO_seconds':b+2*(wire.zero-z0)+.003*(wire.nonzero-n0),
              'model_uniform_012s_SCAN_seconds':b+.012*counts['SCAN'],
              'model_scope':'fixed synthetic keys; injected failure classes; source non-SCAN residual held constant; not live outcome prediction'})
            state_rows.append({'result':result,'reason':actual_reason,'canonical':canonical_json(raw.state),
              'pins':canonical_json({k:sorted(v) for k,v in f.a.barrier.inventory().items()}),'epochs':canonical_json(f.epochs),'trusted':f.a.transport_trusted})
        digest=hashlib.sha256(canonical_json(state_rows).encode()).hexdigest()
    return {'day':day,'variant':variant,'symbols':500,'requested_BAR_per_logical_minute':430,'bars_delivered':bars,'rows':rows,'state_sha256':digest,
      'traffic_scope':'compressed reader/proof BAR callbacks only; no full ingress pipeline, ACK/native/market or wall-time replay'}
