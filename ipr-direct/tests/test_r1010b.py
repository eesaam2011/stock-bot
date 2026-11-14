"""R10.10b: real BAR-flow overlap, counted fake commands, and discrete retries.
No sockets/Redis clients; signal predicates are injected deterministic fixtures.
"""
import asyncio
import copy
import contextlib
import io
import json
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch

from ipr import review_flags
import r4b.recovery_io as rio
import r4b.runtime as scheduler
from r4a.guards import AckTrustDeadline, SafetyStop
from r1010b_support import flow, fixture, cost_profile, NativeWaves
from test_r3 import receive


class FlowTests(unittest.TestCase):
    def test_real_5_10_30_second_recovery_with_500_symbols_and_430_BAR_per_minute(self):
        async def run():
            results=await asyncio.gather(*(flow(t,overlap) for t in (5,10,30) for overlap in (False,True)))
            for row in results:
                self.assertTrue(row['trusted_at_end'],row)
                self.assertEqual(row['scope_symbols'],500)
                self.assertEqual(row['fail_closed_count'],0,row)
                self.assertEqual(row['new_B_engine_calls_during_untrusted'],0,row)
                self.assertGreaterEqual(row['untrusted_seconds'],row['hold_seconds'])
                self.assertGreaterEqual(row['received_BAR'],int(row['untrusted_seconds']*430/60)-1)
                if row['mode']=='gated_BAR_only':
                    self.assertEqual(row['attempt_count'],1,row)
                    self.assertEqual(row['E_crossings'],0,row)
                    self.assertEqual(row['cancellations'],{},row)
                else:
                    self.assertEqual(row['attempt_count'],2,row)
                    self.assertEqual(row['cancellations'],{'RECOVERY_HOT_STATE_CHANGED':1},row)
                    self.assertEqual(row['E_crossings'],1,row)
                    self.assertTrue(row['new_opportunity_observed'],row)
                    self.assertGreaterEqual(row['untrusted_seconds'],2*row['hold_seconds'])
            print(json.dumps({'test':'R1010B_REAL_FLOW','results':results}))
        asyncio.run(run())

    def test_BAR_cannot_reach_B_engine_while_transport_is_untrusted(self):
        f=fixture(n=2,b_symbols=('S001',))
        before=copy.deepcopy(f.a.bridge.p._entry_opportunities)
        calls=len(f.b_engine_calls)
        f.a.close_eligibility('FIXTURE_TRUST_LOST')
        # Existing proof=true intentionally reaches LiveAdapter's trust check.
        msg=dict(S='X',T='b',t='2026-10-28T13:34:00+00:00',o=10.,h=11.,l=9.,c=10.,v=100)
        f.a.on_message(msg,f.now[0])
        self.assertEqual(len(f.b_engine_calls),calls)
        self.assertEqual(f.a.bridge.p._entry_opportunities,before)
        self.assertFalse(f.a.transport_trusted)

    def test_native_guard_false_prevents_new_native_admission(self):
        from dintegration.native_timing import native_guard
        f=fixture(n=2)
        f.a.close_eligibility('FIXTURE_TRUST_LOST')
        calls=[]
        if native_guard(f.r,'trusted',f.a.transport_trusted,f.now[0]):
            calls.append(f.a.bridge.native_cycle(f.symbols,f.now[0],True))
        self.assertEqual(calls,[])


class CostTests(unittest.TestCase):
    def test_observer_additions_do_not_add_Redis_commands_relative_to_R1010(self):
        import types
        from pathlib import Path
        source=Path(__file__).with_name('fixtures').joinpath('R1010_recovery_io_source.txt').read_text()
        baseline=types.ModuleType('r4b._baseline_r1010');baseline.__package__='r4b'
        exec(compile(source,'R1010_recovery_io_source.txt','exec'),baseline.__dict__)
        for total,t,o in ((29,27,0),(4100,27,100),(4100,1001,1001)):
            old=cost_profile(total,t,o,recovery_impl=baseline)
            new=cost_profile(total,t,o)
            self.assertEqual(old['commands'],new['commands'])
            self.assertEqual(old['leader_identity_MGET'],new['leader_identity_MGET'])
            self.assertEqual(old['success'],new['success'])

    def test_count_commands_for_10_enumerations_and_multiple_SCAN_pages(self):
        results=[]
        for total,trades,opps in ((29,27,0),(4100,27,100),(4100,1001,1001)):
            pair=[cost_profile(total,trades,opps,flag=flag) for flag in (False,True)]
            old,new=pair
            self.assertTrue(old['success'] and new['success'],pair)
            expected_scan=10*((total+499)//500)
            self.assertEqual(old['commands']['SCAN'],expected_scan)
            self.assertEqual(new['commands']['SCAN'],expected_scan)
            added=new['leader_identity_MGET']-old['leader_identity_MGET']
            self.assertGreater(added,0)
            # No new data SCAN or MGET; the counted delta consists of leadership identities.
            self.assertEqual(sum(new['commands'].values())-sum(old['commands'].values()),added)
            self.assertEqual(new['require_current_calls'],new['leader_identity_MGET'])
            self.assertEqual(new['checks_calls']+1,new['require_current_calls'])
            rows=[]
            for delay in (.05,.3,1.):
                rows.append({'seconds_per_command':delay,'extra_guard_seconds':added*delay,
                    'new_all_command_seconds':sum(new['commands'].values())*delay,
                    'old_all_command_seconds':sum(old['commands'].values())*delay,
                    'remaining_of_fresh_ACK_120':120-sum(new['commands'].values())*delay})
            results.append({'R109_false_original':old,'R1010b_true':new,
                            'additional_leader_MGET':added,'latency_scenarios':rows})
        print(json.dumps({'test':'R1010B_GUARD_COMMAND_COST','profiles':results}))

    def test_real_50ms_delay_matches_counted_command_model(self):
        pair=[cost_profile(29,27,0,latency=.05,real=True,flag=flag) for flag in (False,True)]
        for row in pair:
            self.assertTrue(row['success'],row)
            self.assertGreaterEqual(row['elapsed_wall_seconds'],row['sum_command_delay_seconds'])
        print(json.dumps({'test':'R1010B_REAL_50MS','results':pair}))

    def test_ACK_120_guard_is_unchanged_and_not_a_ten_attempt_limit(self):
        now=[0.];deadline=AckTrustDeadline(lambda:now[0]);deadline.ack()
        for attempts in range(1,4):
            now[0]=attempts*30;deadline.tick(False)
        now[0]=120
        with self.assertRaisesRegex(SafetyStop,'WS_ACK_UNTRUSTED_TIMEOUT'):deadline.tick(False)
        now[0]=121;deadline.tick(True)
        self.assertIsNone(deadline.armed)


class RetryTests(unittest.TestCase):
    def test_one_pre_admitted_native_cycle_can_cancel_ten_recoveries(self):
        results=[]
        for duration in (5,10,30):
            f=fixture(n=500,b_symbols=tuple('S%03d'%i for i in range(1,11)))
            native=NativeWaves(f);native.start()
            f.mono[0]=0.;f.a.measure.recovery_io.clock=lambda:f.mono[0]
            original=f.src.warmup;attempt=[0]
            def warm(*args):
                value=copy.deepcopy(original(*args))
                f.mono[0]+=duration
                native.next(attempt[0]);attempt[0]+=1
                return value
            f.src.warmup=warm
            output=io.StringIO()
            try:
                with patch.object(rio,'time',NS(monotonic=lambda:f.mono[0],sleep=time.sleep)),contextlib.redirect_stdout(output):
                    reasons=[]
                    for i in range(10):
                        self.assertFalse(f.a.recover(f.now[0]))
                        reasons.append(f.a.report['reason'])
                self.assertEqual(reasons,['RECOVERY_HOT_STATE_CHANGED']*10)
                self.assertFalse(f.a.transport_trusted)
                snap=f.a.measure.recovery_io.snapshot()
                self.assertEqual(snap['consecutive_failures'],10)
                warnings=[json.loads(line) for line in output.getvalue().splitlines() if 'R1010B_RECOVERY_FAILURE_STREAK' in line]
                self.assertEqual(len(warnings),1)
                # Once the single in-flight cycle finishes, no new one may start
                # while untrusted. A stable retry can publish all retained cases.
                native.thread.join(3)
                f.src.warmup=original
                self.assertTrue(f.a.recover(f.now[0]),f.a.report)
                results.append({'attempt_hold_seconds':duration,'failed_attempts':10,'reasons':reasons,
                    'one_native_cycle_E':native.result['early_core_persisted'],'success_attempt':11,
                    'injected_untrusted_seconds_before_success':10*duration,'warning':warnings[0],
                    'limit':'controlled serialized waves; no BAR/case_loop/ACK watcher in this stress; 300s violates fresh ACK120'})
            finally:native.close()
        print(json.dumps({'test':'R1010B_REPEATED_HOT_STATE_WAVES','results':results}))

    def run_failures(self,duration,limit=10,watch=False):
        async def run():
            f=fixture(n=2)
            # A fresh ACK and a trust loss, both clocks injected for a discrete-event test.
            f.a.close_eligibility('FIXTURE_TRUST_LOST')
            f.mono[0]=0.;f.a._recovery_io.metrics.clock=lambda:f.mono[0]
            start_wall=f.now[0]
            calls=[];sleeps=[];deadline=AckTrustDeadline(lambda:f.mono[0]);deadline.ack()
            async def sleep(delay):
                sleeps.append(delay);f.mono[0]+=delay
                f.now[0]=start_wall+timedelta(seconds=f.mono[0])
                await asyncio.sleep(0)
            def warm(*args):
                f.mono[0]+=duration
                f.now[0]=start_wall+timedelta(seconds=f.mono[0])
                raise TimeoutError('fixture')
            f.src.warmup=warm
            old=f.a.recover
            def recover(at):
                calls.append(f.mono[0]);value=old(at)
                if watch:deadline.tick(f.a.transport_trusted)
                if len(calls)>=limit:f.r.stop.set()
                else:f.r.wake.set()  # coalesced new BAR witness, not an autonomous retry
                return value
            f.a.recover=recover;f.r.wake.set()
            caught=None
            local_time=NS(monotonic=lambda:f.mono[0],sleep=time.sleep)
            output=io.StringIO()
            with patch.object(rio,'time',local_time),patch.object(scheduler,'asyncio',NS(sleep=sleep,to_thread=asyncio.to_thread)),contextlib.redirect_stdout(output):
                try:await f.r.recovery_loop()
                except SafetyStop as exc:caught=str(exc)
            rows=[json.loads(s) for s in output.getvalue().splitlines() if s.startswith('{')]
            warnings=[r for r in rows if r.get('stage')=='R1010B_RECOVERY_FAILURE_STREAK']
            return {'duration':duration,'starts':calls,'sleep':sleeps,'snapshot':f.a.measure.recovery_io.snapshot(),
                'warnings':warnings,'fatal':caught,'trusted':f.a.transport_trusted,'elapsed':f.mono[0]}
        return asyncio.run(run())

    def test_retry_spacing_is_between_starts_not_one_second_after_completion(self):
        row=self.run_failures(.2,3)
        self.assertEqual(row['starts'],[0.,1.,2.])
        self.assertEqual(row['sleep'],[.8,.8])
        row=self.run_failures(5,3)
        self.assertEqual(row['starts'],[0.,5.,10.])
        self.assertEqual(row['sleep'],[0.,0.])

    def test_ten_failures_are_counted_and_logged_without_adding_retry_policy(self):
        results=[]
        for duration in (5,10,30):
            row=self.run_failures(duration)
            self.assertEqual(row['snapshot']['consecutive_failures'],10)
            self.assertEqual(row['snapshot']['observer_errors'],0)
            self.assertEqual(len(row['warnings']),1)
            self.assertFalse(row['trusted'])
            self.assertIsNone(row['fatal'])
            results.append(row)
        print(json.dumps({'test':'R1010B_TEN_FAILURES_WITHOUT_WATCH_COUNTERFACTUAL','results':results,
                          'limits':'30s x10 is NOT reachable under fresh ACK120 guard; separate watcher test applies'}))

    def test_fresh_ACK_supervision_stops_slow_retries_at_120(self):
        results=[]
        for duration in (5,10,30):
            row=self.run_failures(duration,limit=100,watch=True)
            self.assertEqual(row['fatal'],'WS_ACK_UNTRUSTED_TIMEOUT')
            self.assertEqual(row['elapsed'],120.)
            self.assertEqual(len(row['starts']),120//duration)
            results.append({'duration':duration,'attempts_before_guard':len(row['starts']),
                            'elapsed':row['elapsed'],'fatal':row['fatal']})
        print(json.dumps({'test':'R1010B_ACK_SUPERVISED_DISCRETE_RETRIES','results':results}))

    def test_failure_without_new_witness_does_not_autonomously_retry(self):
        async def run():
            f=fixture(n=2);f.a.close_eligibility('FIXTURE_TRUST_LOST')
            f.src.warmup=lambda *a:(_ for _ in ()).throw(TimeoutError('fixture'))
            f.r.wake.set();task=asyncio.create_task(f.r.recovery_loop())
            try:
                limit=time.monotonic()+3
                while f.a.measure.recovery_io.consecutive_failures<1:
                    self.assertLess(time.monotonic(),limit);await asyncio.sleep(.005)
                await asyncio.sleep(.05)
                self.assertEqual(f.r.attempts,1)
                self.assertFalse(f.r.wake.is_set())
                self.assertFalse(f.a.transport_trusted)
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
        asyncio.run(run())

    def test_first_success_resets_consecutive_counter_but_keeps_total(self):
        f=fixture(n=2)
        old=f.src.warmup
        f.src.warmup=lambda *a:(_ for _ in ()).throw(TimeoutError('fixture'))
        for _ in range(2):self.assertFalse(f.a.recover(f.now[0]))
        failures=f.a.measure.recovery_io.snapshot()['failures']
        f.src.warmup=old
        self.assertTrue(f.a.recover(f.now[0]))
        snap=f.a.measure.sample()['recovery_io']
        self.assertEqual(snap['consecutive_failures'],0)
        self.assertEqual(snap['failures'],failures)
        self.assertEqual(snap['max_consecutive_failures'],2)

    def test_streak_warning_observer_failure_cannot_replace_application_result(self):
        f=fixture(n=2)
        f.a.measure.recovery_io.consecutive_failures=9
        f.src.warmup=lambda *a:(_ for _ in ()).throw(TimeoutError('fixture'))
        with patch('builtins.print',side_effect=OSError('fixture')):
            self.assertFalse(f.a.recover(f.now[0]))
        self.assertGreater(f.a.measure.recovery_io.observer_errors,0)
        self.assertFalse(f.a.transport_trusted)
