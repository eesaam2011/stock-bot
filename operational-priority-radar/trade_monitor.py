from dataclasses import dataclass,replace
from datetime import datetime
from enum import Enum

class MonitorEvent(str,Enum):
    NONE="NONE";T1="T1";T2="T2";STOP="STOP";POST_T1_EXIT="POST_T1_EXIT"
    HALT="HALT";RESUME="RESUME";MONITORING_EXPIRED="MONITORING_EXPIRED"
    RECOVERY_AMBIGUOUS="RECOVERY_PATH_AMBIGUOUS"

@dataclass(frozen=True)
class TradeState:
    state:str
    entry:float
    stop:float
    t1:float
    t2:float
    monitoring_deadline:datetime
    pre_halt_state:str|None=None

@dataclass(frozen=True)
class MarketEvent:
    kind:str                 # TRADE, BAR_CLOSE, HALT, RESUME, RECOVERY_AMBIGUOUS
    event_ts:datetime
    sequence:int
    price:float|None=None
    eligible:bool=True

def apply_event(s:TradeState,e:MarketEvent):
    if s.state in {"CLOSED_STOP","CLOSED_T2","CLOSED_POST_T1_BREAKEVEN_EXIT","MONITORING_EXPIRED","RECOVERY_PATH_AMBIGUOUS"}:
        return s,MonitorEvent.NONE

    if e.kind=="RECOVERY_AMBIGUOUS":
        return replace(s,state="RECOVERY_PATH_AMBIGUOUS"),MonitorEvent.RECOVERY_AMBIGUOUS

    if e.kind=="HALT":
        if s.state=="HALTED_ACTIVE": return s,MonitorEvent.NONE
        return replace(s,state="HALTED_ACTIVE",pre_halt_state=s.state),MonitorEvent.HALT

    if s.state=="HALTED_ACTIVE":
        if e.kind=="RESUME":
            if s.pre_halt_state not in {"ACTIVE_PRE_T1","ACTIVE_POST_T1"}:
                return replace(s,state="RECOVERY_PATH_AMBIGUOUS"),MonitorEvent.RECOVERY_AMBIGUOUS
            return replace(s,state=s.pre_halt_state,pre_halt_state=None),MonitorEvent.RESUME
        return s,MonitorEvent.NONE

    # Deadline is a monitoring event; market events at/before deadline retain priority.
    if e.event_ts>s.monitoring_deadline:
        return replace(s,state="MONITORING_EXPIRED"),MonitorEvent.MONITORING_EXPIRED

    if not e.eligible:return s,MonitorEvent.NONE

    if e.kind=="TRADE":
        if e.price is None:return s,MonitorEvent.NONE
        # Structural stop is always active, including after T1.
        if e.price<=s.stop:
            return replace(s,state="CLOSED_STOP"),MonitorEvent.STOP
        if s.state=="ACTIVE_PRE_T1":
            if e.price>=s.t2:
                return replace(s,state="CLOSED_T2"),MonitorEvent.T2
            if e.price>=s.t1:
                return replace(s,state="ACTIVE_POST_T1"),MonitorEvent.T1
        elif s.state=="ACTIVE_POST_T1":
            if e.price>=s.t2:
                return replace(s,state="CLOSED_T2"),MonitorEvent.T2

    if e.kind=="BAR_CLOSE" and s.state=="ACTIVE_POST_T1" and e.price is not None and e.price<=s.entry:
        return replace(s,state="CLOSED_POST_T1_BREAKEVEN_EXIT"),MonitorEvent.POST_T1_EXIT

    return s,MonitorEvent.NONE

def process_chronological(s,events):
    # actual event chronology: event_ts then SIP/engine sequence. First terminal transition wins.
    for e in sorted(events,key=lambda x:(x.event_ts,x.sequence)):
        s,ev=apply_event(s,e)
        if s.state in {"CLOSED_STOP","CLOSED_T2","CLOSED_POST_T1_BREAKEVEN_EXIT","MONITORING_EXPIRED","RECOVERY_PATH_AMBIGUOUS"}:
            return s,ev
    return s,MonitorEvent.NONE
