"""Option 3b contracts over local memory only; no network clients.

Endpoint equality is NOT an atomic snapshot. Tests explicitly retain ABA,
same-key mutation, and final-enumeration-to-publish counterexamples.
"""
import ast,contextlib,copy,fnmatch,io,json,types,unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from ipr import review_flags
from r4b import recovery_io as rio
from state_store import base_record,canonical_json,key_trade,key_opportunity
from test_direct import OP
from test_r1010 import ready,add_trade,add_opp
from test_recovery_diag import run_case
from test_recovery_regression import CaseFixture

FLAG='RECOVERY_BOUNDARY_ENUMERATION_ENABLED'

def terminal_opportunity(i):
    row=base_record('opportunity','2026-10-27','O%04d'%i,'OPPORTUNITY_EXPIRED',OP.isoformat())
    row.update(e_id=None,b_id=None,entry_trigger_ts=None,delta_seconds=None,first_event='E',
               expiry=OP.isoformat(),terminal_reason='OPPORTUNITY_EXPIRED',leader_generation=1,worker_instance_id='w')
    return key_opportunity(row['session'],row['symbol']),canonical_json(row)

def terminal_trade(i):
    row=CaseFixture().trade();row.update(trade_id='closed-%04d'%i,state='CLOSED_STOP')
    return key_trade(row['trade_id']),canonical_json(row)

class Memory:
    def __init__(self,n=0,duplicate=False):
        self.values=dict(terminal_opportunity(i) for i in range(n))
        self.counts=Counter();self.duplicate=duplicate;self.before_mget=None
    def scan(self,cursor=0,match='*',count=500):
        self.counts['SCAN']+=1
        keys=sorted(k for k in self.values if fnmatch.fnmatchcase(k,match))
        end=min(int(cursor)+count,len(keys));part=keys[int(cursor):end]
        if self.duplicate and int(cursor) and keys:part=[keys[0]]+part
        return end if end<len(keys) else 0,part
    def mget(self,keys):
        self.counts['MGET']+=1
        if self.before_mget:self.before_mget(keys)
        return [self.values.get(k) for k in keys]

def bare(memory):
    run=object.__new__(rio.RecoveryRun)
    run.bridge=NS(r=memory);run.boundary_keys={};run.boundary_enumeration=True;run.membership_changes=None
    run.capture_boundary_membership()
    return run

class AuditWire:
    """Matching-key pagination fixture, not a model of a Redis server index."""
    def __init__(self,f):
        self.f=f;self.raw=f.a.bridge.r.r;self.counts=Counter();self.zero=0;self.nonzero=0
        self.old={n:getattr(self.raw,n) for n in ('scan','mget','get','eval')}
        for n in self.old:setattr(self.raw,n,getattr(self,n))
        self.after_scan=None
    def check(self,name):
        if self.f.a.bridge.p._decision_lock._is_owned() or self.f.a.control_lock._is_owned():
            raise AssertionError('TEST_IO_UNDER_LIVE_LOCK:'+name)
        self.counts[name.upper()]+=1
    def scan(self,cursor=0,match='*',count=500):
        self.check('scan')
        keys=sorted(k for k in self.raw.state['values'] if fnmatch.fnmatchcase(k,match))
        end=min(int(cursor)+count,len(keys));c=end if end<len(keys) else 0
        self.zero+=int(c==0);self.nonzero+=int(c!=0)
        result=(c,keys[int(cursor):end])
        if self.after_scan:self.after_scan(match,c)
        return result
    def mget(self,keys):
        self.check('mget');return [self.raw.state['values'].get(k) for k in keys]
    def get(self,key):self.check('get');return self.raw.state['values'].get(key)
    def eval(self,*a,**kw):self.check('eval');return self.old['eval'](*a,**kw)

def integration(enabled=True,trades=18,opps=633,mutate=None,live_trade=False,live_opp=False,reason=None):
    with patch.object(review_flags,FLAG,enabled):
        f=ready()
        # Seed fixture state directly, outside the measured command interval.
        raw=f.a.bridge.r.r;prefix=f.a.bridge.r.prefix
        for i in range(trades):
            key,value=terminal_trade(i);raw.state['values'][prefix+key]=value
        for i in range(opps):
            key,value=terminal_opportunity(i);raw.state['values'][prefix+key]=value
        if live_trade:
            add_trade(f);f.src.ts=[{'t':OP.isoformat(),'p':10.6},
                                   {'t':(OP+__import__('datetime').timedelta(seconds=1)).isoformat(),'p':9.4}]
        if live_opp:add_opp(f)
        wire=AuditWire(f)
        oldwarm=f.src.warmup
        def warm(*args):
            if f.a.bridge.p._decision_lock._is_owned() or f.a.control_lock._is_owned():
                raise AssertionError('TEST_WARMUP_UNDER_LIVE_LOCK')
            result=oldwarm(*args)
            # Existing timer seam makes both conditional reads execute: seven checkpoints.
            f.a.bridge.p.expire_entry_opportunities(f.now[0])
            if reason=='RECOVERY_CANCELLED':f.a.close_eligibility('SUBSCRIPTION_CHANGE_REQUIRES_RECOVERY')
            return result
        f.src.warmup=warm
        original_append=f.a.checkpoint.append_epoch
        def append(event):
            result=original_append(event)
            if event.get('kind')=='B_EVIDENCE_READY':
                if mutate:mutate(f,wire)
                if reason=='RECOVERY_TIMER_CHANGED':f.a.bridge.p.expire_entry_opportunities(f.now[0])
                if reason=='RECOVERY_HOT_STATE_CHANGED':f.a.bridge.p.halted.add('Y')
            return result
        f.a.checkpoint.append_epoch=append
        with contextlib.redirect_stdout(io.StringIO()):result=f.a.recover(f.now[0])
        return f,wire,result

class BoundaryRecords(unittest.TestCase):
    def test_boundary_flag_is_literal_bool_matching_loaded_source(self):
        # Both review-OFF and activation-ON are intentional code constants.
        # Validate the literal rather than requiring the OFF review default.
        module=ast.parse(Path(review_flags.__file__).read_text())
        node=next(n for n in module.body if isinstance(n,ast.Assign)
                  and any(isinstance(t,ast.Name) and t.id==FLAG for t in n.targets))
        self.assertIs(type(node.value.value),bool)
        self.assertIs(getattr(review_flags,FLAG),node.value.value)
    def test_sizes_and_zero_empty_and_nonempty(self):
        for n in (0,1,500,501,633):
            with self.subTest(n=n):
                m=Memory(n);r=bare(m);pattern=r.BOUNDARY_PATTERNS[1][1]
                rows=r.records(pattern,'opportunity');self.assertEqual(len(rows),n)
                for _ in range(6):self.assertEqual(r.records(pattern,'opportunity'),rows)
                r.validate_boundary_membership()
                self.assertEqual(m.counts['SCAN'],2*(1+max(1,(n+499)//500)))
                self.assertEqual(m.counts['MGET'],7*((n+499)//500))
    def test_duplicate_keys_across_pages_are_deduplicated(self):
        m=Memory(633,duplicate=True);r=bare(m)
        self.assertEqual(len(r.records(r.BOUNDARY_PATTERNS[1][1],'opportunity')),633)
        r.validate_boundary_membership()
    def test_deleted_between_scan_and_mget_is_not_hidden(self):
        m=Memory(1);r=bare(m);m.values.clear()
        with self.assertRaisesRegex(rio.RecoveryReadFailed,'RECOVERY_CANONICAL_DISAPPEARED'):
            r.records(r.BOUNDARY_PATTERNS[1][1],'opportunity')
    def test_mget_length_is_validated(self):
        m=Memory(1);r=bare(m);m.mget=lambda keys:[]
        with self.assertRaisesRegex(rio.RecoveryReadFailed,'RECOVERY_MGET_LENGTH'):r.records(r.BOUNDARY_PATTERNS[1][1],'opportunity')
    def test_corrupt_value_is_not_hidden(self):
        m=Memory(1);r=bare(m);m.values[next(iter(m.values))]='{' 
        with self.assertRaisesRegex(rio.RecoveryReadFailed,'RECOVERY_CANONICAL_INVALID'):r.records(r.BOUNDARY_PATTERNS[1][1],'opportunity')
    def test_wrong_identity_is_not_hidden(self):
        m=Memory(1);r=bare(m);key=next(iter(m.values));row=json.loads(m.values[key]);row['symbol']='OTHER';m.values[key]=canonical_json(row)
        with self.assertRaisesRegex(rio.RecoveryReadFailed,'RECOVERY_CANONICAL_INVALID'):r.records(r.BOUNDARY_PATTERNS[1][1],'opportunity')
    def test_added_key_cancels_with_counts_only(self):
        m=Memory(1);r=bare(m);k,v=terminal_opportunity(2);m.values[k]=v
        with self.assertRaisesRegex(rio.RecoveryCancelled,'RECOVERY_MEMBERSHIP_CHANGED'):r.validate_boundary_membership()
        self.assertEqual(r.membership_changes,{'trade':{'added':0,'removed':0},'opportunity':{'added':1,'removed':0}})
        self.assertNotIn(k,json.dumps(r.membership_changes))
    def test_removed_key_cancels_with_counts(self):
        m=Memory(1);r=bare(m);m.values.clear()
        with self.assertRaisesRegex(rio.RecoveryCancelled,'RECOVERY_MEMBERSHIP_CHANGED'):r.validate_boundary_membership()
        self.assertEqual(r.membership_changes['opportunity']['removed'],1)
    def test_add_then_remove_ABA_is_not_detected(self):
        m=Memory(1);r=bare(m);k,v=terminal_opportunity(2);m.values[k]=v;del m.values[k]
        r.validate_boundary_membership();self.assertIsNone(r.membership_changes)
    def test_delete_then_recreate_same_name_is_not_detected(self):
        m=Memory(1);r=bare(m);k=next(iter(m.values));v=m.values.pop(k);m.values[k]=v
        r.validate_boundary_membership();self.assertIsNone(r.membership_changes)
    def test_same_key_value_reread_is_fresh_not_a_membership_change(self):
        m=Memory(1);r=bare(m);p=r.BOUNDARY_PATTERNS[1][1]
        a=r.records(p,'opportunity');k=next(iter(m.values));row=json.loads(m.values[k]);row['updated_at']='2026-10-28T01:00:00+00:00';m.values[k]=canonical_json(row)
        b=r.records(p,'opportunity');self.assertNotEqual(a,b);r.validate_boundary_membership()
    def test_decoded_objects_are_not_shared_between_checkpoints(self):
        m=Memory(1);r=bare(m);p=r.BOUNDARY_PATTERNS[1][1]
        a=r.records(p,'opportunity');a[0]['symbol']='MUTATED'
        self.assertNotEqual(r.records(p,'opportunity')[0]['symbol'],'MUTATED')
    def test_uncaptured_scope_is_rejected(self):
        r=bare(Memory())
        with self.assertRaisesRegex(rio.RecoveryReadFailed,'RECOVERY_MEMBERSHIP_SCOPE_UNCAPTURED'):r.records('unknown','unknown')

class BoundaryIntegration(unittest.TestCase):
    def test_seven_checkpoints_21_to_6_SCAN_MGET_unchanged(self):
        off,wo,ro=integration(False);on,wn,rn=integration(True)
        self.assertTrue(ro,off.a.report);self.assertTrue(rn,on.a.report)
        self.assertEqual((wo.counts['SCAN'],wn.counts['SCAN']),(21,6))
        self.assertEqual((wo.zero,wo.nonzero,wn.zero,wn.nonzero),(14,7,4,2))
        io0=off.a.measure.recovery_io.last[-1]['io'] if hasattr(off.a.measure.recovery_io,'last') else off.a.measure.recovery_io.snapshot()['last_recoveries'][-1]['io']
        io1=on.a.measure.recovery_io.snapshot()['last_recoveries'][-1]['io']
        self.assertEqual((io0['redis.mget']['count'],io1['redis.mget']['count']),(21,21))
    def test_added_key_before_final_check_blocks_trust(self):
        def added(f,w):k,v=terminal_opportunity(999);f.a.bridge.r.set(k,v)
        f,w,result=integration(mutate=added)
        self.assertFalse(result);self.assertEqual(f.a.report['reason'],'RECOVERY_MEMBERSHIP_CHANGED');self.assertFalse(f.a.transport_trusted)
    def test_own_trade_events_create_outbox_not_canonical_members(self):
        f,w,result=integration(live_trade=True)
        self.assertTrue(result,f.a.report)
        self.assertEqual(json.loads(f.a.bridge.r.get(key_trade('t')))['state'],'CLOSED_STOP')
    def test_own_opportunity_expiry_updates_value_not_membership(self):
        f,w,result=integration(live_opp=True)
        self.assertTrue(result,f.a.report)
        self.assertEqual(json.loads(f.a.bridge.r.get(key_opportunity('2026-10-28','X')))['state'],'ENTRY_EXPIRED')
    def test_final_hot_state_guard_still_rejects(self):
        f,w,result=integration(reason='RECOVERY_HOT_STATE_CHANGED')
        self.assertFalse(result);self.assertEqual(f.a.report['reason'],'RECOVERY_HOT_STATE_CHANGED')
    def test_final_timer_guard_still_rejects(self):
        f,w,result=integration(reason='RECOVERY_TIMER_CHANGED')
        self.assertFalse(result);self.assertEqual(f.a.report['reason'],'RECOVERY_TIMER_CHANGED')
    def test_cancellation_during_warmup_still_rejects(self):
        f,w,result=integration(reason='RECOVERY_CANCELLED')
        self.assertFalse(result);self.assertEqual(f.a.report['reason'],'RECOVERY_CANCELLED')
    def test_no_io_or_warmup_under_live_locks(self):
        f,w,result=integration(live_trade=True,live_opp=True)
        self.assertTrue(result,f.a.report);self.assertGreater(w.counts['EVAL'],0)
    def test_same_key_change_after_last_value_read_can_escape_membership_check(self):
        def mutate(f,w):
            k=key_opportunity('2026-10-27','O0000');row=json.loads(f.a.bridge.r.get(k));row['updated_at']='2026-10-28T01:00:00+00:00';f.a.bridge.r.set(k,canonical_json(row))
        f,w,result=integration(mutate=mutate)
        self.assertTrue(result,f.a.report)  # Counterexample, not a guarantee of correctness.
    def test_post_final_enumeration_addition_can_escape(self):
        def mutate(f,w):
            def after(pattern,cursor):
                if w.counts['SCAN']==6 and cursor==0:
                    k,v=terminal_opportunity(999);f.a.bridge.r.set(k,v)
            w.after_scan=after
        f,w,result=integration(mutate=mutate)
        self.assertTrue(result,f.a.report)  # Final-enumeration-to-publish gap remains.
    def test_membership_diagnostic_contains_reason_and_counts_no_keys(self):
        from ipr import recovery_diag as d
        from test_recovery_diag import Clock,open_window
        with patch.object(review_flags,FLAG,True),patch.object(review_flags,'IPR_RECOVERY_DIAG_ENABLED',True):
            f=ready();clock=Clock();rows=[]
            rec=d.install(f.r,sink=rows.append,clock=clock,wall=clock.wall,start_writer=False);open_window(rec)
            run=rio.RecoveryRun(f.a,f.a._recovery_io,f.now[0]);run.cancel_reason='RECOVERY_MEMBERSHIP_CHANGED';run.membership_changes={'trade':{'added':2,'removed':1},'opportunity':{'added':0,'removed':0}}
            try:
                rec.safe('begin',run);rec.safe('failure',run);rec.safe('end',run);rec.drain()
                parsed=[json.loads(x) for x in rows];row=next(x for x in parsed if x['stage']=='R1010_DIAG_CANCEL_DETAILS')
                self.assertEqual(row['reason'],'RECOVERY_MEMBERSHIP_CHANGED');self.assertEqual(row['membership_changes'],run.membership_changes)
            finally:rec.close();d._LOCAL.attempt=None

class BoundaryDisabledDifferential(unittest.TestCase):
    def test_saved_18_19_20_sequences_base_off_on(self):
        from boundary_replay_support import replay
        for day in ('2026-11-18','2026-11-19','2026-11-20'):
            with self.subTest(day=day):
                baseline=replay(day,'base');off=replay(day,'off');on=replay(day,'on')
                self.assertEqual(off['state_sha256'],baseline['state_sha256'])
                self.assertEqual(on['state_sha256'],off['state_sha256'])
                self.assertEqual([x['SCAN_commands'] for x in baseline['rows']],[x['SCAN_commands'] for x in off['rows']])
                for x,y in zip(on['rows'],off['rows']):
                    if x['reason']=='RECOVERY_CANCELLED':
                        # Enumeration now follows warmup and its original guard.
                        self.assertEqual((x['SCAN_commands'],y['SCAN_commands']),(0,0))
                    else:self.assertEqual((x['SCAN_commands'],y['SCAN_commands']),(6,21))

    def test_disabled_matches_e35_saved_source_bytewise_outputs(self):
        mod=types.ModuleType('r4b._boundary_e35_baseline');mod.__package__='r4b'
        path=Path(__file__).parent/'fixtures/e35_recovery_io_baseline.txt'
        exec(compile(path.read_text(),rio.__file__,'exec'),mod.__dict__)
        def baseline_recover(adapter,now):
            mod.time=rio.time;mod.datetime=rio.datetime
            return mod.recover(adapter,now)
        for diagnostic in (False,True):
            for why in (None,'RECOVERY_TIMER_CHANGED','RECOVERY_HOT_STATE_CHANGED','RECOVERY_CANCELLED'):
                with self.subTest(diagnostic=diagnostic,reason=why),patch.object(review_flags,FLAG,False):
                    with patch.object(rio,'recover',baseline_recover):expected,erows,_=run_case(diagnostic,why)
                    actual,arows,_=run_case(diagnostic,why)
                    self.assertEqual(actual,expected);self.assertEqual(arows,erows)


def negative_case(flag,baseline=False):
    """Real negative publication with a new canonical member after checkpoints."""
    from test_recovery_diag import Clock,capture,open_window
    from ipr import recovery_diag as diagnostic
    from r4b.cases import CaseBarrier
    from datetime import datetime,timezone
    clock=Clock();module=rio
    if baseline:
        module=types.ModuleType('r4b._negative_e35');module.__package__='r4b'
        source=(Path(__file__).parent/'fixtures/e35_recovery_io_baseline.txt').read_text()
        exec(compile(source,rio.__file__,'exec'),module.__dict__)
    fixed=type('FixedUTC',(),{'now':staticmethod(lambda tz:datetime(2026,11,20,tzinfo=timezone.utc))})
    rows=[];out=io.StringIO();observations={}
    with patch.object(review_flags,FLAG,flag),patch.object(review_flags,'IPR_RECOVERY_DIAG_ENABLED',True), \
         patch.object(rio,'time',NS(monotonic=clock,sleep=lambda _:None)),patch.object(rio,'datetime',fixed), \
         patch('ipr.observability.time.monotonic',clock):
        if module is not rio:module.time=rio.time;module.datetime=fixed
        f=ready();f.r.boot_number=2;f.a._recovery_io.metrics.clock=clock
        wire=AuditWire(f)
        rec=diagnostic.install(f.r,sink=rows.append,clock=clock,wall=clock.wall,start_writer=False)
        open_window(rec)
        original=CaseBarrier.run
        def fail_after_checkpoints(barrier,*args):
            result=original(barrier,*args)
            barrier.bridge.p._entry_uncertain.add('Y')
            barrier.persisted_R.add('Y')
            key,value=terminal_opportunity(999)
            barrier.bridge.r.set(key,value)  # Same mutation for original/off/on.
            result['reconciled']=False
            result['issues']=[{'reason':'FIXTURE_ORIGINAL_CONTINUITY_FAILURE'}]
            observations['scan_before_negative_publish']=wire.counts['SCAN']
            return result
        try:
            with patch.object(CaseBarrier,'run',fail_after_checkpoints),contextlib.redirect_stdout(out):
                success=module.recover(f.a,f.now[0])
            rec.drain()
            state=capture(f)
            state.update(success=success,reason=f.a.report['reason'],business_stdout=out.getvalue())
            observations.update(scan_after=wire.counts['SCAN'],state=state,rows=[json.loads(x) for x in rows])
        finally:rec.close();diagnostic._LOCAL.attempt=None
    return observations


class NegativePublicationContracts(unittest.TestCase):
    def test_publish_negative_source_is_byte_identical_to_saved_original(self):
        saved=(Path(__file__).parent/'fixtures/e35_recovery_io_baseline.txt').read_text()
        current=Path(rio.__file__).read_text()
        def function_bytes(text):
            tree=ast.parse(text)
            node=next(x for x in ast.walk(tree) if isinstance(x,ast.FunctionDef) and x.name=='publish_negative_cases')
            return ast.get_source_segment(text,node).encode()
        self.assertEqual(function_bytes(saved),function_bytes(current))

    def test_disabled_negative_outputs_match_original_byte_for_byte(self):
        original=negative_case(False,baseline=True);disabled=negative_case(False)
        self.assertEqual(disabled,original)

    def test_enabled_negative_publishes_same_cases_and_pins_after_membership_change(self):
        original=negative_case(False,baseline=True);enabled=negative_case(True)
        # Canonical/hot publication content must match. I/O counters deliberately
        # differ when enumeration reuse is enabled; assert reasons separately.
        for key in enabled['state']:
            if key!='business_stdout':self.assertEqual(enabled['state'][key],original['state'][key],key)
        self.assertIn('Y',enabled['state']['pins'])
        self.assertEqual(enabled['scan_after'],enabled['scan_before_negative_publish'])

    def test_negative_original_failure_reason_and_diagnostic_summary_are_preserved(self):
        enabled=negative_case(True)
        self.assertEqual(enabled['state']['reason'],'OPEN_CASE_CONTINUITY_UNPROVEN')
        events=[json.loads(x) for x in enabled['state']['business_stdout'].splitlines() if x.startswith('{"stage": "R1010_RECOVERY"')]
        self.assertEqual(events[-1]['failure_reason'],'OPEN_CASE_CONTINUITY_UNPROVEN')
        summary=next(x for x in enabled['rows'] if x['stage']=='R1010_DIAG_ATTEMPT_SUMMARY')
        self.assertIs(summary['success'],False)
        self.assertNotIn('RECOVERY_MEMBERSHIP_CHANGED',json.dumps(enabled))

    def test_warmup_cancellation_and_exception_add_zero_scan(self):
        for enabled in (False,True):
            with self.subTest(enabled=enabled,kind='cancel'):
                f,w,result=integration(enabled,reason='RECOVERY_CANCELLED')
                self.assertFalse(result);self.assertEqual(w.counts['SCAN'],0)
            with self.subTest(enabled=enabled,kind='exception'),patch.object(review_flags,FLAG,enabled):
                f=ready();wire=AuditWire(f)
                f.src.warmup=lambda *args:(_ for _ in ()).throw(ValueError('WARMUP_FIXTURE'))
                with contextlib.redirect_stdout(io.StringIO()):result=f.a.recover(f.now[0])
                self.assertFalse(result);self.assertEqual(wire.counts['SCAN'],0)
