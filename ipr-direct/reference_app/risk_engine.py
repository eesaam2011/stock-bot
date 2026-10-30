from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from constants import STRUCTURAL_STOP_BUFFER,MAX_INITIAL_RISK_PCT,T1_R_MULTIPLE,T2_R_MULTIPLE

class RiskStatus(str,Enum):
    APPROVED="RISK_APPROVED"
    NO_STRUCTURE="ENTRY_REJECTED_NO_VALID_STRUCTURE_FINAL"
    STRUCTURAL_RISK="ENTRY_REJECTED_STRUCTURAL_RISK_FINAL"

@dataclass(frozen=True)
class StructureBar:
    bar_start_ts:datetime
    bar_end_ts:datetime
    low:float
    received_at:datetime
    source_timeframe:str="native_1Min"
    trusted:bool=True

@dataclass(frozen=True)
class RiskDecision:
    status:RiskStatus
    structure_low:float|None=None
    structural_stop:float|None=None
    risk_pct:float|None=None
    t1:float|None=None
    t2:float|None=None

def evaluate_structural_risk(entry_price,first_event_decision_available_ts,entry_trigger_ts,bars):
    eligible=[]
    for b in bars:
        if b.source_timeframe!="native_1Min" or not b.trusted or b.low<=0:continue
        if b.bar_end_ts<first_event_decision_available_ts or b.bar_end_ts>entry_trigger_ts:continue
        # Causal availability: bar must actually have been known no later than Entry decision/trigger.
        if b.received_at>entry_trigger_ts:continue
        eligible.append(b)
    if not eligible:
        return RiskDecision(RiskStatus.NO_STRUCTURE)
    structure_low=min(b.low for b in eligible)
    stop=structure_low*(1.0-STRUCTURAL_STOP_BUFFER)
    risk=entry_price-stop
    risk_pct=(risk/entry_price)*100.0 if entry_price>0 else float("inf")
    if not (risk>0 and risk_pct<=MAX_INITIAL_RISK_PCT):
        return RiskDecision(RiskStatus.STRUCTURAL_RISK,structure_low,stop,risk_pct)
    return RiskDecision(RiskStatus.APPROVED,structure_low,stop,risk_pct,
                        entry_price+T1_R_MULTIPLE*risk,
                        entry_price+T2_R_MULTIPLE*risk)
