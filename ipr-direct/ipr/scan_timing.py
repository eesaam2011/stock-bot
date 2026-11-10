"""Bounded scan observations only. No scheduling, remote I/O or decision inputs.

Phases use monotonic elapsed wall time, not CPU time. REST wait/hold are
subsets of phase time and must not be added again to the phase totals.
"""
import contextlib,copy,threading,time

PHASES = ('volume_fetch','snapshot_fetch','base_evaluation','ranking',
          'news_fetch','news_evaluation','freshness','needs','selection',
          'persistence','reporting')

class ScanTimings:
    def __init__(self,clock=time.monotonic,wall=lambda:None):
        self.clock=clock;self.wall=wall;self.lock=threading.Lock()
        self.started=0;self.completed=0;self.successes=0;self.failures=0
        self.current=None;self.last=None;self.reasons={};self.phase_stats={}
        self.observation_errors=0

    def _observe(self,fn):
        try:
            with self.lock:return fn()
        except Exception:
            # Diagnostics cannot replace a scan outcome or original exception.
            self.observation_errors+=1

    def begin(self,start,at):
        def record():
            self.started+=1
            self.current={'attempt':self.started,'started_at':at.isoformat(),
                          'started_monotonic':start,'phases':{},'active_phase':None,
                          'failure_phase':None,'rest':{'lock_acquisitions':0,
                          'wait_seconds':0.,'hold_seconds':0.,'by_phase':{},'measurement_available':None}}
        self._observe(record)

    @contextlib.contextmanager
    def phase(self,name):
        started=self.clock()
        def enter():
            if name not in PHASES:raise ValueError('UNKNOWN_SCAN_PHASE')
            if self.current:self.current['active_phase']=name
        self._observe(enter)
        try:yield
        except BaseException:
            self._observe(lambda:self.current.update(failure_phase=name) if self.current else None)
            raise
        finally:
            elapsed=max(0.,self.clock()-started)
            def finish():
                if self.current is None:return
                row=self.current['phases'].setdefault(name,{'count':0,'seconds':0.})
                row['count']+=1;row['seconds']+=elapsed;self.current['active_phase']=None
                row=self.phase_stats.setdefault(name,{'count':0,'total_seconds':0.,'max_seconds':0.})
                row['count']+=1;row['total_seconds']+=elapsed;row['max_seconds']=max(row['max_seconds'],elapsed)
            self._observe(finish)

    def rest(self,operation,wait,hold):
        # Called only in the scan worker's thread-local API observation scope.
        def record():
            if self.current is None:return
            rest=self.current['rest'];phase=self.current['active_phase'] or 'outside_phase'
            row=rest['by_phase'].setdefault(phase,{'lock_acquisitions':0,'wait_seconds':0.,'hold_seconds':0.})
            for target in (rest,row):
                target['lock_acquisitions']+=1;target['wait_seconds']+=wait;target['hold_seconds']+=hold
        self._observe(record)

    @contextlib.contextmanager
    def requests(self,api):
        # Fixtures without a real HTTP client have no REST timing; do not invent it.
        observe=getattr(api,'observe_requests',None)
        self._observe(lambda:self.current['rest'].update(measurement_available=observe is not None) if self.current else None)
        with observe(self.rest) if observe is not None else contextlib.nullcontext():yield

    def finish(self,success,seconds,reason,at):
        def record():
            if self.current is None:return
            self.completed+=1;self.successes+=int(success);self.failures+=int(not success)
            if not success:
                key=reason if reason in self.reasons or len(self.reasons)<16 else 'OTHER_FAILURE'
                self.reasons[key]=self.reasons.get(key,0)+1
            self.current.update(result='SUCCESS' if success else 'FAILED',reason=reason,
                                duration_seconds=seconds,finished_at=at.isoformat())
            self.last=self.current;self.current=None
        self._observe(record)

    def snapshot(self):
        def capture():
            return copy.deepcopy({'attempts_started':self.started,'attempts_completed':self.completed,
                'successes':self.successes,'failures':self.failures,'failure_reasons':self.reasons,
                'phase_totals':self.phase_stats,'active':self.current,'last':self.last,
                'observation_errors':self.observation_errors,
                'scope':'child-local bounded observations; elapsed includes waits, GIL and CPU throttling; REST wait/hold overlap phases; acquisitions include denied rechecks; news time includes cache/read latency; not a cause attribution'})
        return self._observe(capture)

    def attempt_snapshot(self):
        return self._observe(lambda:copy.deepcopy(self.current if self.current is not None else self.last))

def safe_timings(**kwargs):
    from ipr.telemetry_safe import SafeTimings
    try:inner=ScanTimings(**kwargs)
    except BaseException:inner=None
    return SafeTimings(inner)
