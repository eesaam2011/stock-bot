from dataclasses import dataclass
from datetime import datetime,timedelta
from enum import Enum
from constants import MAX_ENTRY_TRADE_AGE_SECONDS,ENTRY_PRICE_WAIT_SECONDS
class EntryStatus(str,Enum):
    WAITING="ENTRY_DATA_WAIT";PRICE_READY="ENTRY_PRICE_READY";EXPIRED="ENTRY_EXPIRED_STALE_MARKET_DATA";HALT_FINAL="ENTRY_BLOCKED_HALT_FINAL"
@dataclass(frozen=True)
class SIPTrade:
    price:float;trade_ts:datetime;received_at:datetime;sequence:int=0;eligible:bool=True
@dataclass(frozen=True)
class EntryDecision:
    status:EntryStatus;entry_alert_price:float|None=None;trade_ts:datetime|None=None;trade_received_at:datetime|None=None;trade_sequence:int|None=None;deadline:datetime|None=None
def evaluate_entry_price(trigger,now,trades,halted):
    deadline=trigger+timedelta(seconds=ENTRY_PRICE_WAIT_SECONDS)
    if halted:return EntryDecision(EntryStatus.HALT_FINAL,deadline=deadline)
    xs=[]
    for t in trades:
        if not t.eligible or t.price<=0 or t.received_at<trigger or t.received_at>now:continue
        age=(t.received_at-t.trade_ts).total_seconds()
        if age<0 or age>MAX_ENTRY_TRADE_AGE_SECONDS or t.received_at>deadline:continue
        xs.append(t)
    if xs:
        t=max(xs,key=lambda x:(x.received_at,x.sequence))
        return EntryDecision(EntryStatus.PRICE_READY,float(t.price),t.trade_ts,t.received_at,t.sequence,deadline)
    return EntryDecision(EntryStatus.EXPIRED if now>=deadline else EntryStatus.WAITING,deadline=deadline)
