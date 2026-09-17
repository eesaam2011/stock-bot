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
                # REST is synchronous: keep it off the asyncio event loop so SIP/leadership/telemetry remain responsive.
                try:
                    self.native5_last_stats = await asyncio.to_thread(
                        self.decision_pipeline.poll_native5,
                        datetime.now(timezone.utc),
                        self.orchestrator.new_decisions_allowed(),
                        500, 4
                    )
                except Exception as exc:
                    self.native5_last_stats={"error":f"{type(exc).__name__}:{exc}"}
                    print({"stage":"NATIVE5_CYCLE_ERROR","error":self.native5_last_stats["error"]},flush=True)
                else:
                    self.native5_cycles += 1
            try: await asyncio.wait_for(self.stop_event.wait(), timeout=5.0)
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
                try:
                    e,b,opp=await asyncio.gather(
                        asyncio.to_thread(self._redis_count,"operational_priority_radar:v1:early_core:*"),
                        asyncio.to_thread(self._redis_count,"operational_priority_radar:v1:base_ready:*"),
                        asyncio.to_thread(self._redis_count,"operational_priority_radar:v1:opportunity:*")
                    )
                except Exception as exc:
                    e=b=opp=None
                    redis_error=type(exc).__name__
                else:
                    redis_error=None
                print({"stage":"LIVE_DATA_FLOW_HEARTBEAT",
                       "trust_state":getattr(self.orchestrator.trust.state,"value",str(self.orchestrator.trust.state)),
                       "buffered_1m_bars":bars,"buffered_trades":trades,
                       "native5_cycles":self.native5_cycles,
                       "native5_batch_size":500,
                       "native5_last":self.native5_last_stats,
                       "sip_subscription":getattr(self.websocket_runtime,"subscription_stats",{}),
                       "sip_last_error":getattr(self.websocket_runtime,"last_error",None),
                       "early_core_events":e,"base_ready_events":b,
                       "opportunities":opp,"redis_error":redis_error},flush=True)
            try: await asyncio.wait_for(self.stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError: pass

    async def leadership_loop(self):
        while not self.stop_event.is_set():
            await asyncio.to_thread(self.orchestrator.redis.renew,self.orchestrator.worker_id,30)
            try: await asyncio.wait_for(self.stop_event.wait(), timeout=10.0)
            except asyncio.TimeoutError: pass

    async def run(self):
        print({"stage":"ACQUIRE_LEADERSHIP_START"}, flush=True)
        self.orchestrator.acquire_leadership()
        print({"stage":"ACQUIRE_LEADERSHIP_OK"}, flush=True)

        if hasattr(self,"decision_pipeline"):
            print({"stage":"SYNC_LEADER_GENERATION_START"}, flush=True)
            self.decision_pipeline.leadership.sync_generation()
            print({"stage":"SYNC_LEADER_GENERATION_OK"}, flush=True)

        print({"stage":"STARTUP_RECOVERY_START"}, flush=True)
        recovery_result=self.orchestrator.begin_recovery()
        print({"stage":"STARTUP_RECOVERY_RETURNED",
               "reconciled": recovery_result.get("reconciled",False)
                   if isinstance(recovery_result,dict) else None}, flush=True)

        if hasattr(self,"decision_pipeline") and hasattr(self.decision_pipeline,"refresh_runtime_caches"):
            cache_stats=await asyncio.to_thread(self.decision_pipeline.refresh_runtime_caches)
            print({"stage":"RUNTIME_HOT_CACHE_READY",**cache_stats},flush=True)

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

        print({"stage":"SIP_SUBSCRIPTION_VERIFIED",
               "subscription":getattr(self.websocket_runtime,"subscription_stats",{})}, flush=True)
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
                   "trust_state": getattr(trust_state, "value", str(trust_state))}, flush=True)
        task_map={
            ws_task:"websocket",
            asyncio.create_task(self.outbox_loop()):"outbox",
            asyncio.create_task(self.native5_loop()):"native5",
            asyncio.create_task(self.leadership_loop()):"leadership",
            asyncio.create_task(self.telemetry_loop()):"telemetry",
        }
        stop_wait=asyncio.create_task(self.stop_event.wait())
        tasks=list(task_map)
        try:
            done,_=await asyncio.wait([stop_wait,*tasks],return_when=asyncio.FIRST_COMPLETED)
            unexpected=[t for t in done if t is not stop_wait]
            if unexpected and not self.stop_event.is_set():
                t=unexpected[0];name=task_map[t]
                try:exc=t.exception()
                except asyncio.CancelledError:exc=None
                print({"stage":"RUNTIME_TASK_TERMINATED","task":name,
                       "error":None if exc is None else f"{type(exc).__name__}:{exc}",
                       "new_entries_allowed":False},flush=True)
                self.orchestrator.on_disconnect()
                raise RuntimeError(f"CRITICAL_RUNTIME_TASK_TERMINATED:{name}") from exc
        finally:
            self.drain.begin();self.websocket_runtime.stop();stop_wait.cancel()
            for t in tasks:t.cancel()
            await asyncio.gather(stop_wait,*tasks,return_exceptions=True)
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
