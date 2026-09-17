from __future__ import annotations
from runtime_provenance import stamp
from dataclasses import dataclass
from typing import Mapping, Any
from state_store import CanonicalStateStore, base_record, CanonicalConflict
from leadership import LeadershipManager, NotLeader

class EarlyCorePersistenceError(RuntimeError): pass

@dataclass
class EarlyCoreStateWriter:
    store: CanonicalStateStore
    leadership: LeadershipManager

    def persist_first_e(self, crossing):
        token=self.leadership.require_current()
        r=base_record(
            "early_core",
            crossing.session,
            crossing.symbol,
            "FIRST_CROSSING",
            crossing.decision_available_ts.isoformat(),
        )
        r.update(
            source="alpaca_sip",
            source_timeframe="native_5Min",
            bar_start_ts=crossing.bar_start_ts.isoformat(),
            bar_end_ts=crossing.bar_end_ts.isoformat(),
            received_at=crossing.received_at.isoformat(),
            decision_available_ts=crossing.decision_available_ts.isoformat(),
            score=float(crossing.score),
            threshold=0.5205528990060366,
            observed_weight=float(crossing.observed_weight),
            leader_generation=token.leader_generation,
            worker_instance_id=token.worker_instance_id,
        )
        r=stamp(r)
        key=self.store.create(r)
        return key, r

    def read_first_e(self, session, symbol):
        key=f"operational_priority_radar:v1:early_core:{session}:{symbol}"
        return self.store.read(key,"early_core")
