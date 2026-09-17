from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from typing import Mapping, Any
from state_store import CanonicalStateStore, canonical_json, validate_record, record_key, CanonicalConflict
from leadership import LeadershipManager, NotLeader

class AtomicCommitError(RuntimeError): pass

class InMemoryAtomicBackend:
    """Test-only two-key atomic primitive with crash injection."""
    def __init__(self, state_backend):
        self.state_backend = state_backend

    def commit_state_and_outbox(self, state_key, expected_raw, new_state_raw, outbox_key, outbox_raw, crash_point=None):
        if crash_point == "before_commit":
            raise AtomicCommitError("injected crash before commit")

        current = self.state_backend.get(state_key)
        if current != expected_raw:
            raise CanonicalConflict("state changed before atomic commit")
        existing_outbox = self.state_backend.get(outbox_key)
        if existing_outbox is not None and existing_outbox != outbox_raw:
            raise CanonicalConflict("conflicting deterministic outbox event")

        if crash_point == "during_commit":
            # Atomic contract: injected failure occurs before either write becomes visible.
            raise AtomicCommitError("injected crash during atomic commit")

        self.state_backend._data[state_key] = new_state_raw
        self.state_backend._data[outbox_key] = outbox_raw
        return True

@dataclass
class AtomicDecisionWriter:
    store: CanonicalStateStore
    atomic_backend: InMemoryAtomicBackend
    leadership: LeadershipManager

    def commit(self, expected_state: Mapping[str,Any], new_state: Mapping[str,Any],
               outbox_record: Mapping[str,Any], crash_point=None) -> None:
        token = self.leadership.require_current()
        validate_record(expected_state)
        validate_record(new_state)
        validate_record(outbox_record, "outbox")

        if expected_state["record_type"] != new_state["record_type"]:
            raise AtomicCommitError("state record_type cannot change")
        if outbox_record["leader_generation"] != token.leader_generation:
            raise AtomicCommitError("outbox fencing generation mismatch")

        state_key = record_key(expected_state)
        if record_key(new_state) != state_key:
            raise AtomicCommitError("canonical state key cannot change")
        outbox_key = record_key(outbox_record)

        self.atomic_backend.commit_state_and_outbox(
            state_key,
            canonical_json(expected_state),
            canonical_json(new_state),
            outbox_key,
            canonical_json(outbox_record),
            crash_point=crash_point,
        )
