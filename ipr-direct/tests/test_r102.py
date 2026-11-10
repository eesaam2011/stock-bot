"""Measured phases and observational equivalence. Offline synthetic I/O only."""
import contextlib,copy,threading,unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
from ipr.scan_timing import ScanTimings,PHASES
from ipr.api import Alpaca
from test_r71 import Sim
import test_r71 as prior
import test_r91 as watermark_tests

class R102Timings(unittest.TestCase):
    def fixture(self):
        t=Sim(0);d=ScanTimings(t.clock,t.wall);d.begin(t.clock(),t.wall());return t,d
    def test_exact_elapsed_phase_including_wait(self):
        t,d=self.fixture()
        with d.phase('volume_fetch'):t.sleep(7)
        with d.phase('ranking'):t.sleep(2)
        d.finish(True,9,None,t.wall());m=d.snapshot()
        self.assertEqual(m['last']['phases']['volume_fetch']['seconds'],7)
        self.assertEqual(m['phase_totals']['ranking']['total_seconds'],2)
        self.assertEqual((m['successes'],m['failures']),(1,0));self.assertIsNone(m['active'])
    def test_failure_phase_and_original_exception_preserved(self):
        t,d=self.fixture();exc=ValueError('fixture')
        with self.assertRaises(ValueError) as cm:
            with d.phase('snapshot_fetch'):t.sleep(3);raise exc
        self.assertIs(cm.exception,exc);d.finish(False,3,'ValueError',t.wall())
        m=d.snapshot();self.assertEqual(m['last']['failure_phase'],'snapshot_fetch')
        self.assertEqual(m['failure_reasons'],{'ValueError':1});self.assertEqual(m['failures'],1)
    def test_snapshots_detached_and_storage_bounded(self):
        t,d=self.fixture()
        for i in range(1000):
            d.begin(t.clock(),t.wall())
            with d.phase('base_evaluation'):t.sleep(.001)
            d.finish(False,.001,'R'+str(i),t.wall())
        m=d.snapshot();self.assertLessEqual(len(m['failure_reasons']),17)
        self.assertEqual(sum(m['failure_reasons'].values()),1000)
        self.assertEqual(len(m['phase_totals']),1);self.assertEqual(m['attempts_completed'],1000)
        m['last']['phases'].clear();self.assertTrue(d.snapshot()['last']['phases'])
    def test_no_api_observer_does_not_invent_lock_measurement(self):
        t,d=self.fixture()
        with d.requests(NS()):
            with d.phase('volume_fetch'):t.sleep(5)
        self.assertEqual(d.snapshot()['active']['rest']['lock_acquisitions'],0)
        self.assertFalse(d.snapshot()['active']['rest']['measurement_available'])
    def test_observer_storage_fault_does_not_replace_business_exception(self):
        t,d=self.fixture();d.current={'phases':None}
        with self.assertRaisesRegex(RuntimeError,'business'):
            with d.phase('ranking'):raise RuntimeError('business')
        self.assertGreater(d.observation_errors,0)

class R102REST(unittest.TestCase):
    def api(self,t):
        a=Alpaca({'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'})
        a.timing_clock=t.clock
        class Lock:
            def acquire(self):t.sleep(2)
            def release(self):pass
        a.lock=Lock();return a
    def test_scan_scope_wait_hold_and_counts_by_phase(self):
        t=Sim(0);a=self.api(t);d=ScanTimings(t.clock,t.wall);d.begin(0,t.wall())
        with d.requests(a),d.phase('volume_fetch'),a.operation('scan_rest'):
            with a.serialized_request():t.sleep(3)
        with a.operation('native'):
            with a.serialized_request():t.sleep(9)
        rest=d.snapshot()['active']['rest']
        self.assertEqual((rest['lock_acquisitions'],rest['wait_seconds'],rest['hold_seconds']),(1,2,3))
        self.assertEqual(rest['by_phase']['volume_fetch']['wait_seconds'],2)
        self.assertEqual(a.lock_metrics()['operations']['native']['count'],1)
        self.assertEqual(d.snapshot()['active']['phases']['volume_fetch']['seconds'],5)
    def test_thread_local_ignores_native_on_another_thread(self):
        t=Sim(0);a=self.api(t);rows=[]
        with a.observe_requests(lambda *v:rows.append(v)):
            worker=threading.Thread(target=lambda:self.request(a));worker.start();worker.join()
            self.request(a)
        self.assertEqual(len(rows),1)
    def request(self,a):
        with a.serialized_request():pass
    def test_observer_failure_keeps_original_exception_and_releases_lock(self):
        a=Alpaca({'APCA_API_KEY_ID':'fixture','APCA_API_SECRET_KEY':'fixture'})
        def fail(*a):raise OSError('diagnostic')
        with self.assertRaisesRegex(ValueError,'original'):
            with a.observe_requests(fail),a.serialized_request():raise ValueError('original')
        self.assertTrue(a.lock.acquire(False));a.lock.release()
        self.assertIsNone(getattr(a.local,'request_observer',None))
    def test_denied_recheck_is_measured_without_skipping_request(self):
        t=Sim(0);a=self.api(t);allowed=iter([False,True]);a.request_allowed=lambda:next(allowed);rows=[]
        with a.observe_requests(lambda *v:rows.append(v)),a.serialized_request():t.sleep(3)
        self.assertEqual(len(rows),2);self.assertEqual(sum(x[1] for x in rows),4)
        self.assertEqual(sum(x[2] for x in rows),3)

class R102ActualScan(unittest.TestCase):
    def capture(self,fn):
        observed=[]
        class Captured(ScanTimings):
            def __init__(self,*a,**kw):super().__init__(*a,**kw);observed.append(self)
        with patch('ipr.scan_timing.ScanTimings',Captured):result=fn()
        return result,observed[0].snapshot()
    def test_actual_success_exact_phases_same_plan_and_gate(self):
        r,m=self.capture(lambda:prior.R71Scan().scan_once())
        self.assertTrue(r[3]['scan_ok']);self.assertEqual(r[4].propose.call_count,1)
        self.assertEqual(r[6],[54.,55.,80.]);self.assertEqual(r[0].at,107.)
        phases=m['last']['phases'];self.assertEqual(phases['volume_fetch']['seconds'],1)
        self.assertEqual(phases['snapshot_fetch']['seconds'],52)
        self.assertEqual(m['last']['duration_seconds'],53)
        self.assertEqual(set(phases),set(PHASES));self.assertEqual(m['successes'],1)
    def test_actual_failure_duration_printed_not_inferred(self):
        r,m=self.capture(lambda:prior.R71Scan().scan_once(durations=(1.,60.,60.)))
        self.assertFalse(r[3]['scan_ok']);self.assertEqual(r[4].propose.call_count,0)
        line=r[5][-1];self.assertEqual(line['duration_seconds'],121)
        self.assertEqual(line['reason'],'RTH_VOLUME_STALE');self.assertIn('started_at',line)
        self.assertEqual(m['last']['failure_phase'],'freshness')
        self.assertEqual(m['failure_reasons'],{'RTH_VOLUME_STALE':1})
    def test_watermark_and_snapshot_boundaries_unchanged(self):
        for age,price_age,success,selected in [(100,0,True,['X']),(120,60,True,['X']),
                                               (120.000001,0,False,[]),(100,61,True,[])]:
            r,m=self.capture(lambda:watermark_tests.R91Watermark().actual_scan(age,price_age))
            self.assertEqual(r[0]['scan_ok'],success);self.assertEqual(r[2],selected)
            self.assertEqual(len(r[3]),int(success));self.assertEqual(m['successes'],int(success))
