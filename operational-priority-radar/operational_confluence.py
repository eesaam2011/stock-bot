from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

WINDOW_SECONDS=900

class ConfluenceStatus(str,Enum):
    WAITING="WAITING"
    VALID="CONFLUENCE_VALID"
    EXPIRED="OPPORTUNITY_EXPIRED"

@dataclass(frozen=True)
class ConfluenceDecision:
    status: ConfluenceStatus
    delta_seconds: float|None
    first_event: str|None
    entry_trigger_ts: datetime|None
    expiry: datetime|None

def decide_confluence(e_ts:datetime|None,b_ts:datetime|None,now:datetime)->ConfluenceDecision:
    if e_ts is None and b_ts is None:
        return ConfluenceDecision(ConfluenceStatus.WAITING,None,None,None,None)
    if e_ts is None or b_ts is None:
        first_ts=e_ts if e_ts is not None else b_ts
        first="E" if e_ts is not None else "B"
        expiry=first_ts+timedelta(seconds=WINDOW_SECONDS)
        if now>expiry:
            return ConfluenceDecision(ConfluenceStatus.EXPIRED,None,first,None,expiry)
        return ConfluenceDecision(ConfluenceStatus.WAITING,None,first,None,expiry)
    delta=abs((e_ts-b_ts).total_seconds())
    first="SIMULTANEOUS" if e_ts==b_ts else ("E" if e_ts<b_ts else "B")
    trigger=max(e_ts,b_ts)
    expiry=min(e_ts,b_ts)+timedelta(seconds=WINDOW_SECONDS)
    if delta<=WINDOW_SECONDS:
        return ConfluenceDecision(ConfluenceStatus.VALID,delta,first,trigger,expiry)
    return ConfluenceDecision(ConfluenceStatus.EXPIRED,delta,first,None,expiry)
