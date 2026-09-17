from models import (
    OpportunityState, TERMINAL_OPPORTUNITY_STATES,
    TradeState, TERMINAL_TRADE_STATES,
)

_OPPORTUNITY_ALLOWED = {
    OpportunityState.NONE: {OpportunityState.E_ONLY, OpportunityState.B_ONLY},
    OpportunityState.E_ONLY: {OpportunityState.CONFLUENCE_VALID, OpportunityState.OPPORTUNITY_EXPIRED},
    OpportunityState.B_ONLY: {OpportunityState.CONFLUENCE_VALID, OpportunityState.OPPORTUNITY_EXPIRED},
    OpportunityState.CONFLUENCE_VALID: {OpportunityState.ENTRY_DATA_WAIT},
    OpportunityState.ENTRY_DATA_WAIT: {
        OpportunityState.ENTRY_COMMITTED,
        OpportunityState.ENTRY_EXPIRED_STALE_MARKET_DATA,
        OpportunityState.ENTRY_BLOCKED_HALT_FINAL,
        OpportunityState.ENTRY_REJECTED_NO_VALID_STRUCTURE_FINAL,
        OpportunityState.ENTRY_REJECTED_STRUCTURAL_RISK_FINAL,
        OpportunityState.OPPORTUNITY_MISSED_DURING_DATA_GAP,
    },
}

_TRADE_ALLOWED = {
    TradeState.ACTIVE_PRE_T1: {
        TradeState.ACTIVE_POST_T1,
        TradeState.CLOSED_STOP,
        TradeState.MONITORING_EXPIRED,
        TradeState.HALTED_ACTIVE,
    },
    TradeState.ACTIVE_POST_T1: {
        TradeState.CLOSED_STOP,
        TradeState.CLOSED_T2,
        TradeState.CLOSED_POST_T1_BREAKEVEN_EXIT,
        TradeState.MONITORING_EXPIRED,
        TradeState.HALTED_ACTIVE,
    },
    TradeState.HALTED_ACTIVE: {
        TradeState.ACTIVE_PRE_T1,
        TradeState.ACTIVE_POST_T1,
        TradeState.RECOVERY_PATH_AMBIGUOUS,
    },
}

def validate_opportunity_transition(old: OpportunityState, new: OpportunityState) -> None:
    if old in TERMINAL_OPPORTUNITY_STATES:
        raise ValueError(f"terminal opportunity state cannot transition: {old}")
    if new not in _OPPORTUNITY_ALLOWED.get(old, set()):
        raise ValueError(f"invalid opportunity transition: {old} -> {new}")

def validate_trade_transition(old: TradeState, new: TradeState) -> None:
    if old in TERMINAL_TRADE_STATES:
        raise ValueError(f"terminal trade state cannot transition: {old}")
    if new not in _TRADE_ALLOWED.get(old, set()):
        raise ValueError(f"invalid trade transition: {old} -> {new}")
