from runtime_provenance import stamp
from dataclasses import dataclass
from state_store import CanonicalStateStore,base_record
from leadership import LeadershipManager

@dataclass
class BaseReadyStateWriter:
    store: CanonicalStateStore
    leadership: LeadershipManager

    def persist_first_b(self,session,symbol,bar_start_ts,bar_end_ts,received_at,decision_available_ts,features,diagnostics):
        token=self.leadership.require_current()
        payload={
            "opportunity":float(features["opportunity"]),
            "failure_pressure":float(features["failure_pressure"]),
            "price":float(diagnostics["price"]),
            "vwap":float(diagnostics["vwap"]),
            "demand_efficiency":float(diagnostics["demand_efficiency"]),
            "price_acceptance":float(diagnostics["price_acceptance"]),
            "volume_acceleration":float(diagnostics["volume_acceleration"]),
        }
        r=base_record("base_ready",session,symbol,"FIRST_TRUE",decision_available_ts.isoformat())
        r.update(features=payload,source="alpaca_sip",source_timeframe="completed_1Min",
                 bar_start_ts=bar_start_ts.isoformat(),bar_end_ts=bar_end_ts.isoformat(),
                 received_at=received_at.isoformat(),decision_available_ts=decision_available_ts.isoformat(),
                 leader_generation=token.leader_generation,worker_instance_id=token.worker_instance_id)
        r=stamp(r)
        return self.store.create(r),r

    def read_first_b(self,session,symbol):
        return self.store.read(f"operational_priority_radar:v1:base_ready:{session}:{symbol}","base_ready")
