"""Fail-closed per-connection SIP trust gate.

The caller must have already reconciled canonical E/B, active trades and halt
status and completed a same-event-loop CAPTURING -> DRAINING -> DIRECT handoff.
A REST fetch or subscription ACK alone never establishes continuity.
"""
class ContinuityUnproven(RuntimeError):
    pass

def require_live_trust_proof(websocket, recovery, leadership, expected_epoch):
    leadership.require_current()
    if (expected_epoch is None or not websocket.connected_event.is_set()
            or getattr(websocket, "connection_epoch", None) != expected_epoch):
        raise ContinuityUnproven("SIP_CONNECTION_EPOCH_CHANGED")
    capture = getattr(websocket, "epoch_capture", None)
    if (capture is None or capture.phase != capture.DIRECT
            or capture.buffer.epoch != expected_epoch):
        raise ContinuityUnproven("SIP_DIRECT_HANDOFF_UNPROVEN")
    if not recovery.ready_after_stream():
        raise ContinuityUnproven("POST_STREAM_RECONCILIATION_UNPROVEN")
    verify = getattr(recovery, "continuity_verified", None)
    if not callable(verify) or verify(expected_epoch) is not True:
        raise ContinuityUnproven("CONTINUITY_PROOF_MISSING")
    leadership.require_current()
    if (not websocket.connected_event.is_set()
            or websocket.connection_epoch != expected_epoch
            or capture.phase != capture.DIRECT
            or capture.buffer.epoch != expected_epoch):
        raise ContinuityUnproven("SIP_CONNECTION_EPOCH_CHANGED")
    return True
