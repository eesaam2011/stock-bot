from dataclasses import dataclass
from state_store import base_record,record_key,canonical_json,validate_record,CanonicalConflict
from event_ids import trade_event_id
from leadership import NotLeader

EVENT_MAP={"T1":"T1","T2":"T2","STOP":"STOP","POST_T1_EXIT":"POST_T1_EXIT","MONITORING_EXPIRED":"FINAL","RECOVERY_PATH_AMBIGUOUS":"FINAL"}

class AtomicTradeTransitionError(RuntimeError):pass

class InMemoryTradeTransitionBackend:
    def __init__(self,state_backend):self.b=state_backend
    def commit(self,trade_key,expected_raw,new_raw,outbox_key,outbox_raw,crash=None):
        if crash=="before":raise AtomicTradeTransitionError("crash before")
        if self.b.get(trade_key)!=expected_raw:raise CanonicalConflict("trade changed")
        old=self.b.get(outbox_key)
        if old is not None and old!=outbox_raw:raise CanonicalConflict("outbox conflict")
        if crash in {"between","during"}:raise AtomicTradeTransitionError("atomic failure")
        self.b._data[trade_key]=new_raw;self.b._data[outbox_key]=outbox_raw

@dataclass
class TradeTransitionWriter:
    backend:InMemoryTradeTransitionBackend
    leadership:object
    def commit(self,expected_trade,new_state,event_name,event_ts,payload_extra=None,crash=None):
        token=self.leadership.require_current()
        et=EVENT_MAP.get(event_name)
        if et is None:raise ValueError("transition has no Telegram event")
        new=dict(expected_trade);new["state"]=new_state;new["updated_at"]=event_ts.isoformat()
        new["leader_generation"]=token.leader_generation;new["worker_instance_id"]=token.worker_instance_id
        eid=trade_event_id(et,expected_trade["trade_id"])
        out=base_record("outbox",expected_trade["session"],expected_trade["symbol"],"PENDING",event_ts.isoformat())
        payload={"trade_id":expected_trade["trade_id"],"event":event_name,"event_ts":event_ts.isoformat()}
        if payload_extra:payload.update(payload_extra)
        out.update(event_id=eid,event_type=et,payload=payload,attempt_count=0,leader_generation=token.leader_generation)
        validate_record(new);validate_record(out)
        self.backend.commit(record_key(expected_trade),canonical_json(expected_trade),canonical_json(new),record_key(out),canonical_json(out),crash)
        return new,out
