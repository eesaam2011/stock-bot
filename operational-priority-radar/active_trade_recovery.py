import json,math
from datetime import datetime,timedelta,timezone
from itertools import groupby,permutations
from trade_monitor import TradeState,MarketEvent,apply_event,MonitorEvent
from state_store import key_trade,key_outbox,canonical_json,base_record,validate_record
from event_ids import trade_event_id
from redis_lua_production import ProductionRedisLua
UTC=timezone.utc
TERMINAL={"CLOSED_STOP","CLOSED_T2","CLOSED_POST_T1_BREAKEVEN_EXIT","MONITORING_EXPIRED","RECOVERY_PATH_AMBIGUOUS"}
EVENT_MAP={"T1":"T1","T2":"T2","STOP":"STOP","POST_T1_EXIT":"POST_T1_EXIT","MONITORING_EXPIRED":"FINAL","RECOVERY_PATH_AMBIGUOUS":"FINAL"}

class ActiveTradeRecoveryError(RuntimeError):pass

def parse_ts(v):
    if isinstance(v,datetime):return v
    return datetime.fromisoformat(str(v).replace("Z","+00:00"))

class ActiveTradeChronologicalReconciler:
    def __init__(self,rest,redis_client,worker_id,now_fn=None,leadership=None):
        self.rest=rest;self.r=redis_client;self.worker_id=worker_id
        self.lua=ProductionRedisLua(redis_client);self.now_fn=now_fn or (lambda:datetime.now(UTC))
        # Set after lease acquisition in production composition; absent means
        # recovery cannot commit, not permission to read a fresh generation.
        self.leadership=leadership
        self.cancel_event=None
    def _require_not_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ActiveTradeRecoveryError("RECOVERY_CANCELLED")

    def _state(self,t):
        pre=t.get("pre_halt_state")
        return TradeState(t["state"],float(t["entry_alert_price"]),float(t["structural_stop"]),float(t["t1"]),float(t["t2"]),parse_ts(t["monitoring_deadline"]),pre)

    def _events(self,t,start,end):
        self._require_not_cancelled()
        # Both page chains must terminate before a recovered trade can be
        # committed. A truncated trade tape could hide an earlier STOP/T2.
        if hasattr(self.rest,"trades_audited"):
            trades,trade_audit=self.rest.trades_audited(t["symbol"],start,end)
            if not trade_audit.get("api_pagination_exhausted"):
                raise ActiveTradeRecoveryError("TRADE_PAGINATION_UNPROVEN")
        else:
            trades=self.rest.trades(t["symbol"],start,end)
        self._require_not_cancelled()
        if hasattr(self.rest,"bars_audited"):
            bars,bar_audit=self.rest.bars_audited(t["symbol"],start,end,"1Min")
            if not bar_audit.get("api_pagination_exhausted"):
                raise ActiveTradeRecoveryError("BAR_PAGINATION_UNPROVEN")
        else:
            bars=self.rest.native_1m_gap(t["symbol"],start,end)
        self._require_not_cancelled()
        ev=[];seq=0
        for x in trades:
            # Never silently drop a malformed historical trade: it could
            # be the missing first stop breach or T2 event.
            if not isinstance(x,dict) or x.get("t") is None or x.get("p") is None:
                raise ActiveTradeRecoveryError("MALFORMED_RECOVERED_TRADE")
            try:
                ts=parse_ts(x["t"]);price=float(x["p"])
            except (ValueError,TypeError,OverflowError) as exc:
                raise ActiveTradeRecoveryError("MALFORMED_RECOVERED_TRADE") from exc
            if (ts.tzinfo is None or not start<=ts<=end
                or not math.isfinite(price) or price<=0):
                raise ActiveTradeRecoveryError("INVALID_RECOVERED_TRADE_EVENT")
            ev.append(MarketEvent("TRADE",ts,seq,price,True));seq+=1
        for b in bars:
            if not isinstance(b,dict) or b.get("t") is None or b.get("c") is None:
                raise ActiveTradeRecoveryError("MALFORMED_RECOVERED_BAR")
            try:
                ts=parse_ts(b["t"])+timedelta(minutes=1);close=float(b["c"])
            except (ValueError,TypeError,OverflowError) as exc:
                raise ActiveTradeRecoveryError("MALFORMED_RECOVERED_BAR") from exc
            # Native 1Min timestamp is bar start; close becomes available at bar end.
            if (ts.tzinfo is None or not start<=ts<=end
                or not math.isfinite(close) or close<=0):
                raise ActiveTradeRecoveryError("INVALID_RECOVERED_BAR_EVENT")
            ev.append(MarketEvent("BAR_CLOSE",ts,seq,close,True));seq+=1
        return sorted(ev,key=lambda e:(e.event_ts,e.sequence))

    def _group_ambiguous(self,state,group):
        """Exact bounded same-timestamp ordering audit, including outbox events.

        REST event timestamps do not establish order for simultaneous trades
        and bar closes. A T1->STOP trace differs from STOP alone even if both
        end CLOSED_STOP; T1 must not be committed on such evidence.
        """
        # Identical observations have the same state effects, so collapse
        # only for the ambiguity audit, not the actual event replay.
        unique={}
        for e in group:
            unique[(e.kind,e.price,e.eligible)]=e
        events=list(unique.values())
        if len(events)<=1:return False
        if len(events)>7:
            # Avoid factorial work under a busy SIP timestamp. If every
            # observation is a no-op now, their order cannot change state.
            if all(apply_event(state,e)[1]==MonitorEvent.NONE for e in events):
                return False
            return True
        reference=None
        for order in permutations(events):
            current=state;emitted=[]
            for e in order:
                current,evt=apply_event(current,e)
                if evt!=MonitorEvent.NONE:emitted.append(evt.value)
                if current.state in TERMINAL:break
            outcome=(current,tuple(emitted))
            if reference is None:reference=outcome
            elif outcome!=reference:return True
        return False

    def _preflight_chronology(self,state,events):
        """Audit all recoverable history BEFORE the first fenced Redis write.

        A later ambiguous timestamp must not leave an earlier partial T1
        transition or an outbox event committed.
        """
        current=state
        for _,items in groupby(events,key=lambda e:e.event_ts):
            self._require_not_cancelled()
            group=list(items)
            if self._group_ambiguous(current,group):
                return {"ambiguous":True,"reason":"RECOVERY_PATH_AMBIGUOUS",
                        "event_ts":group[0].event_ts.isoformat()}
            for e in group:
                current,_=apply_event(current,e)
                if current.state in TERMINAL:
                    return {"ambiguous":False}
        return {"ambiguous":False}

    def _commit(self,expected,new_state,event_name,event_ts,breach_price=None):
        self._require_not_cancelled()
        if self.leadership is None:
            raise ActiveTradeRecoveryError("RECOVERY_LEADERSHIP_UNBOUND")
        token=self.leadership.require_current()
        if token.worker_instance_id!=self.worker_id:
            raise ActiveTradeRecoveryError("RECOVERY_WORKER_ID_MISMATCH")
        generation=token.leader_generation
        new=dict(expected);new["state"]=new_state;new["updated_at"]=event_ts.isoformat();new["leader_generation"]=generation;new["worker_instance_id"]=self.worker_id
        et=EVENT_MAP[event_name];eid=trade_event_id(et,expected["trade_id"])
        out=base_record("outbox",expected["session"],expected["symbol"],"PENDING",event_ts.isoformat())
        payload={"trade_id":expected["trade_id"],"event":event_name,"event_ts":event_ts.isoformat(),"recovered":True}
        if breach_price is not None:payload["first_observed_breach_price"]=breach_price
        out.update(event_id=eid,event_type=et,payload=payload,attempt_count=0,leader_generation=generation)
        validate_record(new);validate_record(out)
        self._require_not_cancelled()
        self.lua.atomic_trade_event(self.worker_id,key_trade(expected["trade_id"]),canonical_json(expected),canonical_json(new),key_outbox(eid),canonical_json(out),generation)
        return new

    def reconcile(self,trade):
        self._require_not_cancelled()
        if trade["state"]=="HALTED_ACTIVE":return {"ambiguous":True,"reason":"HALTED_ACTIVE_REQUIRES_STATUS_RECONCILIATION"}
        start=parse_ts(trade.get("updated_at") or trade.get("entry_alert_sent_at"))
        now=self.now_fn();events=self._events(trade,start,now);state=self._state(trade);current=dict(trade)
        preflight=self._preflight_chronology(state,events)
        if preflight["ambiguous"]:return preflight
        self._require_not_cancelled()
        for _,g in groupby(events,key=lambda e:e.event_ts):
            self._require_not_cancelled()
            group=list(g)
            for e in group:
                ns,evt=apply_event(state,e)
                if evt!=MonitorEvent.NONE:
                    if evt in {MonitorEvent.HALT,MonitorEvent.RESUME}:return {"ambiguous":True,"reason":"STATUS_EVENT_REQUIRES_STATUS_STREAM"}
                    current=self._commit(current,ns.state,evt.value,e.event_ts,e.price if evt==MonitorEvent.STOP else None)
                    state=ns
                    if state.state in TERMINAL:return {"ambiguous":False,"state":state.state,"trade":current}
                else:state=ns
        if now>state.monitoring_deadline and state.state not in TERMINAL:
            expiry_ts=state.monitoring_deadline+timedelta(microseconds=1)
            ns,evt=apply_event(state,MarketEvent("TIMEOUT",expiry_ts,10**12,None,True))
            current=self._commit(current,ns.state,evt.value,expiry_ts);state=ns
        return {"ambiguous":False,"state":state.state,"trade":current}
