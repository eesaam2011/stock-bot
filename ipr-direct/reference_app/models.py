from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

class MarketTrustState(str, Enum):
    STARTING = "STARTING"
    RECOVERING_GAP = "RECOVERING_GAP"
    CONNECTING_STREAM = "CONNECTING_STREAM"
    VERIFYING_CONTINUITY = "VERIFYING_CONTINUITY"
    LIVE_TRUSTED = "LIVE_TRUSTED"
    STREAM_UNTRUSTED = "STREAM_UNTRUSTED"
    RECONNECTING = "RECONNECTING"

class OpportunityState(str, Enum):
    NONE = "NONE"
    E_ONLY = "E_ONLY"
    B_ONLY = "B_ONLY"
    CONFLUENCE_VALID = "CONFLUENCE_VALID"
    ENTRY_DATA_WAIT = "ENTRY_DATA_WAIT"
    ENTRY_COMMITTED = "ENTRY_COMMITTED"
    OPPORTUNITY_EXPIRED = "OPPORTUNITY_EXPIRED"
    ENTRY_EXPIRED_STALE_MARKET_DATA = "ENTRY_EXPIRED_STALE_MARKET_DATA"
    ENTRY_BLOCKED_HALT_FINAL = "ENTRY_BLOCKED_HALT_FINAL"
    ENTRY_REJECTED_NO_VALID_STRUCTURE_FINAL = "ENTRY_REJECTED_NO_VALID_STRUCTURE_FINAL"
    ENTRY_REJECTED_STRUCTURAL_RISK_FINAL = "ENTRY_REJECTED_STRUCTURAL_RISK_FINAL"
    OPPORTUNITY_MISSED_DURING_DATA_GAP = "OPPORTUNITY_MISSED_DURING_DATA_GAP"

TERMINAL_OPPORTUNITY_STATES = frozenset({
    OpportunityState.ENTRY_COMMITTED,
    OpportunityState.OPPORTUNITY_EXPIRED,
    OpportunityState.ENTRY_EXPIRED_STALE_MARKET_DATA,
    OpportunityState.ENTRY_BLOCKED_HALT_FINAL,
    OpportunityState.ENTRY_REJECTED_NO_VALID_STRUCTURE_FINAL,
    OpportunityState.ENTRY_REJECTED_STRUCTURAL_RISK_FINAL,
    OpportunityState.OPPORTUNITY_MISSED_DURING_DATA_GAP,
})

class TradeState(str, Enum):
    ACTIVE_PRE_T1 = "ACTIVE_PRE_T1"
    ACTIVE_POST_T1 = "ACTIVE_POST_T1"
    HALTED_ACTIVE = "HALTED_ACTIVE"
    CLOSED_STOP = "CLOSED_STOP"
    CLOSED_T2 = "CLOSED_T2"
    CLOSED_POST_T1_BREAKEVEN_EXIT = "CLOSED_POST_T1_BREAKEVEN_EXIT"
    MONITORING_EXPIRED = "MONITORING_EXPIRED"
    RECOVERY_PATH_AMBIGUOUS = "RECOVERY_PATH_AMBIGUOUS"

TERMINAL_TRADE_STATES = frozenset({
    TradeState.CLOSED_STOP,
    TradeState.CLOSED_T2,
    TradeState.CLOSED_POST_T1_BREAKEVEN_EXIT,
    TradeState.MONITORING_EXPIRED,
    TradeState.RECOVERY_PATH_AMBIGUOUS,
})

@dataclass(frozen=True)
class CausalEvent:
    session: str
    symbol: str
    source: str
    received_at: datetime
    decision_available_ts: datetime
    leader_generation: int
    bar_start_ts: Optional[datetime] = None
    bar_end_ts: Optional[datetime] = None
    recovered_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.bar_end_ts and self.bar_start_ts and self.bar_end_ts < self.bar_start_ts:
            raise ValueError("bar_end_ts cannot precede bar_start_ts")
        if self.decision_available_ts < self.received_at:
            raise ValueError("decision_available_ts cannot precede received_at")
        if self.bar_end_ts and self.decision_available_ts < self.bar_end_ts:
            raise ValueError("decision_available_ts cannot precede bar_end_ts")

@dataclass
class TradeRuntimeState:
    state: TradeState
    pre_halt_state: Optional[TradeState] = None

    def halt(self) -> None:
        if self.state in TERMINAL_TRADE_STATES:
            raise ValueError("terminal trade cannot be halted")
        if self.state == TradeState.HALTED_ACTIVE:
            return
        if self.state not in (TradeState.ACTIVE_PRE_T1, TradeState.ACTIVE_POST_T1):
            raise ValueError("only active trade states may halt")
        self.pre_halt_state = self.state
        self.state = TradeState.HALTED_ACTIVE

    def resume(self) -> None:
        if self.state != TradeState.HALTED_ACTIVE:
            raise ValueError("trade is not halted")
        if self.pre_halt_state not in (TradeState.ACTIVE_PRE_T1, TradeState.ACTIVE_POST_T1):
            raise ValueError("invalid pre_halt_state")
        self.state = self.pre_halt_state
        self.pre_halt_state = None
