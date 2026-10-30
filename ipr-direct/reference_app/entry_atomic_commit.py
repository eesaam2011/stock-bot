from dataclasses import dataclass
from state_store import canonical_json,record_key,validate_record,CanonicalConflict
from atomic_commit import AtomicCommitError

class InMemoryEntryAtomicBackend:
    def __init__(self,state_backend):self.state_backend=state_backend
    def commit(self,state_key,expected_raw,new_raw,trade_key,trade_raw,outbox_key,outbox_raw,crash_point=None):
        if crash_point=="before_commit":raise AtomicCommitError("injected crash before commit")
        if self.state_backend.get(state_key)!=expected_raw:raise CanonicalConflict("opportunity changed")
        for k,v in ((trade_key,trade_raw),(outbox_key,outbox_raw)):
            existing=self.state_backend.get(k)
            if existing is not None and existing!=v:raise CanonicalConflict("conflicting deterministic record")
        if crash_point in {"between_state_trade","between_trade_outbox","during_commit"}:
            raise AtomicCommitError("injected atomic failure")
        self.state_backend._data[state_key]=new_raw
        self.state_backend._data[trade_key]=trade_raw
        self.state_backend._data[outbox_key]=outbox_raw

@dataclass
class AtomicEntryWriter:
    backend:InMemoryEntryAtomicBackend
    leadership:object
    def commit(self,expected,new_state,trade,outbox,crash_point=None):
        token=self.leadership.require_current()
        for r in (expected,new_state,trade,outbox):validate_record(r)
        if new_state["leader_generation"]!=token.leader_generation or trade["leader_generation"]!=token.leader_generation or outbox["leader_generation"]!=token.leader_generation:
            raise AtomicCommitError("fencing generation mismatch")
        self.backend.commit(record_key(expected),canonical_json(expected),canonical_json(new_state),
                            record_key(trade),canonical_json(trade),record_key(outbox),canonical_json(outbox),crash_point)
