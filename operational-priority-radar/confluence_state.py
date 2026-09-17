from runtime_provenance import stamp
from dataclasses import dataclass
from state_store import CanonicalStateStore,base_record
from leadership import LeadershipManager
from operational_confluence import decide_confluence,ConfluenceStatus

@dataclass
class ConfluenceStateWriter:
    store:CanonicalStateStore
    leadership:LeadershipManager

    def evaluate_and_persist(self,session,symbol,e_record,b_record,now):
        e_ts=None if e_record is None else __import__("datetime").datetime.fromisoformat(e_record["decision_available_ts"])
        b_ts=None if b_record is None else __import__("datetime").datetime.fromisoformat(b_record["decision_available_ts"])
        d=decide_confluence(e_ts,b_ts,now)
        if d.status==ConfluenceStatus.WAITING:return d,None
        token=self.leadership.require_current()
        e_id=None if e_record is None else e_record["event_id"]
        b_id=None if b_record is None else b_record["event_id"]
        event_ts=(d.entry_trigger_ts or d.expiry).isoformat()
        r=base_record("opportunity",session,symbol,d.status.value,event_ts)
        r.update(e_id=e_id,b_id=b_id,delta_seconds=d.delta_seconds,first_event=d.first_event,
                 entry_trigger_ts=None if d.entry_trigger_ts is None else d.entry_trigger_ts.isoformat(),
                 expiry=None if d.expiry is None else d.expiry.isoformat(),
                 terminal_reason=None if d.status==ConfluenceStatus.VALID else "OPPORTUNITY_EXPIRED",
                 leader_generation=token.leader_generation,worker_instance_id=token.worker_instance_id)
        r=stamp(r)
        return d,self.store.create(r)
