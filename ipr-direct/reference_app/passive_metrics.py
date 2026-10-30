"""Opt-in local observer. No Redis/socket/decision/trust writes; optional tiny local resource reads."""
import asyncio
import builtins
from collections import deque
from contextlib import contextmanager
from functools import wraps
import threading
import time

class PassiveMetrics:
    def __init__(self, clock=time.monotonic, wall=time.time, renew_capacity=256, resource_reader=None):
        self.clock=clock;self.wall=wall;self.lock=threading.Lock()
        self.resource_reader=resource_reader;self.resource_previous=None;self.resource_samples=0;self.resource_errors=0;self.rss_max=None;self.cpu_max=None;self.resource_gap_max=None;self.window_start_unix=wall()
        now=clock();self.start=now;self.window_start=now;self.next_tick=now+1.0
        self.report_due=now+60.0;self.lag_max=0.0;self.samples=0
        self.last_hb=None;self.hb_gap_max=None;self.hb_late_max=None;self.hb_count=0
        self.seq=0;self.renew=deque(maxlen=renew_capacity);self.dropped=0
        self.pending={};self.internal_errors=0;self.report_seq=0;self.reports_dropped_total=0

    def sample(self, now=None):
        now=self.clock() if now is None else now
        # Absolute one-second deadlines: measure wake-up lateness, not callback CPU.
        if now < self.next_tick:return
        self.lag_max=max(self.lag_max,max(0.0,now-self.next_tick));self.samples+=1
        missed=int(max(0.0,now-self.next_tick)//1.0)
        self.next_tick+=missed+1.0
        if self.resource_reader is not None:
            try:
                rss,cpu=self.resource_reader()
                self.resource_samples+=1
                if rss is not None:self.rss_max=max(self.rss_max or 0,rss)
                if self.resource_previous is not None:
                    prev_t,prev_cpu=self.resource_previous;gap=now-prev_t
                    if gap>0:
                        rate=max(0.0,(cpu-prev_cpu)/gap);self.cpu_max=max(self.cpu_max or 0.0,rate)
                        self.resource_gap_max=max(self.resource_gap_max or 0.0,gap)
                self.resource_previous=(now,cpu)
            except Exception:self.resource_errors+=1


    def heartbeat(self, now=None):
        now=self.clock() if now is None else now
        if self.last_hb is not None:
            gap=now-self.last_hb
            self.hb_gap_max=max(self.hb_gap_max or 0.0,gap)
            self.hb_late_max=max(self.hb_late_max or 0.0,max(0.0,gap-60.0))
        self.last_hb=now;self.hb_count+=1

    def begin_renew(self, worker, ttl):
        now=self.clock();utc=self.wall()
        with self.lock:
            self.seq+=1;seq=self.seq
            self.pending[seq]=(now,utc,str(worker),ttl)
        return seq

    def end_renew(self, seq, error_type=None):
        now=self.clock();utc=self.wall()
        with self.lock:
            start,start_utc,worker,ttl=self.pending.pop(seq)
            row={'renew_seq':seq,'start_monotonic':start,'end_monotonic':now,
                 'start_unix_utc':start_utc,'end_unix_utc':utc,'duration_sec':now-start,
                 'worker_id':worker,'requested_ttl_sec':ttl,
                 'result':'ERROR' if error_type else 'OK','error_type':error_type}
            if len(self.renew)==self.renew.maxlen:self.dropped+=1
            self.renew.append(row)

    def wrap_renew(self, fn):
        @wraps(fn)
        def observed(worker, ttl=30):
            # Failures in observer must not prevent the real request or replace its exception.
            seq=None
            try:seq=self.begin_renew(worker,ttl)
            except Exception:self.internal_errors+=1
            try:result=fn(worker,ttl)
            except BaseException as exc:
                if seq is not None:
                    try:self.end_renew(seq,type(exc).__name__)
                    except Exception:self.internal_errors+=1
                raise
            if seq is not None:
                try:self.end_renew(seq)
                except Exception:self.internal_errors+=1
            return result
        return observed

    def report(self, now=None):
        now=self.clock() if now is None else now
        if now<self.report_due:return None
        overdue=max(0.0,now-self.report_due)
        age=None if self.last_hb is None else now-self.last_hb
        # A stalled heartbeat must be visible even when no new heartbeat arrived.
        pending_late=None if age is None else max(0.0,age-60.0)
        with self.lock:
            renew=list(self.renew);self.renew.clear()
            inflight=[{'renew_seq':seq,'start_monotonic':x[0],'age_sec':now-x[0]} for seq,x in self.pending.items()]
            dropped=self.dropped;self.dropped=0
        self.report_seq+=1
        out={'stage':'PASSIVE_RUNTIME_WINDOW','report_seq':self.report_seq,
             'start_monotonic':self.window_start,'end_monotonic':now,
             'window_start_unix_utc':self.window_start_unix,'window_end_unix_utc':self.wall(),
             'worker_rss_sample_max_bytes':self.rss_max,'worker_cpu_interval_max_cpu':self.cpu_max,
             'resource_sample_count':self.resource_samples,'resource_sample_gap_max_sec':self.resource_gap_max,
             'resource_sampling_errors':self.resource_errors,
             'resource_scope':'worker RSS sampled ~1s and process CPU delta/monotonic interval, NOT Render or cgroup; sample max not continuous peak',
             'target_window_sec':60,'actual_window_sec':now-self.window_start,
             'report_lateness_sec':overdue,'crossed_unreported_windows':int(overdue//60),
             'event_loop_max_lag_sec':self.lag_max,'loop_samples':self.samples,
             'heartbeat_count':self.hb_count,'heartbeat_max_gap_sec':self.hb_gap_max,
             'heartbeat_max_lateness_sec':self.hb_late_max,'heartbeat_age_sec':age,
             'heartbeat_pending_lateness_sec':pending_late,'renewals':renew,
             'renewals_inflight':inflight,'renew_records_dropped':dropped,
             'observer_errors_total':self.internal_errors,'reports_dropped_total':self.reports_dropped_total}
        self.window_start_unix=self.wall();self.resource_samples=0;self.resource_errors=0;self.rss_max=None;self.cpu_max=None;self.resource_gap_max=None
        self.window_start=now;self.report_due+= (int(overdue//60)+1)*60
        self.lag_max=0.0;self.samples=0;self.hb_count=0;self.hb_gap_max=None;self.hb_late_max=None
        return out

    async def run(self, stop_event, queue):
        """Nonblocking delivery only; queue consumer/exporter is explicitly external."""
        dropped_reports=0
        while not stop_event.is_set():
            delay=max(0.0,self.next_tick-self.clock())
            try:await asyncio.wait_for(stop_event.wait(),timeout=delay)
            except asyncio.TimeoutError:pass
            if stop_event.is_set():break
            self.sample();row=self.report()
            if row is not None:
                row['reports_dropped_before_this']=dropped_reports
                try:queue.put_nowait(row);dropped_reports=0
                except asyncio.QueueFull:
                    dropped_reports+=1;self.reports_dropped_total+=1
        # A partial final window is intentionally not labelled a full 60-second window.

@contextmanager
def attach(supervisor, module, metrics):
    """Explicit opt-in seam AFTER composition, BEFORE supervisor.run; never called by app.

    The original app files remain byte-identical. Wrap this supervisor's renew only;
    observe heartbeat AFTER the existing module print succeeds. No new Redis commands.
    One attachment per module; this local adapter is not a production entrypoint.
    """
    if getattr(module,'_passive_attached',False):raise RuntimeError('ALREADY_ATTACHED')
    from bounded_telemetry import read_worker_resources
    metrics.resource_reader=read_worker_resources
    redis=supervisor.orchestrator.redis
    had_renew='renew' in vars(redis);original_renew=redis.renew
    had_print='print' in vars(module);original_print=vars(module).get('print',builtins.print)
    def observed_print(*args,**kwargs):
        result=original_print(*args,**kwargs)
        try:
            if len(args)==1 and isinstance(args[0],dict) and args[0].get('stage')=='LIVE_DATA_FLOW_HEARTBEAT':metrics.heartbeat()
        except Exception:metrics.internal_errors+=1
        return result
    redis.renew=metrics.wrap_renew(original_renew);module.print=observed_print;module._passive_attached=True
    try:yield metrics
    finally:
        if had_renew:redis.renew=original_renew
        else:delattr(redis,'renew')
        if had_print:module.print=original_print
        else:delattr(module,'print')
        delattr(module,'_passive_attached')
