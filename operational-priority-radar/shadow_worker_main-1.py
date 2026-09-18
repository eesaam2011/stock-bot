import asyncio, os, signal
from operational_priority_radar import WorkerConfig
from shutdown import GracefulDrain

REQUIRED=("REDIS_URL",)

class StartupBlocked(RuntimeError): pass

def startup_probe_env(env=os.environ):
    missing=[k for k in REQUIRED if not env.get(k)]
    key=env.get("APCA_API_KEY_ID") or env.get("ALPACA_API_KEY")
    secret=env.get("APCA_API_SECRET_KEY") or env.get("ALPACA_SECRET_KEY")
    if not key:missing.append("APCA_API_KEY_ID|ALPACA_API_KEY")
    if not secret:missing.append("APCA_API_SECRET_KEY|ALPACA_SECRET_KEY")
    if missing:return {"ok":False,"status":"CONFIG_MISSING","missing":missing}
    if env.get("OPR_SHADOW_MODE","true").lower()!="true":
        return {"ok":False,"status":"ACTIONABLE_MODE_BLOCKED"}
    return {"ok":True,"status":"SHADOW_CONFIG_READY"}

class ShadowRuntimeSupervisor:
    """Long-running supervisor. Production components are injected by build_runtime()."""
    def __init__(self, orchestrator, websocket_runtime, outbox_sender, outbox_store,
                 symbols, key, secret, stream_url, drain=None):
        self.orchestrator=orchestrator
        self.websocket_runtime=websocket_runtime
        self.outbox_sender=outbox_sender
        self.outbox_store=outbox_store
        self.symbols=symbols
        self.key=key; self.secret=secret; self.stream_url=stream_url
        self.drain=drain or GracefulDrain()
        self.stop_event=asyncio.Event()
        self.native5_cycles=0
        self.native5_last_stats={}

    def _leadership_snapshot(self):
        """Diagnostic only: expose lease identity/generation/TTL; no decision-policy changes."""
        lua=self.orchestrator.redis
        try:
            owner=lua.r.get(lua.leader_key)
            ttl=lua.r.ttl(lua.leader_key)
            generation=lua.r.get(lua.generation_key)
            return {
                "worker_id":self.orchestrator.worker_id,
                "owner":owner,
                "owner_matches_worker":owner==self.orchestrator.worker_id,
                "leader_generation":generation,
                "leader_ttl_sec":ttl,
            }
        except Exception as exc:
            return {"worker_id":self.orchestrator.worker_id,
                    "diagnostic_error_type":type(exc).__name__,
                    "diagnostic_error":str(exc)}

    async def outbox_loop(self):
        while not self.stop_event.is_set():
            for event in self.outbox_store.pending():
                self.outbox_sender.deliver_one(event)  # Shadow sender suppresses actionable delivery.
            try: await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError: pass

    async def native5_loop(self):
        while not self.stop_event.is_set():
            if hasattr(self,"decision_pipeline"):
                from datetime import datetime,timezone
                cycle_no=self.native5_cycles+1
                allow_decision=self.orchestrator.new_decisions_allowed()
                print({"stage":"NATIVE5_CYCLE_START","cycle":cycle_no,
                       "allow_decision":allow_decision,"batch_size":500,"max_workers":4},flush=True)
                try:
                    stats=await asyncio.to_thread(
                        self.decision_pipeline.poll_native5,
                        datetime.now(timezone.utc),allow_decision,500,4)
                except Exception as exc:
                    print({"stage":"NATIVE5_CYCLE_ERROR","cycle":cycle_no,
                           "error_type":type(exc).__name__,"error":str(exc)},flush=True)
                    try: await asyncio.wait_for(self.stop_event.wait(),timeout=5.0)
                    except asyncio.TimeoutError: pass
                    continue
                self.native5_last_stats=stats
                self.native5_cycles+=1
                print({"stage":"NATIVE5_CYCLE_OK","cycle":self.native5_cycles,
                       "symbols_with_rows":stats.get("symbols_with_rows") if isinstance(stats,dict) else None,
                       "evaluated_symbols":stats.get("evaluated_symbols") if isinstance(stats,dict) else None,
                       "scoreable_symbols":stats.get("scoreable_symbols") if isinstance(stats,dict) else None,
                       "crossings":stats.get("crossings") if isinstance(stats,dict) else None},flush=True)
            else:
                print({"stage":"NATIVE5_PIPELINE_MISSING"},flush=True)
            try: await asyncio.wait_for(self.stop_event.wait(),timeout=5.0)
            except asyncio.TimeoutError: pass


    def _redis_count(self, pattern):
        total=0; cursor=0
        while True:
            cursor,keys=self.orchestrator.redis.r.scan(cursor=cursor,match=pattern,count=200)
            total += len(keys)
            if int(cursor)==0:return total

    async def telemetry_loop(self):
        while not self.stop_event.is_set():
            p=getattr(self,"decision_pipeline",None)
            if p is not None:
                bars=sum(len(v) for v in p.bars.values())
                trades=sum(len(v) for v in p.trades.values())
                memory_stats=p.memory_stats() if hasattr(p,"memory_stats") else {}
                try:
                    session=str(p.session)
                    e_total=self._redis_count("operational_priority_radar:v1:early_core:*")
                    b_total=self._redis_count("operational_priority_radar:v1:base_ready:*")
                    opp_total=self._redis_count("operational_priority_radar:v1:opportunity:*")
                    e_session=self._redis_count(f"operational_priority_radar:v1:early_core:{session}:*")
                    b_session=self._redis_count(f"operational_priority_radar:v1:base_ready:{session}:*")
                    opp_session=self._redis_count(f"operational_priority_radar:v1:opportunity:{session}:*")
                except Exception as exc:
                    e_total=b_total=opp_total=None
                    e_session=b_session=opp_session=None
                    session=getattr(p,"session",None)
                    redis_error=type(exc).__name__
                else:
                    redis_error=None
                print({"stage":"LIVE_DATA_FLOW_HEARTBEAT",
                       "trust_state":getattr(self.orchestrator.trust.state,"value",str(self.orchestrator.trust.state)),
                       "buffered_1m_bars":bars,"buffered_trades":trades,
                       "memory_stats":memory_stats,
                       "native5_cycles":self.native5_cycles,
                       "native5_batch_size":500,
                       "native5_last":self.native5_last_stats,
                       "operational_session":session,
                       "early_core_events_session":e_session,
                       "base_ready_events_session":b_session,
                       "opportunities_session":opp_session,
                       "early_core_events_total":e_total,
                       "base_ready_events_total":b_total,
                       "opportunities_total":opp_total,
                       "redis_error":redis_error},flush=True)
            try: await asyncio.wait_for(self.stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError: pass

    async def leadership_loop(self):
        while not self.stop_event.is_set():
            before=self._leadership_snapshot()
            try:
                self.orchestrator.redis.renew(self.orchestrator.worker_id,30)
            except Exception as exc:
                print({"stage":"LEADERSHIP_RENEW_FAILED",
                       "error_type":type(exc).__name__,"error":str(exc),
                       "lease_before_renew":before,
                       "lease_after_failure":self._leadership_snapshot()},flush=True)
                self.stop_event.set()
                return
            print({"stage":"LEADERSHIP_RENEW_OK",
                   "lease_before_renew":before,
                   "lease_after_renew":self._leadership_snapshot()},flush=True)
            try: await asyncio.wait_for(self.stop_event.wait(), timeout=10.0)
            except asyncio.TimeoutError: pass

    async def run(self):
        print({"stage":"ACQUIRE_LEADERSHIP_START"}, flush=True)
        self.orchestrator.acquire_leadership()
        print({"stage":"ACQUIRE_LEADERSHIP_OK",
               "lease":self._leadership_snapshot()}, flush=True)

        if hasattr(self,"decision_pipeline"):
            print({"stage":"SYNC_LEADER_GENERATION_START"}, flush=True)
            self.decision_pipeline.leadership.sync_generation()
            print({"stage":"SYNC_LEADER_GENERATION_OK",
                   "lease":self._leadership_snapshot()}, flush=True)

        # Keep lease renewal alive throughout long startup recovery.
        leadership_task=asyncio.create_task(self.leadership_loop())
        print({"stage":"LEADERSHIP_RENEW_TASK_STARTED",
               "lease":self._leadership_snapshot()},flush=True)

        print({"stage":"STARTUP_RECOVERY_START"}, flush=True)
        recovery_result=await asyncio.to_thread(self.orchestrator.begin_recovery)
        print({"stage":"STARTUP_RECOVERY_RETURNED",
               "reconciled": recovery_result.get("reconciled",False)
                   if isinstance(recovery_result,dict) else None,
               "lease":self._leadership_snapshot()}, flush=True)
        # Fail closed before stream trust if the lease was lost during recovery.
        self.decision_pipeline.leadership.require_current() if hasattr(self,"decision_pipeline") else None

        print({"stage":"SIP_WEBSOCKET_TASK_START",
               "symbols_count":len(self.symbols)}, flush=True)
        ws_task=asyncio.create_task(self.websocket_runtime.reconnect_loop(
                self.stream_url,self.key,self.secret,self.symbols))

        # CONNECTED is not trusted. Never wait silently forever during startup.
        print({"stage":"WAITING_FOR_SIP_CONNECTED","timeout_sec":45}, flush=True)
        try:
            await asyncio.wait_for(
                self.websocket_runtime.connected_event.wait(),
                timeout=45.0
            )
        except asyncio.TimeoutError:
            print({"stage":"SIP_CONNECTED_TIMEOUT",
                   "timeout_sec":45,
                   "new_entries_allowed":False}, flush=True)
            self.websocket_runtime.stop()
            ws_task.cancel()
            await asyncio.gather(ws_task, return_exceptions=True)
            raise RuntimeError("SIP_WEBSOCKET_CONNECT_TIMEOUT")

        print({"stage":"SIP_CONNECTED_NOT_YET_TRUSTED"}, flush=True)
        self.orchestrator.mark_stream_connected()
        recovery=self.orchestrator.c.recovery
        while hasattr(recovery,"ready_after_stream") and not recovery.ready_after_stream():
            if self.stop_event.is_set():break
            await asyncio.sleep(0.05)
        if not self.stop_event.is_set():
            reconciled = recovery.ready_after_stream() if hasattr(recovery,"ready_after_stream") else recovery_result.get("reconciled",False)
            if not reconciled:
                raise RuntimeError("POST_STREAM_RECONCILIATION_UNPROVEN")
            trust_state = self.orchestrator.finish_reconciliation(continuity_ok=True)
            print({"stage":"SIP_LIVE_TRUSTED",
                   "trust_state": getattr(trust_state, "value", str(trust_state)),
                   "lease":self._leadership_snapshot()}, flush=True)
        tasks=[
            ws_task,
            asyncio.create_task(self.outbox_loop()),
            asyncio.create_task(self.native5_loop()),
            leadership_task,
            asyncio.create_task(self.telemetry_loop()),
        ]
        await self.stop_event.wait()
        self.drain.begin()
        self.websocket_runtime.stop()
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.drain.complete()

    def request_stop(self):
        self.stop_event.set()

def install_signal_handlers(loop, supervisor):
    for sig in (signal.SIGTERM,signal.SIGINT):
        try:loop.add_signal_handler(sig,supervisor.request_stop)
        except (NotImplementedError,RuntimeError):pass

def build_runtime(config):
    # Explicit deployment boundary: imports/constructors for real Redis, SIP, engines,
    # recovery and durable outbox must be supplied by the composition module.
    from production_composition import compose_shadow_runtime
    return compose_shadow_runtime(config)

async def async_main():
    p=startup_probe_env()
    if not p["ok"]:raise StartupBlocked(str(p))
    cfg=WorkerConfig.from_env();cfg.validate()
    supervisor=build_runtime(cfg)
    install_signal_handlers(asyncio.get_running_loop(),supervisor)
    print({"status":"OPERATIONAL_SHADOW_MODE_STARTING","actionable_alerts":False},flush=True)
    await supervisor.run()

def main():
    asyncio.run(async_main())

if __name__=="__main__":
    main()
