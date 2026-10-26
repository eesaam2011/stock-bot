import os,uuid,asyncio
from alpaca_production_market import AlpacaCredentials,AlpacaREST,AlpacaSIPProtocol,SIP_STREAM_URL
from runtime_wiring import RuntimeComponents,RuntimeOrchestrator
from websocket_runtime import WebSocketRuntime
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
from epoch_subscription_policy import EpochSubscriptionPolicy
from pathlib import Path

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
    # D uses the pinned static scope plus canonical monitoring/recovery needs.
    # `symbols` remains an explicit injection seam for deterministic tests only.
    policy=None
    if symbols is not None:
        syms=list(symbols)
    elif redis_client is not None or websocket_connector is not None or recovery is not None or early_core is not None or base_ready is not None:
        syms=[]  # deterministic injected/test seam; production path below never uses this branch
    else:
        policy=EpochSubscriptionPolicy(Path(__file__).resolve().parent/"frozen",session)
        # Resolve again under leadership immediately before startup recovery.
        syms=list(policy.frozen.static)
    if recovery is not None:
        rec=recovery
    else:
        if not syms and policy is None:
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
    if policy is not None:
        original_allowed=orch.new_decisions_allowed
        orch.new_decisions_allowed=lambda: original_allowed() and policy.new_nominations_allowed()
    leadership=RedisLeadershipFacade(orch.redis,worker_id)
    pipeline=ProductionDecisionPipeline(r,orch.redis,leadership,session,syms,ec,br,rest,worker_id,shadow=True)
    async def on_message(msg):
      kind=AlpacaSIPProtocol.classify(msg)
      received_at=datetime.now(timezone.utc);symbol=msg.get("S")
      allow=orch.new_decisions_allowed()
      if kind=="BAR" and symbol:await asyncio.to_thread(pipeline.on_bar,symbol,msg,received_at,allow)
      elif kind=="TRADE" and symbol:await asyncio.to_thread(pipeline.on_trade,symbol,msg,received_at,allow)
      elif kind=="STATUS":
        await asyncio.to_thread(rec.on_status,msg);await asyncio.to_thread(pipeline.on_status,msg)
      rec.on_stream_message(kind,msg)
    async def on_disconnect():
      # Read-only timestamped evidence; original trust/recovery actions stay identical.
      try:print({"stage":"SHADOW_WEBSOCKET_DISCONNECT","utc":datetime.now(timezone.utc).isoformat(),"error_type":str(ws.last_error or "UNKNOWN").split(":",1)[0]},flush=True)
      except Exception:pipeline.disconnect_log_errors=getattr(pipeline,"disconnect_log_errors",0)+1
      orch.on_disconnect();rec.on_disconnect()
    ws=WebSocketRuntime(connector,AlpacaSIPProtocol,on_message,on_disconnect)
    supervisor=ShadowRuntimeSupervisor(orch,ws,sender,outbox_store,syms,
        config.alpaca_key,config.alpaca_secret,SIP_STREAM_URL)
    supervisor.decision_pipeline=pipeline
    if policy is not None:
        def prepare_subscription():
            # No transport update in-place. Reconnects retain this startup scope.
            if policy.epoch or supervisor.websocket_runtime.connected_event.is_set():
                raise RuntimeError("SUBSCRIPTION_SCOPE_ALREADY_FROZEN_RESTART_REQUIRED")
            scope=policy.freeze(r)
            syms[:]=scope
            pipeline.symbols=list(scope);pipeline._admitted_symbols=set(scope)
            if hasattr(rec,"symbols"):rec.symbols=list(scope)
            print({"stage":"SUBSCRIPTION_EPOCH_PREPARED",**policy.snapshot()},flush=True)
        def verify_subscription_needs():
            policy.observe_redis(r)
            if policy.pending or policy.failure=="CANONICAL_SCOPE_OBSERVATION_FAILED":
                raise RuntimeError("SUBSCRIPTION_NEEDS_CHANGED_BEFORE_CONNECT")
        supervisor.verify_subscription_needs=verify_subscription_needs
        supervisor.prepare_subscription_epoch=prepare_subscription
        supervisor.subscription_policy=policy
    return supervisor
