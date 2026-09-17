from dataclasses import dataclass,replace
from enum import Enum
class TrustState(str,Enum):
 STARTING="STARTING";RECOVERING_GAP="RECOVERING_GAP";CONNECTING_STREAM="CONNECTING_STREAM";VERIFYING_CONTINUITY="VERIFYING_CONTINUITY";LIVE_TRUSTED="LIVE_TRUSTED";STREAM_UNTRUSTED="STREAM_UNTRUSTED";RECONNECTING="RECONNECTING"
@dataclass(frozen=True)
class Trust:
 state:TrustState=TrustState.STARTING
 subscriptions_ok:bool=False
 gap_recovered:bool=False
 reconciled:bool=False
 continuity_ok:bool=False
def transition(t,event):
 if event=="START_RECOVERY":return replace(t,state=TrustState.RECOVERING_GAP,gap_recovered=False,reconciled=False,continuity_ok=False)
 if event=="GAP_RECOVERED":return replace(t,state=TrustState.CONNECTING_STREAM,gap_recovered=True)
 if event=="STREAM_CONNECTED":return replace(t,state=TrustState.VERIFYING_CONTINUITY,subscriptions_ok=True)
 if event=="RECONCILED":return replace(t,reconciled=True)
 if event=="CONTINUITY_OK":
  n=replace(t,continuity_ok=True)
  return replace(n,state=TrustState.LIVE_TRUSTED) if n.subscriptions_ok and n.gap_recovered and n.reconciled else n
 if event=="DISCONNECT":return Trust(TrustState.STREAM_UNTRUSTED,False,False,False,False)
 if event=="BEGIN_RECONNECT":return replace(t,state=TrustState.RECONNECTING)
 raise ValueError(event)
def new_entries_allowed(t):return t.state==TrustState.LIVE_TRUSTED
