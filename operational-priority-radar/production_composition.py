import os,uuid,asyncio
from alpaca_production_market import AlpacaCredentials,AlpacaREST,AlpacaSIPProtocol,SIP_STREAM_URL
from runtime_wiring import RuntimeComponents,RuntimeOrchestrator
from websocket_runtime import WebSocketRuntime
from sip_epoch_capture import BoundedEpochCapture
from sip_drain_coordinator import BoundedSIPDrainCoordinator
from production_sip_semantic_journal import ProductionSIPSemanticJournal
from production_sip_transport_journal import ProductionSIPTransportJournal
from durable_outbox_sender import DurableOutboxSender
from redis_outbox_store import RedisOutboxStore
from resource_guard import ResourceGuard
from shadow_worker_main import ShadowRuntimeSupervisor
from production_adapters import OperationalEarlyCore,OperationalBaseReady
from production_recovery import RedisCanonicalReader,ProductionStartupRecovery
from active_trade_recovery import ActiveTradeChronologicalReconciler
from production_halt_status import ProductionStatusTracker
from production_runtime_state import RedisLeadershipFacade
from production_pipeline import ProductionDecisionPipeline
from datetime import datetime,timezone
from production_universe import build_operational_universe

class CompositionUnavailable(RuntimeError):pass

class ProductionRecoveryBridge:
 def __init__(self):self._disconnect=False
 def run(self):
  # Startup cannot claim reconciliation without canonical recovery implementation/context.
  # Fail closed until deployment supplies session/universe recovery coordinator.
  return {"gap_recovered":False,"reconciled":False}
 def on_disconnect(self):self._disconnect=True
 def on_status(self,msg):pass
 def on_stream_message(self,kind,msg):pass

def compose_shadow_runtime(config, *, redis_client=None, websocket_connector=None,
                           recovery=None, early_core=None, base_ready=None, symbols=None):
    production_redis=redis_client is None
    if redis_client is None:
        try:
            import redis
        except ImportError as e:
            raise RuntimeError("PRODUCTION_DEPENDENCY_MISSING:redis") from e
        r=redis.Redis.from_url(config.redis_url,decode_responses=True)
    else:
        r=redis_client
    if websocket_connector is None:
        try:
            import websockets
        except ImportError as e:
            raise RuntimeError("PRODUCTION_DEPENDENCY_MISSING:websockets") from e
        connector=websockets.connect
    else:
        connector=websocket_connector
    rest=AlpacaREST(AlpacaCredentials(config.alpaca_key,config.alpaca_secret))
    ec=early_core or OperationalEarlyCore(rest)
    br=base_ready or OperationalBaseReady()
    worker_id=os.getenv("WORKER_INSTANCE_ID") or uuid.uuid4().hex
    session=os.getenv("OPR_SESSION") or datetime.now(timezone.utc).date().isoformat()
    # Production Shadow uses Alpaca active/tradable US-equity universe automatically.
    # `symbols` remains an explicit injection seam for deterministic tests only.
    if symbols is not None:
        syms=list(symbols)
    elif redis_client is not None or websocket_connector is not None or recovery is not None or early_core is not None or base_ready is not None:
        syms=[]  # deterministic injected/test seam; production path below never uses this branch
    else:
        syms=build_operational_universe(rest)
    if recovery is not None:
        rec=recovery
    else:
        if not syms:
            rec=ProductionRecoveryBridge()  # explicit fail-closed until deployment context supplied
        else:
            trade_reconciler=ActiveTradeChronologicalReconciler(
                rest,r,worker_id,enable_trade_chunk_fallback=True)
            status_tracker=ProductionStatusTracker()
            rec=ProductionStartupRecovery(RedisCanonicalReader(r),rest,trade_reconciler,session,syms,status_tracker=status_tracker)
    guard=ResourceGuard()
    outbox_store=RedisOutboxStore(r)
    class ForbiddenTelegram:
      def send(self,payload):raise AssertionError("ACTIONABLE_TELEGRAM_FORBIDDEN_IN_SHADOW")
    sender=DurableOutboxSender(outbox_store,ForbiddenTelegram(),shadow_mode=True)
    comps=RuntimeComponents(r,None,rest,ec,br,sender,guard,rec)
    orch=RuntimeOrchestrator(comps,worker_id)
    leadership=RedisLeadershipFacade(orch.redis,worker_id)
    if isinstance(rec,ProductionStartupRecovery) and rec.trade_reconciler is not None:
        rec.trade_reconciler.leadership=leadership
    if production_redis and isinstance(rec,ProductionStartupRecovery):
        from durable_revision_journal import DurableRevisionJournal
        rec.durable_revision_journal=DurableRevisionJournal(orch.redis,leadership)
    pipeline=ProductionDecisionPipeline(r,orch.redis,leadership,session,syms,ec,br,rest,worker_id,shadow=True)
    pipeline.status_tracker=getattr(rec,"status_tracker",None)
    capture=BoundedEpochCapture(max_messages=4096,max_bytes=8*1024*1024)
    # Empty symbols exist only in the deterministic injection seam above.
    # Real production always has a non-empty universe and therefore uses the
    # semantic journal before capture ACK.
    semantic_journal=(ProductionSIPSemanticJournal(
                          orch.redis,session,syms,
                          bar_window_start=os.getenv("OPR_SESSION_START_UTC"),
                          bar_window_end=os.getenv("OPR_SESSION_END_UTC"))
                      if syms else ProductionSIPTransportJournal(
                          orch.redis,session))
    sip_drain=BoundedSIPDrainCoordinator(
        capture,leadership,semantic_journal.reconcile_batch,batch_size=128)
    # Capture/ACK are transport prerequisites, not continuity evidence.
    # Closing the ACK instantly closes the gate, even before disconnect
    # callback acquires the decision lock.
    pipeline.decision_gate=lambda: (
        orch.new_decisions_allowed()
        and ws.connected_event.is_set()
        and capture.phase==capture.DIRECT
        and capture.epoch==ws.connection_epoch)
    def process_message(msg):
      kind=AlpacaSIPProtocol.classify(msg)
      received_at=datetime.now(timezone.utc);symbol=msg.get("S")
      with pipeline.decision_lock:
        tagged_epoch=msg.get("_sip_epoch")
        if tagged_epoch is not None and (
            not ws.connected_event.is_set()
            or tagged_epoch!=ws.connection_epoch):
          return  # late to_thread work from a disconnected/old SIP epoch
        allow=pipeline.decision_gate()
        if kind=="BAR" and symbol and allow:pipeline.on_bar(symbol,msg,received_at,True)
        elif kind=="TRADE" and symbol and allow:pipeline.on_trade(symbol,msg,received_at,True)
        elif kind=="STATUS":
          # Pre-trust statuses belong to the captured SIP epoch, not the
          # authoritative halt state. In particular, a premature TRADING
          # status must never resume a HALTED_ACTIVE trade or authorize entry.
          # The chronological recovery coordinator must replay these statuses
          # before continuity can be proven and DIRECT processing enabled.
          if allow:
            rec.on_status(msg)
            pipeline.on_status(msg)
        rec.on_stream_message(kind,msg)
        # During startup recovery this is transport reconciliation only.  A
        # durable exact receipt permits bounded capture ACK, but never DIRECT.
        if capture.phase==capture.DRAINING and capture.snapshot()["buffered"]>=sip_drain.batch_size:
          sip_drain.drain_available(ws.connection_epoch,max_batches=4)
    async def on_message(msg):
      # Redis and active-trade REST work must not block the SIP socket reader.
      # A single bounded queue consumer preserves message arrival order.
      await asyncio.to_thread(process_message,msg)
    async def on_disconnect():
      # Synchronize with the threaded native5 E persistence critical section.
      # The websocket has already cleared its ACK before this callback.
      with pipeline.decision_lock:
        orch.on_disconnect();rec.on_disconnect()
        # Bounded memory-only handoff. Never perform REST under this lock or
        # allow a later non-revision disconnect to erase unresolved evidence.
        retain=getattr(rec,"retain_revision_terminal",None)
        if callable(retain):retain(ws.last_epoch_diagnostic)
        pipeline.halted.clear()
        pipeline._entry_opportunities.clear()
        pipeline.trades.clear()
      persist=getattr(rec,"persist_revision_terminal",None)
      if callable(persist):
        # Offload Redis outside the decision lock and socket event loop.
        # Failure cancels recovery; reconnect alone cannot clear that failure.
        await asyncio.to_thread(persist,ws.last_epoch_diagnostic)
    # Capture overflow and dispatch backlog overflow are fatal disconnects.
    # No drop-oldest behavior is permitted for trades, bars or halt statuses.
    ws=WebSocketRuntime(connector,AlpacaSIPProtocol,on_message,on_disconnect,
                        epoch_capture=capture,dispatch_queue_max=1024)
    supervisor=ShadowRuntimeSupervisor(orch,ws,sender,outbox_store,syms,
        config.alpaca_key,config.alpaca_secret,SIP_STREAM_URL)
    supervisor.decision_pipeline=pipeline
    supervisor.sip_drain_coordinator=sip_drain
    # The combined journal atomically advances the original transport receipt
    # and validated semantic summaries.  It still cannot claim reconciliation.
    supervisor.sip_transport_journal=semantic_journal
    supervisor.sip_semantic_journal=(semantic_journal if syms else None)
    return supervisor
