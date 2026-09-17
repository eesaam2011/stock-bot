import asyncio, os, signal
from operational_priority_radar import WorkerConfig
from shutdown import GracefulDrain

REQUIRED=("REDIS_URL","APCA_API_KEY_ID","APCA_API_SECRET_KEY")

class StartupBlocked(RuntimeError): pass

def startup_probe_env(env=os.environ):
    missing=[k for k in REQUIRED if not env.get(k)]
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
                self.decision_pipeline.poll_native5(datetime.now(timezone.utc),self.orchestrator.new_decisions_allowed())
            try: await asyncio.wait_for(self.stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError: pass

    async def leadership_loop(self):
        while not self.stop_event.is_set():
            self.orchestrator.redis.renew(self.orchestrator.worker_id,30)
            try: await asyncio.wait_for(self.stop_event.wait(), timeout=10.0)
            except asyncio.TimeoutError: pass

    async def run(self):
        self.orchestrator.acquire_leadership()
        if hasattr(self,"decision_pipeline"):self.decision_pipeline.leadership.sync_generation()
        recovery_result=self.orchestrator.begin_recovery()
        ws_task=asyncio.create_task(self.websocket_runtime.reconnect_loop(
                self.stream_url,self.key,self.secret,self.symbols))
        # CONNECTED is not trusted. Subscription must exist before status reconciliation.
        await self.websocket_runtime.connected_event.wait()
        self.orchestrator.mark_stream_connected()
        recovery=self.orchestrator.c.recovery
        while hasattr(recovery,"ready_after_stream") and not recovery.ready_after_stream():
            if self.stop_event.is_set():break
            await asyncio.sleep(0.05)
        if not self.stop_event.is_set():
            reconciled = recovery.ready_after_stream() if hasattr(recovery,"ready_after_stream") else recovery_result.get("reconciled",False)
            if not reconciled:
                raise RuntimeError("POST_STREAM_RECONCILIATION_UNPROVEN")
            self.orchestrator.finish_reconciliation(continuity_ok=True)
        tasks=[
            ws_task,
            asyncio.create_task(self.outbox_loop()),
            asyncio.create_task(self.native5_loop()),
            asyncio.create_task(self.leadership_loop()),
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
