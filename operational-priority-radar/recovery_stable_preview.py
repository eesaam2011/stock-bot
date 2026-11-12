"""Step3K: bounded read-only retries for a growing DRAINING SIP capture.

A REST request has a fixed cutoff. SIP may arrive while REST is fetched and
while frozen E/B engines run. Retry only append races, never a disconnect,
ACK, overflow, conflicting bar or stale epoch. No writes, ACK, or DIRECT.
"""
from recovery_chronology import _utc
from recovery_sip_overlap import SIPOverlapUnsafe
from recovery_session_preview import preview_session_overlap

class StablePreviewUnsafe(SIPOverlapUnsafe):pass

def stable_session_preview(native_events,capture,*,session,epoch,symbols,
                           session_start,requested_start,rest_cutoff,
                           now_fn,max_attempts=3,max_events=100000,
                           preview_fn=None,cancel_check=None):
    if (not isinstance(max_attempts,int) or isinstance(max_attempts,bool)
        or not 1<=max_attempts<=3):
        raise StablePreviewUnsafe("STABLE_PREVIEW_INVALID_ATTEMPTS")
    if preview_fn is None:preview_fn=preview_session_overlap
    cutoff=_utc(rest_cutoff)
    previous_as_of=cutoff
    retries=0
    for attempt in range(1,max_attempts+1):
        if cancel_check:cancel_check()
        before=capture.snapshot()
        if (before["phase"]!=capture.DRAINING or before["epoch"]!=epoch
            or before["acked_upto"]!=0
            or before["last_sequence"]!=before["buffered"]):
            raise StablePreviewUnsafe("STABLE_PREVIEW_INVALID_EPOCH_OR_ACK")
        as_of=_utc(now_fn())
        if as_of<cutoff or as_of<previous_as_of:
            raise StablePreviewUnsafe("STABLE_PREVIEW_CLOCK_REGRESSION")
        previous_as_of=as_of
        try:
            signals,audit=preview_fn(
                native_events,capture,session=session,epoch=epoch,
                symbols=symbols,session_start=session_start,
                session_end=as_of,requested_start=requested_start,
                as_of=as_of,max_events=max_events)
        except SIPOverlapUnsafe as exc:
            # Only a growing, still-unacknowledged capture is retriable.
            # All other errors (407, conflict, bad time, overflow, truncation)
            # are fatal; never retry with a new epoch or after any ACK.
            if str(exc)!="SESSION_PREVIEW_CAPTURE_CHANGED":
                raise
            after=capture.snapshot()
            if (after["phase"]!=capture.DRAINING or after["epoch"]!=epoch
                or after["acked_upto"]!=0
                or after["buffered"]<=before["buffered"]
                or after["last_sequence"]!=after["buffered"]):
                raise StablePreviewUnsafe("STABLE_PREVIEW_NON_APPEND_MUTATION") from exc
            retries+=1
            continue
        prefix=audit["overlap"]["audited_prefix"]
        after=capture.snapshot()
        if (after["phase"]!=capture.DRAINING or after["epoch"]!=epoch
            or after["acked_upto"]!=0):
            raise StablePreviewUnsafe("STABLE_PREVIEW_EPOCH_CHANGED")
        if (after["buffered"]!=prefix["queue_size_at_snapshot"]
            or after["last_sequence"]!=prefix["last_sequence_at_snapshot"]
            or after["acked_upto"]!=prefix["acked_upto_at_snapshot"]):
            if (after["buffered"]>prefix["queue_size_at_snapshot"]
                and after["last_sequence"]==after["buffered"]):
                retries+=1
                continue
            raise StablePreviewUnsafe("STABLE_PREVIEW_NON_APPEND_MUTATION")
        if cancel_check:cancel_check()
        audit["stable_preview_attempts"]=attempt
        audit["stable_preview_append_retries"]=retries
        audit["rest_cutoff"]=cutoff.isoformat()
        audit["audit_as_of"]=as_of.isoformat()
        audit["post_rest_sip_coverage_proven"]=False
        audit["capture_acknowledged"]=False
        audit["direct_handoff_authorized"]=False
        return signals,audit
    raise StablePreviewUnsafe("STABLE_PREVIEW_RETRY_BUDGET_EXHAUSTED")
