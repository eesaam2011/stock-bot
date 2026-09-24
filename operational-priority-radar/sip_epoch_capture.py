"""Bounded SIP epoch capture; transport-only, never a continuity proof.

Capture starts only after a complete subscription ACK. A disconnect, epoch
mismatch, oversized frame, or queue overflow invalidates the entire epoch.
No old-epoch message can enter a new capture. Handoff is explicit and remains
blocked until an external chronological REST/SIP coordinator supplies proof.
"""
import json,threading
from copy import deepcopy
from functools import wraps
from collections import deque
from dataclasses import dataclass
from datetime import datetime,timezone

def _locked(fn):
    @wraps(fn)
    def wrapper(self,*args,**kwargs):
        with self._lock:return fn(self,*args,**kwargs)
    return wrapper

class EpochCaptureError(RuntimeError):pass
class EpochCaptureOverflow(EpochCaptureError):pass

@dataclass(frozen=True)
class CapturedSIP:
    epoch:int
    sequence:int
    kind:str
    event_ts:str
    payload:dict
    bytes:int
    received_at:str=""  # actual capture time; event_ts is market event time

class BoundedEpochCapture:
    CAPTURING="CAPTURING"
    DRAINING="DRAINING"
    DIRECT="DIRECT"
    INVALID="INVALID"
    TYPES={"b":"BAR","t":"TRADE","s":"STATUS"}
    def __init__(self,max_messages=4096,max_bytes=8*1024*1024):
        if not (1<=max_messages<=100000 and 1024<=max_bytes<=256*1024*1024):
            raise ValueError("invalid capture limits")
        self._lock=threading.RLock()
        self.max_messages=max_messages;self.max_bytes=max_bytes
        self.phase=self.INVALID;self.epoch=None
        self._items=deque();self._bytes=0;self._seq=0;self._acked_upto=0
        self._invalid_reason="NOT_STARTED"
        self._revision_diagnostic=None
        # Trust gate uses capture.buffer.epoch to tie DIRECT to the ACK epoch.
        self.buffer=self
    @_locked
    def start(self,epoch):
        if not isinstance(epoch,int) or epoch<1:
            raise EpochCaptureError("INVALID_EPOCH")
        self.invalidate("NEW_EPOCH")
        self.epoch=epoch;self.phase=self.CAPTURING
        # Sequence numbers and ACK watermarks belong to one ACK epoch only.
        self._seq=0;self._acked_upto=0
        self._invalid_reason=None
    @_locked
    def invalidate(self,reason="DISCONNECT"):
        self._revision_diagnostic=None
        self.phase=self.INVALID;self.epoch=None
        self._items.clear();self._bytes=0;self._acked_upto=0
        self._invalid_reason=reason
    @staticmethod
    def _timestamp(msg):
        raw=msg.get("t")
        if not isinstance(raw,str):
            raise EpochCaptureError("SIP_EVENT_TIME_MISSING")
        try:
            dt=datetime.fromisoformat(raw.replace("Z","+00:00"))
            if dt.tzinfo is None:raise ValueError("naive")
            return dt.astimezone(timezone.utc).isoformat()
        except (ValueError,TypeError) as exc:
            raise EpochCaptureError("SIP_EVENT_TIME_INVALID") from exc
    @_locked
    def ingest(self,epoch,msg,*,received_at=None):
        if self.phase not in {self.CAPTURING,self.DRAINING,self.DIRECT} or self.epoch!=epoch:
            raise EpochCaptureError("SIP_CAPTURE_EPOCH_INVALID")
        # Alpaca delivers corrections and cancels automatically with trades.
        # Until trade-ID reconciliation is implemented, they invalidate this
        # epoch; treating them as ordinary control frames loses market state.
        if msg.get("T") in {"c","x"}:
            from sip_revision_diagnostic import revision_diagnostic
            diagnostic=revision_diagnostic(msg,epoch=epoch,
                last_sequence=self._seq,acked_upto=self._acked_upto)
            self.invalidate("SIP_TRADE_REVISION_UNRECONCILED")
            self._revision_diagnostic=diagnostic
            raise EpochCaptureError("SIP_TRADE_REVISION_UNRECONCILED")
        kind=self.TYPES.get(msg.get("T"))
        if kind is None:return None
        if self.phase==self.DIRECT:return None  # caller delivers directly after verified handoff
        ts=self._timestamp(msg)
        captured=received_at or datetime.now(timezone.utc)
        if not isinstance(captured,datetime) or captured.tzinfo is None:
            raise EpochCaptureError("SIP_RECEIVED_AT_INVALID")
        captured=captured.astimezone(timezone.utc).isoformat()
        raw=json.dumps(msg,separators=(",",":"),ensure_ascii=False)
        size=len(raw.encode("utf-8"))
        if len(self._items)>=self.max_messages or self._bytes+size>self.max_bytes:
            self.invalidate("CAPTURE_OVERFLOW")
            raise EpochCaptureOverflow("SIP_CAPTURE_OVERFLOW_FAIL_CLOSED")
        self._seq+=1
        item=CapturedSIP(epoch,self._seq,kind,ts,deepcopy(msg),size,captured)
        self._items.append(item);self._bytes+=size
        return item
    @_locked
    def begin_drain(self,epoch):
        if self.epoch!=epoch or self.phase!=self.CAPTURING:
            raise EpochCaptureError("CAPTURE_DRAIN_EPOCH_INVALID")
        self.phase=self.DRAINING
    @_locked
    def peek_batch(self,epoch,max_items=None):
        if self.epoch!=epoch or self.phase!=self.DRAINING:
            raise EpochCaptureError("CAPTURE_NOT_DRAINING")
        if max_items is None:max_items=min(512,self.max_messages)
        if not isinstance(max_items,int) or max_items<1 or max_items>self.max_messages:
            raise ValueError("invalid batch")
        # Non-destructive until the coordinator has committed chronological
        # replay; a failed replay must not silently discard captured events.
        from itertools import islice
        return list(islice(self._items,max_items))
    @_locked
    def ack_batch(self,epoch,upto_sequence):
        if self.epoch!=epoch or self.phase!=self.DRAINING or not self._items:
            raise EpochCaptureError("CAPTURE_ACK_INVALID")
        if not isinstance(upto_sequence,int) or upto_sequence<self._items[0].sequence:
            raise EpochCaptureError("CAPTURE_ACK_NONCONTIGUOUS")
        count=0
        for item in self._items:
            if item.sequence>upto_sequence:break
            count+=1
        if count==0 or self._items[count-1].sequence!=upto_sequence:
            raise EpochCaptureError("CAPTURE_ACK_NONCONTIGUOUS")
        for _ in range(count):
            item=self._items.popleft();self._bytes-=item.bytes
        self._acked_upto=upto_sequence
        return count
    @_locked
    def finish_direct(self,epoch,*,reconciliation_proven=False):
        if self.epoch!=epoch or self.phase!=self.DRAINING or self._items:
            raise EpochCaptureError("CAPTURE_DIRECT_HANDOFF_INVALID")
        if reconciliation_proven is not True:
            raise EpochCaptureError("EXTERNAL_RECONCILIATION_PROOF_REQUIRED")
        self.phase=self.DIRECT
    @_locked
    def snapshot_prefix(self,epoch,max_items=None):
        """Atomic deep-copy of a DRAINING prefix; never ACKs captured events.

        A copy cannot be changed by a later append, disconnect or accidental
        mutation of the live queue. The caller must revalidate the epoch
        after auditing; even that check is NOT a continuity proof.
        """
        if self.phase!=self.DRAINING or self.epoch!=epoch:
            raise EpochCaptureError("CAPTURE_NOT_DRAINING")
        if max_items is None:max_items=min(512,self.max_messages)
        if not isinstance(max_items,int) or not 1<=max_items<=self.max_messages:
            raise ValueError("invalid snapshot limit")
        from itertools import islice
        copied=tuple(deepcopy(item) for item in islice(self._items,max_items))
        return copied,{"epoch":self.epoch,"phase":self.phase,
                       "first_sequence":copied[0].sequence if copied else None,
                       "last_sequence":copied[-1].sequence if copied else None,
                       "captured_prefix_count":len(copied),
                       "queue_size_at_snapshot":len(self._items),
                       "last_sequence_at_snapshot":self._seq,
                       "acked_upto_at_snapshot":self._acked_upto}

    @_locked
    def prefix_still_valid(self,epoch,first_sequence,last_sequence):
        """Detect invalidation or ACK of the audited prefix (not continuity)."""
        if self.phase!=self.DRAINING or self.epoch!=epoch:
            return False
        if first_sequence is None:
            return last_sequence is None
        if not self._items or self._items[0].sequence!=first_sequence:
            return False
        return any(x.sequence==last_sequence for x in self._items)

    @_locked
    def snapshot(self):
        result={"phase":self.phase,"epoch":self.epoch,"buffered":len(self._items),
                "bytes":self._bytes,"last_sequence":self._seq,
                "acked_upto":self._acked_upto,
                "invalid_reason":self._invalid_reason}
        if self._revision_diagnostic is not None:
            result['revision_diagnostic']=deepcopy(self._revision_diagnostic)
        return result
