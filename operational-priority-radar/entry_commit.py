from dataclasses import dataclass
from datetime import timedelta
from state_store import base_record
from event_ids import entry_event_id
from risk_engine import RiskStatus
from constants import MONITORING_HORIZON_SECONDS

@dataclass
class EntryCommitBuilder:
    leadership: object

    def build(self, expected_opportunity, risk_decision, entry_price, entry_alert_sent_at):
        if risk_decision.status != RiskStatus.APPROVED:
            raise ValueError("risk must be approved before ENTRY_COMMITTED")
        if expected_opportunity["state"] not in {"CONFLUENCE_VALID","ENTRY_DATA_WAIT","ENTRY_PRICE_READY"}:
            raise ValueError("invalid opportunity state for entry commit")
        token=self.leadership.require_current()
        session=expected_opportunity["session"];symbol=expected_opportunity["symbol"]
        opportunity_id=(expected_opportunity.get("event_id") or
                        f'{session}:{symbol}:{expected_opportunity["entry_trigger_ts"]}')
        eid=entry_event_id(session,symbol,opportunity_id)
        trade_id=f"TRADE:{session}:{symbol}:{opportunity_id}"

        new_state=dict(expected_opportunity)
        new_state.update(
            state="ENTRY_COMMITTED",updated_at=entry_alert_sent_at.isoformat(),
            terminal_reason=None,entry_alert_price=float(entry_price),
            structure_low=float(risk_decision.structure_low),
            structural_stop=float(risk_decision.structural_stop),
            risk_pct=float(risk_decision.risk_pct),
            t1=float(risk_decision.t1),t2=float(risk_decision.t2),
            trade_id=trade_id,entry_event_id=eid,
            entry_alert_sent_at=entry_alert_sent_at.isoformat(),
            leader_generation=token.leader_generation,worker_instance_id=token.worker_instance_id)

        trade=base_record("trade",session,symbol,"ACTIVE_PRE_T1",entry_alert_sent_at.isoformat())
        trade.update(
            trade_id=trade_id,entry_alert_price=float(entry_price),
            structure_low=float(risk_decision.structure_low),
            structural_stop=float(risk_decision.structural_stop),
            risk_pct=float(risk_decision.risk_pct),t1=float(risk_decision.t1),t2=float(risk_decision.t2),
            monitoring_deadline=(entry_alert_sent_at+timedelta(seconds=MONITORING_HORIZON_SECONDS)).isoformat(),
            leader_generation=token.leader_generation,worker_instance_id=token.worker_instance_id)

        outbox=base_record("outbox",session,symbol,"PENDING",entry_alert_sent_at.isoformat())
        outbox.update(
            event_id=eid,event_type="ENTRY",
            payload={"symbol":symbol,"entry_alert_price":float(entry_price),
                     "structural_stop":float(risk_decision.structural_stop),
                     "risk_pct":float(risk_decision.risk_pct),
                     "t1":float(risk_decision.t1),"t2":float(risk_decision.t2),
                     "trade_id":trade_id,"entry_alert_sent_at":entry_alert_sent_at.isoformat()},
            attempt_count=0,leader_generation=token.leader_generation)
        return new_state,trade,outbox
