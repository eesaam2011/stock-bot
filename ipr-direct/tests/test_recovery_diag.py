"""Review-only diagnostics: in-memory command models, sockets disabled by runner.
Recorded Nov18/19 schedules inject failure classes into the real recovery code;
this is not a complete replay of live SIP/control callbacks or live time.
"""
import asyncio
import contextlib
import copy
import hashlib
import io
import json
import socket
import threading
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from ipr import review_flags
from ipr import recovery_diag as d
from ipr.observability import Diagnostics, RedisTelemetry
from r4a.guards import AckTrustDeadline, SafetyStop
from r4b import recovery_io as rio
from test_r1010 import ready, add_trade
from r1010b_support import Wire
from state_store import canonical_json

HERE=Path(__file__).resolve().parent
FIX=HERE/'fixtures'


class Clock:
    def __init__(self):self.value=1000.
    def __call__(self):return self.value
    def wall(self):return datetime.fromtimestamp(self.value,timezone.utc).isoformat()


def runtime_fixture():
    f=ready()
    f.r.boot_number=2
    return f


def open_window(recorder):
    recorder.gate_transition({'stage':'SCAN_GATE_TRANSITION','reason':'TRUST_LOST',
                              'from':True,'to':False})


def capture(f):
    raw=getattr(f.a.bridge.r.r,'raw',f.a.bridge.r.r)
    return {'canonical':canonical_json(raw.state),
            'pins':canonical_json({k:sorted(v) for k,v in f.a.barrier.inventory().items()}),
            'epochs':canonical_json(f.epochs),
            'proofs':canonical_json(f.a.c.proofs),
            'trusted':f.a.transport_trusted,
            'warmup_calls':copy.deepcopy(f.calls),
            'timers':[v.isoformat() for v in f.a._recovery_io.timers]}


def run_case(flag,reason=None,baseline=False,break_observer=False,trade=False,schedule=None):
    clock=Clock()
    with patch.object(review_flags,'IPR_RECOVERY_DIAG_ENABLED',False):
        f=runtime_fixture()
        if trade:add_trade(f)
    raw=f.a.bridge.r.r
    f.a._recovery_io.metrics.clock=clock
    wire=Wire(raw,raw.state['values'])
    tele=RedisTelemetry(raw,Diagnostics())
    f.a.bridge.r.r=tele
    f.a.bridge.leadership.backend.r.r=tele
    if trade:
        from test_direct import OP
        from datetime import timedelta
        f.src.ts=[{'t':OP.isoformat(),'p':10.6},{'t':(OP+timedelta(seconds=1)).isoformat(),'p':9.4}]
    old=f.a.checkpoint.append_epoch
    oldwarm=f.a.rest.warmup
    def append(event):
        value=old(event)
        if event.get('kind')=='B_EVIDENCE_READY':
            if reason=='RECOVERY_TIMER_CHANGED':f.a.bridge.p.expire_entry_opportunities(f.now[0])
            if reason=='RECOVERY_HOT_STATE_CHANGED':f.a.bridge.p.entry_exit_counts['fixture']+=1
        return value
    injected_seconds=0.
    def warm(*a,**kw):
        value=oldwarm(*a,**kw)
        clock.value+=injected_seconds
        if reason=='RECOVERY_CANCELLED':f.a.close_eligibility('SUBSCRIPTION_CHANGE_REQUIRES_RECOVERY')
        return value
    f.a.checkpoint.append_epoch=append
    f.a.rest.warmup=warm
    rows=[]
    rec=None
    wallfixed=type('FixedUTC',(),{'now':staticmethod(lambda tz:datetime(2026,11,19,tzinfo=timezone.utc))})
    with patch.object(review_flags,'IPR_RECOVERY_DIAG_ENABLED',flag), \
         patch.object(rio,'time',NS(monotonic=clock,sleep=lambda _:None)), \
         patch.object(rio,'datetime',wallfixed), \
         patch('ipr.observability.time.monotonic',clock):
        if flag:
            rec=d.install(f.r,sink=rows.append,clock=clock,wall=clock.wall,start_writer=False)
            if rec is None:raise AssertionError('installation failed')
            open_window(rec)
            if break_observer:rec.emit=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('OBSERVER_FAULT'))
        impl=rio
        if baseline:
            impl=types.ModuleType('r4b._r1010b_diag_baseline')
            impl.__package__='r4b'
            exec(compile((FIX/'R1010b_recovery_io_baseline.txt').read_text(),'<saved-baseline>','exec'),impl.__dict__)
            impl.time=NS(monotonic=clock,sleep=lambda _:None)
            impl.datetime=wallfixed
        out=io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                sequence=[]
                if schedule is not None:
                    for recorded in schedule:
                        reason=recorded['cancel_reason']
                        injected_seconds=recorded['duration_seconds']
                        result=impl.recover(f.a,f.now[0])
                        sequence.append({'source_log_id':recorded['log_id'],'result':result,
                                         'reason':f.a.report.get('reason'),'state':capture(f),
                                         'commands':dict(wire.counts),'logical_monotonic':clock()})
                else:
                    result=impl.recover(f.a,f.now[0])
                if trade:
                    from ipr.sender import Sender
                    messages=[]
                    wire.keys=sorted(raw.state['values'])
                    from test_direct import OP
                    transport=NS(post=lambda text:(messages.append(text) or 'SENT'))
                    sender=Sender(f.r,tele,{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture'},transport)
                    with patch('ipr.sender.uuid.uuid4',return_value=NS(hex='fixed-review-attempt')):
                        sender.step();sender.step()
                else:messages=[]
            observed=capture(f)
            observed.update(result=result,reason=f.a.report.get('reason'),commands=dict(wire.counts),messages=messages,sequence=sequence,
                            business_stdout=out.getvalue())
            if rec:rec.drain()
        finally:
            if rec:rec.close()
            d._LOCAL.attempt=None
    return observed,[json.loads(v) for v in rows],rec


class DiagnosticContracts(unittest.TestCase):
    def setUp(self):
        self.flag=patch.object(review_flags,'IPR_RECOVERY_DIAG_ENABLED',True)
        self.flag.start();self.addCleanup(self.flag.stop)
        self.f=runtime_fixture();self.clock=Clock();self.rows=[]
        self.rec=d.install(self.f.r,sink=self.rows.append,clock=self.clock,wall=self.clock.wall,start_writer=False)
        self.assertIsNotNone(self.rec)
        open_window(self.rec)
        self.addCleanup(self.rec.close)
        self.addCleanup(lambda:setattr(d._LOCAL,'attempt',None))
        self.addCleanup(lambda:setattr(d._LOCAL,'journal',None))
        self.addCleanup(lambda:setattr(d._LOCAL,'rest_request',None))

    def flush(self):
        self.rec.drain()
        return [json.loads(v) for v in self.rows]

    def begin(self):
        run=rio.RecoveryRun(self.f.a,self.f.a._recovery_io,self.f.now[0])
        self.rec.safe('begin',run)
        return run

    def test_disabled_installs_nothing_and_emits_nothing(self):
        before=self.f.a.close_eligibility
        with patch.object(review_flags,'IPR_RECOVERY_DIAG_ENABLED',False):
            self.assertIsNone(d.install(self.f.r,sink=self.rows.append))
            self.rec.safe('emit','TEST');self.rec.drain()
        self.assertEqual(self.rows,[])
        self.assertEqual(self.f.a.close_eligibility,before)

    def test_producer_never_writes_sink(self):
        self.rec.safe('emit','TEST',count=1)
        self.assertEqual(self.rows,[])
        self.assertTrue(any(x['stage']=='R1010_DIAG_TEST' for x in self.flush()))

    def test_live_decision_lock_skips_payload_and_sink(self):
        with self.f.a.bridge.p._decision_lock:
            self.rec.safe('emit','FORBIDDEN',secret='must-not-collect')
        self.assertFalse(any('FORBIDDEN' in x for x in self.rows))
        self.assertEqual(self.rec.stats()['skipped_live_lock'],1)

    def test_live_control_lock_skips_stack_and_mutation_inspection(self):
        with self.f.a.control_lock:
            self.assertIsNone(self.rec.safe('before_close'))
        self.assertEqual(self.rec.stats()['skipped_live_lock'],1)

    def test_unknown_ownership_fails_closed(self):
        with patch.object(self.rec,'r',NS()):self.rec.safe('emit','NO')
        self.assertEqual(sum(len(v['queue']) for v in self.rec.lanes.values()),0)

    def test_observer_exception_is_swallowed_and_counted(self):
        with patch.object(self.rec,'emit',side_effect=RuntimeError('do-not-log')):
            self.assertIsNone(self.rec.safe('emit','TEST'))
        self.assertEqual(self.rec.observer_errors,1)

    def test_writer_sink_exception_is_swallowed_and_counted(self):
        self.rec.sink=lambda _:(_ for _ in ()).throw(RuntimeError())
        self.rec.drain();self.assertGreater(self.rec.writer_errors,0)

    def test_rate_is_rolling_60_seconds_not_boundary_burst(self):
        self.rec.last_health=int(self.clock()//60)
        for lane in self.rec.lanes.values():lane['queue'].clear()
        with patch.object(d,'MAX_LINES_PER_MINUTE',3):
            for _ in range(4):self.rec.write({'stage':'R1010_DIAG_TEST'})
            self.assertEqual(len(self.rows),3);self.assertEqual(self.rec.dropped_rate,1)
            self.clock.value+=59
            self.rec.write({'stage':'R1010_DIAG_TEST'})
            self.assertEqual(len(self.rows),3)
            self.clock.value+=1
            self.rec.write({'stage':'R1010_DIAG_TEST'})
            self.assertEqual(len(self.rows),4)

    def test_line_size_includes_utf8_and_newline(self):
        self.rec.write({'stage':'R1010_DIAG_TEST','x':'ا'*4096})
        self.assertEqual(self.rec.dropped_size,1)
        self.assertTrue(all(len(v.encode())+1<=d.MAX_LINE_BYTES for v in self.rows))

    def test_producer_queue_has_hard_cap_and_drop_counter(self):
        for _ in range(100):self.rec.safe('emit','TEST',number=1)
        self.assertEqual(max(len(v['queue']) for v in self.rec.lanes.values()),d.LANE_CAPACITY)
        self.assertGreater(self.rec.stats()['dropped_queue'],0)

    def test_scan_fields_cursor_class_and_no_raw_args(self):
        self.begin()
        secret='redis://token:secret@private/0'
        token=self.rec.safe('command_begin','SCAN')
        self.clock.value+=1.25
        self.rec.safe('command','scan',1.25,(),{'match':secret+':trade:*','count':500},(427,[secret]),token)
        rows=self.flush();scan=next(v for v in rows if v['stage']=='R1010_DIAG_SCAN')
        self.assertEqual(scan['pattern_class'],'CANONICAL_TRADE')
        self.assertEqual(scan['cursor_after'],'NONZERO')
        self.assertEqual(scan['returned_keys'],1)
        self.assertEqual(scan['command_number'],1)
        self.assertEqual(scan['start_monotonic'],1000.)
        self.assertEqual(scan['end_monotonic'],1001.25)
        self.assertEqual(scan['pool_wait_seconds'],'UNKNOWN')
        self.assertNotIn(secret,'\n'.join(self.rows));self.assertNotIn(427,scan.values());self.assertNotIn('427',scan.values())

    def test_mget_count_only_no_keys_or_values(self):
        self.begin();token=self.rec.safe('command_begin','MGET')
        self.rec.safe('command','mget',.1,(['SECRET_KEY'],),{},['SECRET_VALUE'],token)
        row=next(v for v in self.flush() if v['stage']=='R1010_DIAG_MGET')
        self.assertEqual(row['requested_keys'],1)
        self.assertNotIn('SECRET_','\n'.join(self.rows))

    def test_concurrent_commands_observed_at_start_without_locks(self):
        token=self.rec.safe('command_begin','EVAL')
        seen=[]
        def worker():
            item=self.rec.safe('command_begin','SCAN');seen.append(item['other_inflight'])
            self.rec.safe('command','scan',.1,(),{},(0,[]),item)
        t=threading.Thread(target=worker);t.start();t.join(2)
        self.rec.safe('command','eval',.2,(),{},None,token)
        self.assertEqual(seen,[{'EVAL':1}])
        self.assertTrue(all(v['active_command'] is None for v in self.rec.lanes.values()))

    def test_outside_recovery_scan_has_separate_cumulative_summary(self):
        token=self.rec.safe('command_begin','SCAN')
        self.rec.safe('command','scan',.3,(),{'match':'secret:outbox:*'},(0,[]),token)
        self.assertFalse(any(v['stage']=='R1010_DIAG_SCAN' for v in self.flush()))
        self.assertEqual(self.rec.outside_summary()['count'],1)

    def test_timer_after_call_observations_and_no_extra_drain(self):
        self.begin();coord=self.f.a._recovery_io
        coord.begin()
        self.f.a.bridge.p.expire_entry_opportunities(self.f.now[0])
        self.assertEqual(len(coord.timers),1)
        value=coord.drain();self.assertEqual(len(value),1)
        self.assertEqual(self.rec.last_timer_drain['count'],1)
        self.assertIsNotNone(self.rec.last_timer_enqueue)
        self.assertEqual(coord.timers,[])

    def test_ack_deadline_uses_original_armed_clock_and_unchanged_120_guard(self):
        calls=[]
        deadline=self.f.r.deadline
        original_clock=deadline.clock
        deadline.clock=lambda:(calls.append(1) or self.clock())
        deadline.ack()
        self.assertEqual(len(calls),1)
        self.assertEqual(self.rec.ack_observed['armed_monotonic'],1000.)
        self.assertEqual(self.rec.ack_observed['deadline_monotonic'],1120.)
        self.clock.value=1119.999;deadline.tick(False)
        self.clock.value=1120.
        with self.assertRaisesRegex(SafetyStop,'WS_ACK_UNTRUSTED_TIMEOUT'):deadline.tick(False)
        self.assertEqual(len(calls),3)
        deadline.clock=original_clock

    def test_trust_loss_and_cancel_origin_are_after_call_observations(self):
        self.f.a.cancel_event.clear()
        self.f.a._LiveAdapter__trusted=True
        self.f.a.c._set_transport_from_adapter(True,__import__('dreview.core',fromlist=['_LIVE_ADAPTER_AUTHORITY'])._LIVE_ADAPTER_AUTHORITY)
        self.f.a.close_eligibility('SUBSCRIPTION_CHANGE_REQUIRES_RECOVERY')
        rows=self.flush()
        self.assertTrue(any(v['stage']=='R1010_DIAG_TRUST_LOST' for v in rows))
        self.assertEqual(self.rec.cancel_observed['invalidation_exact_time'],'UNKNOWN')

    def test_rest_counts_single_original_read_and_bounded_metadata(self):
        self.begin();reads=[];requests=[]
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*a):return False
            def read(self,*a,**k):reads.append(1);return b'{"SECRET_BODY":123}'
        api=NS(opener=lambda *a,**k:(requests.append(1) or Response()))
        def call(host,path,params):
            with api.opener('SECRET_URL',headers='SECRET_HEADERS') as response:return json.loads(response.read())
        api.call=call;self.rec.bind_api(api)
        result=api.call('SECRET_HOST','SECRET_PATH',{'timeframe':'1Min','symbols':'SECRET_SYMBOLS'})
        self.assertEqual(result,{'SECRET_BODY':123});self.assertEqual(reads,[1]);self.assertEqual(requests,[1])
        row=next(v for v in self.flush() if v['stage']=='R1010_DIAG_REST_REQUEST')
        self.assertEqual(row['body_bytes'],19)
        self.assertNotIn('SECRET','\n'.join(self.rows))

    def test_warmup_recomputed_identity_is_observed_not_reused(self):
        self.begin()
        args=({'X'},self.f.now[0],305)
        self.f.a.rest.warmup(*args);self.f.a.rest.warmup(*args)
        rows=[v for v in self.flush() if v['stage']=='R1010_DIAG_WARMUP']
        self.assertEqual([v['recomputed'] for v in rows],[True,True])
        self.assertEqual([v['identical_request_identity_to_previous'] for v in rows],[False,True])
        self.assertEqual(len(self.f.calls),2)

    def test_observer_does_not_acquire_or_join(self):
        source=Path(d.__file__).read_text()
        self.assertNotIn('.acquire(',source);self.assertNotIn('.join(',source)
        self.assertNotIn('import redis',source);self.assertNotIn('import socket',source)

    def test_restore_wrapped_methods_without_waiting(self):
        self.rec.close()
        self.assertIsNone(self.f.a._ipr_recovery_diag)
        self.assertIsNone(d._ACTIVE)
        self.f.r.deadline.ack()

    def test_journal_existing_eval_bytes_and_original_lock_phase_only(self):
        self.begin()
        before=self.rec.safe('before_journal')
        self.rec.safe('phase','journal.lock_wait',.2)
        self.rec.safe('phase','journal.persist',.5)
        token=self.rec.safe('command_begin','EVAL')
        self.rec.safe('command','eval',.5,('SECRET_SCRIPT',1,'SECRET_KEY','SECRET_VALUE'),{},'OK',token)
        self.rec.safe('after_journal',before)
        row=next(v for v in self.flush() if v['stage']=='R1010_DIAG_JOURNAL')
        self.assertEqual(row['payload_utf8_argument_bytes'],len('SECRET_SCRIPTSECRET_KEYSECRET_VALUE'.encode()))
        self.assertEqual(row['lock_wait_seconds'],.2)
        self.assertNotIn('SECRET','\n'.join(self.rows))

    def test_rest_observer_fault_preserves_original_reply_and_single_read(self):
        self.begin();reads=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):return False
            def read(self):reads.append(1);return b'{}'
        api=NS(opener=lambda *a,**k:Response())
        def call(*args):
            with api.opener() as response:return response.read()
        api.call=call;self.rec.bind_api(api)
        with patch.object(self.rec,'body_read',side_effect=RuntimeError('DO_NOT_EXPOSE')):
            self.assertEqual(api.call('H','P',{}),b'{}')
        self.assertEqual(reads,[1]);self.assertGreater(self.rec.observer_errors,0)

    def test_original_client_exception_is_not_replaced_by_diagnostics(self):
        class Raw:
            def scan(self,*a,**k):raise LookupError('ORIGINAL_SENTINEL')
        client=RedisTelemetry(Raw(),Diagnostics())
        with patch.object(self.rec,'emit',side_effect=RuntimeError('OBSERVER')):
            with self.assertRaisesRegex(LookupError,'ORIGINAL_SENTINEL'):client.scan(match='SECRET:trade:*')

    def test_baseline_fixture_is_exact_saved_source(self):
        baseline=HERE.parents[1]/'baseline'/'r4b'/'recovery_io.py'
        if baseline.exists():self.assertEqual((FIX/'R1010b_recovery_io_baseline.txt').read_bytes(),baseline.read_bytes())
        else:self.assertEqual(hashlib.sha256((FIX/'R1010b_recovery_io_baseline.txt').read_bytes()).hexdigest(),
                              'a5bf963db95c34598f07dd271d251cc521f892022fbf9566ccbf9cc1d118617b')

    def test_outside_window_has_no_per_command_bookkeeping(self):
        self.rec.window=None
        raw=NS(scan=lambda:(0,[]),get=lambda:None)
        client=RedisTelemetry(raw,Diagnostics())
        with patch.object(self.rec,'command_begin',side_effect=AssertionError('unexpected')), \
             patch.object(self.rec,'command',side_effect=AssertionError('unexpected')):
            self.assertEqual(client.scan(),(0,[]))
            self.assertIsNone(client.get())
        self.assertEqual(self.rec.observer_errors,0)
        self.assertEqual(self.rec.outside_summary()['count'],1)
        self.assertEqual([row['stage'] for row in self.flush()],['R1010_DIAG_HEALTH'])
        self.clock.value+=59.9
        self.assertEqual(len(self.flush()),1)

    def test_real_gate_transition_reopens_and_success_sets_tail(self):
        from ipr.scan_health import ScanGate
        self.rec.close()
        self.f.a.scan_gate=ScanGate({'scan_ok':True},clock=self.clock,log=lambda row:None)
        self.rec=d.install(self.f.r,sink=self.rows.append,clock=self.clock,wall=self.clock.wall,start_writer=False)
        self.addCleanup(self.rec.close)
        gate=self.f.a.scan_gate
        gate.invalidate('SCAN_FAILED')
        self.assertFalse(self.rec.in_window())
        gate.success()
        gate.invalidate('TRUST_LOST')
        first=self.rec.window
        run=self.begin()
        run.success=True
        self.rec.safe('end',run)
        self.assertEqual(first[1],1180.)
        self.clock.value=1179.
        self.assertTrue(self.rec.in_window())
        self.clock.value=1180.
        self.assertFalse(self.rec.in_window())
        gate.success()
        gate.invalidate('TRUST_LOST')
        self.assertEqual(self.rec.window,[2,None,0])
        self.rec.close()
        self.assertFalse(self.rec.in_window())

    def test_cap_is_per_window_and_survives_window_overlap(self):
        self.rec.last_health=self.clock()
        with patch.object(d,'MAX_LINES_PER_WINDOW',2):
            for _ in range(3):self.rec.safe('emit','TEST')
            open_window(self.rec)
            for _ in range(3):self.rec.safe('emit','TEST')
            rows=self.flush()
        self.assertEqual([row['window_id'] for row in rows],[1,1,2,2])
        self.assertEqual(self.rec.dropped_window,2)

    def test_control_flow_exceptions_keep_identity_in_measured_client(self):
        for flag in (False,True):
            for cls in (asyncio.CancelledError,SystemExit,KeyboardInterrupt):
                with self.subTest(flag=flag,cls=cls):
                    error=cls()
                    with patch.object(review_flags,'IPR_RECOVERY_DIAG_ENABLED',flag):
                        client=RedisTelemetry(NS(scan=lambda:(0,[])),Diagnostics())
                        if flag:
                            with patch.object(self.rec,'command',side_effect=error):
                                with self.assertRaises(cls) as caught:client.scan()
                        else:
                            client.raw.scan=lambda:(_ for _ in ()).throw(error)
                            with self.assertRaises(cls) as caught:client.scan()
                    self.assertIs(caught.exception,error)

    def test_control_flow_exceptions_escape_recorder_install(self):
        for cls in (asyncio.CancelledError,SystemExit,KeyboardInterrupt):
            error=cls()
            with patch.object(d.Recorder,'bind',side_effect=error):
                with self.assertRaises(cls) as caught:d.install(self.f.r,start_writer=False)
            self.assertIs(error,caught.exception)

    def test_clock_error_does_not_escape_or_change_guard_clock_calls(self):
        self.rec.clock=lambda:(_ for _ in ()).throw(ValueError('CLOCK_OBSERVER'))
        self.f.r.deadline.ack()
        self.assertIsNotNone(self.f.r.deadline.armed)
        self.assertGreaterEqual(self.rec.observer_errors,0)


class DifferentialRecovery(unittest.TestCase):
    def test_off_matches_saved_R1010b_recover_business_stdout_and_state_byte_for_byte(self):
        for reason in (None,'RECOVERY_TIMER_CHANGED','RECOVERY_HOT_STATE_CHANGED','RECOVERY_CANCELLED'):
            with self.subTest(reason=reason):
                baseline,_,_=run_case(False,reason,baseline=True)
                candidate,_,_=run_case(False,reason)
                self.assertEqual(candidate,baseline)

    def test_on_matches_off_state_command_counts_and_original_print_times(self):
        for reason in (None,'RECOVERY_TIMER_CHANGED','RECOVERY_HOT_STATE_CHANGED','RECOVERY_CANCELLED'):
            with self.subTest(reason=reason):
                off,_,_=run_case(False,reason)
                on,rows,_=run_case(True,reason)
                self.assertEqual(on,off)
                self.assertTrue(any(x['stage']=='R1010_DIAG_ATTEMPT_SUMMARY' for x in rows))
                if reason:
                    row=next(x for x in rows if x['stage']=='R1010_DIAG_CANCEL_DETAILS')
                    self.assertEqual(row['reason'],reason)
                    if reason=='RECOVERY_HOT_STATE_CHANGED':self.assertIn('entry_exit_counts',row['hot_fields_observed_after_failure'])

    def test_observer_faults_do_not_change_recovery_or_commands(self):
        for reason in (None,'RECOVERY_TIMER_CHANGED','RECOVERY_CANCELLED'):
            with self.subTest(reason=reason):
                off,_,_=run_case(False,reason)
                on,_,rec=run_case(True,reason,break_observer=True)
                self.assertEqual(on,off)
                self.assertGreater(rec.observer_errors,0)

    def test_actual_trade_outbox_order_and_event_times_identical(self):
        # Original case pipeline and atomic Lua execute over the existing memory model.
        # This contract covers recovery of an active trade, not live Sender/network.
        off,_,_=run_case(False,trade=True)
        on,_,_=run_case(True,trade=True)
        self.assertEqual(on,off)
        self.assertEqual(len(on['messages']),2)
        self.assertIn('CLOSED_STOP',on['canonical'])
        self.assertIn('T1:t',on['canonical'])
        self.assertIn('STOP:t',on['canonical'])

    def test_sequential_recorded_failure_schedules_with_injected_clock(self):
        rows=json.loads((FIX/'recovery_18_19_recorded_schedule.json').read_text())['attempts']
        for day in ('2026-11-18','2026-11-19'):
            schedule=[v for v in rows if v['day']==day]
            with self.subTest(day=day):
                off,_,_=run_case(False,schedule=schedule)
                on,_,_=run_case(True,schedule=schedule)
                self.assertEqual(on,off)
                self.assertEqual([v['result'] for v in on['sequence']],[v['success'] for v in schedule])

    def test_recorded_Nov18_19_fault_schedule_14_attempts_identical(self):
        schedule=json.loads((FIX/'recovery_18_19_recorded_schedule.json').read_text())
        self.assertEqual(len(schedule['attempts']),14)
        for row in schedule['attempts']:
            with self.subTest(day=row['day'],attempt=row['attempt'],log_id=row['log_id']):
                off,_,_=run_case(False,row['cancel_reason'])
                on,_,_=run_case(True,row['cancel_reason'])
                self.assertEqual(on,off)
                self.assertEqual(on['result'],row['success'])
                if not row['success']:self.assertEqual(on['reason'],row['cancel_reason'])


if __name__=='__main__':unittest.main()
