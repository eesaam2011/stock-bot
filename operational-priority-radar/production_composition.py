import os,uuid
from alpaca_production_market import AlpacaCredentials,AlpacaREST,AlpacaSIPProtocol,SIP_STREAM_URL
from runtime_wiring import RuntimeComponents,RuntimeOrchestrator
from websocket_runtime import WebSocketRuntime
from sip_epoch_capture import BoundedEpochCapture
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
            trade_reconciler=ActiveTradeChronologicalReconciler(rest,r,worker_id)
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
    pipeline=ProductionDecisionPipeline(r,orch.redis,leadership,session,syms,ec,br,rest,worker_id,shadow=True)
    pipeline.status_tracker=getattr(rec,"status_tracker",None)
    pipeline.decision_gate=orch.new_decisions_allowed
    async def on_message(msg):
      kind=AlpacaSIPProtocol.classify(msg)
      received_at=datetime.now(timezone.utc);symbol=msg.get("S")
      allow=orch.new_decisions_allowed()
      # Until chronological gap reconciliation is proven, neither new entry
      # decisions nor active-trade transitions may consume post-gap market
      # messages out of context. Status messages remain quarantined for
      # authoritative halt reconciliation by the recovery coordinator.
      if kind=="BAR" and symbol and allow:pipeline.on_bar(symbol,msg,received_at,True)
      elif kind=="TRADE" and symbol and allow:pipeline.on_trade(symbol,msg,received_at,True)
      elif kind=="STATUS":
        rec.on_status(msg)
        pipeline.on_status(msg)
      rec.on_stream_message(kind,msg)
    async def on_disconnect():
      # Synchronize with the threaded native5 E persistence critical section.
      # The websocket has already cleared its ACK before this callback.
      with pipeline.decision_lock:
        orch.on_disconnect();rec.on_disconnect()
        pipeline.halted.clear()
        pipeline._entry_opportunities.clear()
        pipeline.trades.clear()
    # Bounded capture is intentionally not sufficient to establish trust.
    # Overflow invalidates the epoch and forces fail-closed reconnect.
    capture=BoundedEpochCapture(max_messages=4096,max_bytes=8*1024*1024)
    ws=WebSocketRuntime(connector,AlpacaSIPProtocol,on_message,on_disconnect,
                        epoch_capture=capture)
    supervisor=ShadowRuntimeSupervisor(orch,ws,sender,outbox_store,syms,
        config.alpaca_key,config.alpaca_secret,SIP_STREAM_URL)
    supervisor.decision_pipeline=pipeline
    return supervisor
