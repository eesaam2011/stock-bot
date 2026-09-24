"""Strict adjudication of one real SIP regular-session transport run.

Passing this contract proves a single ACK epoch was observed across the
declared time window and every received message reached the bounded drain.
It does not prove upstream market completeness, semantic E/B replay, DIRECT,
or Shadow authorization.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


SCHEMA = "OPR_FULL_SESSION_TRANSPORT_AUDIT_V1"


class FullSessionTransportUnsafe(RuntimeError):
    pass


def _utc(value):
    if not isinstance(value, str) or not value:
        raise FullSessionTransportUnsafe("FULL_SESSION_TIME_INVALID")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FullSessionTransportUnsafe("FULL_SESSION_TIME_INVALID") from exc
    if dt.tzinfo is None:
        raise FullSessionTransportUnsafe("FULL_SESSION_TIME_INVALID")
    return dt.astimezone(timezone.utc)


def _verify_evidence_digest(evidence):
    if not isinstance(evidence, dict):
        raise FullSessionTransportUnsafe("FULL_SESSION_EVIDENCE_MISSING")
    body = dict(evidence)
    expected = body.pop("evidence_sha256", None)
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if expected != actual:
        raise FullSessionTransportUnsafe("FULL_SESSION_EVIDENCE_DIGEST_MISMATCH")
    return expected


def adjudicate_full_session_transport(evidence):
    digest = _verify_evidence_digest(evidence)
    if evidence.get("schema") != "OPR_LIVE_SIP_SOAK_EVIDENCE_V1":
        raise FullSessionTransportUnsafe("FULL_SESSION_EVIDENCE_SCHEMA_INVALID")
    window = evidence.get("session_window")
    terminal = evidence.get("terminal")
    drain = evidence.get("metrics_drain")
    ledger = evidence.get("payload_free_ledger")
    observations = evidence.get("observations")
    if not all(isinstance(x, dict) for x in
               (window, terminal, drain, ledger, observations)):
        raise FullSessionTransportUnsafe("FULL_SESSION_EVIDENCE_INCOMPLETE")
    start = _utc(window.get("expected_start_utc"))
    end = _utc(window.get("expected_end_utc"))
    ack = _utc(window.get("subscription_ack_at_utc"))
    stopped = _utc(evidence.get("ended_at_utc"))
    if not ack <= start < end <= stopped:
        raise FullSessionTransportUnsafe("FULL_SESSION_WINDOW_NOT_COVERED")
    if (window.get("ack_before_or_at_session_start") is not True
            or window.get("observed_through_session_end") is not True
            or observations.get("subscription_ack_verified") is not True):
        raise FullSessionTransportUnsafe("FULL_SESSION_ACK_OR_WINDOW_UNPROVEN")
    received = terminal.get("received")
    if (type(received) is not int or received < 1
            or terminal.get("handled") != received
            or terminal.get("received_not_confirmed_handled") != 0
            or drain.get("acked_messages") != received
            or ledger.get("first_sequence") != 1
            or ledger.get("last_sequence") != received
            or terminal.get("epoch") != 1):
        raise FullSessionTransportUnsafe("FULL_SESSION_MESSAGE_CHAIN_MISMATCH")
    # Older evidence has no kind counters, and a trade cancel/correction can
    # be received and handled while bypassing the b/t/s semantic ledger.
    # Transport attestation must reject that ambiguity, not infer coverage.
    kinds = ledger.get("counts")
    if (type(terminal.get("market_data_received")) is not int
            or terminal["market_data_received"] != received
            or type(terminal.get("known_control_received")) is not int
            or terminal["known_control_received"] != 0
            or type(terminal.get("unknown_nonmarket_received")) is not int
            or terminal["unknown_nonmarket_received"] != 0
            or not isinstance(kinds, dict)
            or set(kinds) != {"BAR", "TRADE", "STATUS"}
            or any(type(kinds.get(kind)) is not int or kinds[kind] < 0
                   for kind in ("BAR", "TRADE", "STATUS"))
            or sum(kinds[kind] for kind in ("BAR", "TRADE", "STATUS")) != received):
        raise FullSessionTransportUnsafe("FULL_SESSION_MESSAGE_KIND_UNPROVEN")
    capture = terminal.get("capture_before_teardown")
    dispatch = terminal.get("dispatch_queue")
    if (not isinstance(capture, dict) or not isinstance(dispatch, dict)
            or capture.get("acked_upto") != received
            or capture.get("last_sequence") != received
            or capture.get("buffered") != 0
            or capture.get("invalid_reason") is not None
            or dispatch.get("overflows") != 0
            or observations.get("capture_overflow_observed") is not False
            or observations.get("dispatch_overflow_observed") is not False):
        raise FullSessionTransportUnsafe("FULL_SESSION_OVERFLOW_OR_TAIL")
    # 407 is a fact only when WebSocketRuntime classified an Alpaca error
    # frame. Text containing 407 in any other exception cannot set this flag.
    frame_407 = terminal.get("failure_class") == "SIP_ERROR_FRAME_407"
    if observations.get("alpaca_error_frame_407_observed") is not frame_407:
        raise FullSessionTransportUnsafe("FULL_SESSION_407_CLASSIFICATION_INVALID")
    if frame_407:
        raise FullSessionTransportUnsafe("FULL_SESSION_SIP_ERROR_FRAME_407")
    if terminal.get("failure_class") != "OTHER_OR_CANCELLED":
        raise FullSessionTransportUnsafe("FULL_SESSION_UNPLANNED_TERMINATION")
    if ledger.get("retained_payloads") != 0 or ledger.get("retained_symbols") != 0:
        raise FullSessionTransportUnsafe("FULL_SESSION_LEDGER_NOT_PAYLOAD_FREE")
    return {
        "schema": SCHEMA,
        "market_session_evidence_sha256": digest,
        "epoch": 1,
        "received": received,
        "handled": received,
        "metrics_acked": received,
        "session_start_utc": start.isoformat(),
        "session_end_utc": end.isoformat(),
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch_observed": True,
        "capture_overflow_observed": False,
        "dispatch_overflow_observed": False,
        "sip_error_frame_407_observed": False,
        "payload_retained": False,
        "transport_full_window_observed": True,
        "upstream_market_completeness_proven": False,
        "semantic_eb_replay_completed": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }
