"""Fail-closed orchestration; delegates successful startup to the unchanged supervisor."""
import asyncio,time
from dataclasses import dataclass
from contextlib import contextmanager
from market_trust import new_entries_allowed
from passive_metrics import PassiveMetrics,attach
from metrics_stdout import OfferQueue
from restart_budget import UNTRUSTED_AFTER_ACK_SEC,MAX_RESTARTS

class StartupConnectionChanged(RuntimeError):pass

def trusted(orch):
    return getattr(orch.trust.state,'value',orch.trust.state)=='LIVE_TRUSTED'

@dataclass(frozen=True)
class Outcome:
    action:str # RESTART_REQUESTED, PARK, EXTERNAL_STOP
    reason:str

class AckEvent(asyncio.Event):
    def __init__(self,bridge):super().__init__();self.bridge=bridge
    def set(self):
        super().set();self.bridge.ack()
    def clear(self):
        was=self.is_set();super().clear()
        if was:self.bridge.disconnected()

class ConnectionBridge:
    def __init__(self,s,emit,clock=time.monotonic,wall=time.time):
        self.s=s;self.emit=emit;self.clock=clock;self.wall=wall
        self.attempt=0;self.ack_seq=0;self.ack_attempt=None;self.armed_at=None
        self.startup_mark=None;self.stopping=False;self.last_gap_wall=None
    def ack(self):
        self.ack_seq+=1;self.ack_attempt=self.attempt
        if not trusted(self.s.orchestrator) and self.armed_at is None:self.armed_at=self.clock()
        self.emit({'stage':'WS_ACK_OBSERVED','connection_attempt':self.attempt,'ack_seq':self.ack_seq,
                   'trust_state':str(self.s.orchestrator.trust.state),'watchdog_armed_at':self.armed_at})
    def disconnected(self):
        if trusted(self.s.orchestrator):self.armed_at=None
        self.s.orchestrator.on_disconnect() # Only closes trust; no positive transition.
        if self.last_gap_wall is None:self.last_gap_wall=self.wall()
        self.emit({'stage':'DATA_GAP_OPEN','connection_attempt':self.attempt,'gap_start_unix_utc':self.last_gap_wall,
                   'coverage':'UNPROVEN','events_lost_count':None,'new_entries_allowed':False})
    def timeout_reason(self):
        if trusted(self.s.orchestrator):self.armed_at=None;return None
        if self.armed_at is not None and self.clock()-self.armed_at>=UNTRUSTED_AFTER_ACK_SEC:
            return 'WS_ACK_UNTRUSTED_TIMEOUT'
        return None
    @contextmanager
    def install(self):
        ws=self.s.websocket_runtime;orch=self.s.orchestrator
        event=ws.connected_event;run_once=ws.run_once
        mark=orch.mark_stream_connected;finish=orch.finish_reconciliation
        ws.connected_event=AckEvent(self)
        async def observed_once(*a,**kw):
            self.attempt+=1
            try:return await run_once(*a,**kw)
            finally:
                # Clean iterator termination is also a disconnect unless shutting down.
                if not self.stopping and ws.connected_event.is_set():ws.connected_event.clear()
        def mark_connected():
            if not ws.connected_event.is_set():raise StartupConnectionChanged('ACK_NOT_CURRENT')
            self.startup_mark=(self.attempt,self.ack_seq)
            return mark()
        def finish_startup(*a,**kw):
            if (self.stopping or not ws.connected_event.is_set() or
                self.startup_mark!=(self.attempt,self.ack_seq)):
                raise StartupConnectionChanged('STARTUP_CONNECTION_EPOCH_CHANGED')
            result=finish(*a,**kw) # Existing gap/reconciliation/continuity behavior, unchanged.
            if not trusted(orch):raise StartupConnectionChanged('INHERITED_STARTUP_DID_NOT_REACH_LIVE_TRUSTED')
            self.armed_at=None
            self.emit({'stage':'STARTUP_PATH_RETURNED_TRUSTED','connection_attempt':self.attempt,
                       'gap_event_replay_proven':False,'continuity_proven_by_this_guard':False})
            return result
        ws.run_once=observed_once;orch.mark_stream_connected=mark_connected;orch.finish_reconciliation=finish_startup
        try:yield self
        finally:
            ws.connected_event=event;ws.run_once=run_once
            orch.mark_stream_connected=mark;orch.finish_reconciliation=finish

class GuardedRuntime:
    def __init__(self,s,module,policy,boot_id,boot_number,sink,clock=time.monotonic,wall=time.time):
        self.s=s;self.module=module;self.policy=policy;self.boot_id=boot_id
        self.boot_number=boot_number;self.sink=sink;self.clock=clock;self.wall=wall
        self.metrics=PassiveMetrics(clock,wall);self.bridge=ConnectionBridge(s,sink.offer,clock,wall)
        self.external_stop=False;self.metrics_stop=asyncio.Event()
    def signal_stop(self):self.external_stop=True;self.s.request_stop()
    async def watchdog(self):
        while not self.s.stop_event.is_set():
            if getattr(getattr(self.s,"decision_pipeline",None),"_confluence_lease_lost",False):
                return "CONFLUENCE_LEASE_LOST"
            reason=self.bridge.timeout_reason()
            if reason:return reason
            try:await asyncio.wait_for(self.s.stop_event.wait(),timeout=1.0)
            except asyncio.TimeoutError:pass
        return 'EXTERNAL_TERMINATION' if self.external_stop else 'RUNTIME_STOPPED'
    async def _quiesce(self,baseline):
        self.bridge.stopping=True;self.s.orchestrator.on_disconnect();self.s.request_stop();self.s.websocket_runtime.stop()
        # This entrypoint owns a private asyncio loop. Cancel its application tasks,
        # including tasks created during startup before the original supervisor's list.
        current=asyncio.current_task()
        for task in asyncio.all_tasks():
            if task not in baseline and task is not current and task is not self.consumer:task.cancel()
        # No new Redis mutation here. The worker process exits after this bounded
        # coroutine cleanup; it cannot park with an in-flight native5 thread.
        # Its existing leadership lease expires naturally, just as before.
        await asyncio.sleep(0)
    async def run(self):
        baseline=set(asyncio.all_tasks());self.consumer=asyncio.create_task(self.sink.consume())
        with attach(self.s,self.module,self.metrics),self.bridge.install():
            meter=asyncio.create_task(self.metrics.run(self.metrics_stop,OfferQueue(self.sink)))
            run=asyncio.create_task(self.s.run());watch=asyncio.create_task(self.watchdog())
            self.sink.offer({'stage':'WIRED_RUNTIME_STARTED','T_sec':120,'max_self_restarts':MAX_RESTARTS,'boot_number':self.boot_number,
                             'session_end_utc':self.policy.end_utc,'actionable_alerts':False})
            if self.boot_number>1:
                self.sink.offer({'stage':'GAP_COVERAGE_UNPROVEN_AFTER_REBOOT','events_lost_count':None,
                                 'eb_gap_replayed':False,'new_trust_path_added':False})
            done,_=await asyncio.wait([run,watch],return_when=asyncio.FIRST_COMPLETED)
            if watch in done:reason=watch.result()
            else:
                try:run.result();reason='SUPERVISOR_RETURNED'
                except BaseException as exc:reason='STARTUP_OR_RUNTIME_ERROR:'+type(exc).__name__+':'+str(exc)[:160]
            # Close eligibility immediately, before any ledger I/O or cleanup wait.
            deadline=self.clock()+60.0
            self.bridge.stopping=True;self.s.orchestrator.on_disconnect();self.s.request_stop();self.s.websocket_runtime.stop()
            self.sink.offer({'stage':'FAIL_CLOSED_SELF_STOP','reason':reason,'T_sec':120,
                             'connection_attempt':self.bridge.attempt,'ack_seq':self.bridge.ack_seq,
                             'new_entries_allowed':False,'gap_event_count_lost':None})
            # Same 60-second tail ceiling; never increase it. The caller handles the
            # exit/parking outcome without asyncio.run's unbounded executor join.
            try:
                await asyncio.wait_for(self._quiesce(baseline),timeout=max(0.0,deadline-self.clock()))
            except asyncio.TimeoutError:self.sink.offer({'stage':'SELF_STOP_TAIL_TIMEOUT','timeout_sec':60})
            # Export the actual final partial interval; never label it a full minute.
            now=self.clock();self.metrics.report_due=now
            partial=self.metrics.report(now)
            if partial is not None:
                partial.update(stage='PASSIVE_RUNTIME_FINAL_PARTIAL',partial=True,target_window_sec=None)
                self.sink.offer(partial)
            outcome=Outcome('EXTERNAL_STOP' if self.external_stop else 'WORKER_EXIT',reason)
            self.sink.offer({'stage':'SELF_STOP_OUTCOME','action':outcome.action,'reason':outcome.reason,
                             'boot_number':self.boot_number,'session_end_utc':self.policy.end_utc})
            # Bounded JSON drain; no claim that this drains native5 thread work.
            try:await asyncio.wait_for(self.sink.flush(),timeout=min(1.0,max(0.0,deadline-self.clock())))
            except asyncio.TimeoutError:pass
            self.consumer.cancel();await asyncio.gather(self.consumer,return_exceptions=True)
            return outcome
