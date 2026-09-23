"""Read-only Alpaca SIP soak used to collect the final external evidence.

This runner deliberately has no Redis, pipeline, alert, entry, or DIRECT
handoff dependency.  It measures the real websocket ACK epoch and continuously
drains captured messages into a payload-free metrics ledger.  The resulting
JSON is evidence about transport behaviour only; it cannot authorize Shadow.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from alpaca_production_market import (
    AlpacaCredentials, AlpacaREST, AlpacaSIPProtocol, SIP_STREAM_URL,
)
from production_universe import build_operational_universe
from sip_drain_coordinator import BoundedSIPDrainCoordinator
from sip_epoch_capture import BoundedEpochCapture
from websocket_runtime import WebSocketRuntime


SCHEMA = "OPR_LIVE_SIP_SOAK_EVIDENCE_V1"


class LiveSIPSoakBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class _MetricsToken:
    worker_instance_id: str = "live-soak-metrics-only"
    leader_generation: int = 1


class _MetricsLeadership:
    """Stable process-local token; explicitly not a production lease proof."""

    def require_current(self):
        return _MetricsToken()


class PayloadFreeLedger:
    """Commit exact drain prefixes while retaining no events or symbols."""

    def __init__(self):
        self.counts = {"BAR": 0, "TRADE": 0, "STATUS": 0}
        self.batches = 0
        self.first_sequence = None
        self.last_sequence = None

    def reconcile(self, items, context):
        for item in items:
            self.counts[item.kind] += 1
        self.batches += 1
        if self.first_sequence is None:
            self.first_sequence = items[0].sequence
        self.last_sequence = items[-1].sequence
        return {
            **context,
            "committed": True,
            "direct_handoff_authorized": False,
            "full_session_coverage_proven": False,
            "retroactive_entries_allowed": False,
        }

    def snapshot(self):
        return {
            "retained_payloads": 0,
            "retained_symbols": 0,
            "counts": dict(self.counts),
            "batches": self.batches,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
        }


def _require_live_gate(env):
    if env.get("OPR_LIVE_SIP_SOAK") != "I_UNDERSTAND_READ_ONLY_SIP":
        raise LiveSIPSoakBlocked("LIVE_SIP_SOAK_EXPLICIT_GATE_REQUIRED")
    key = env.get("APCA_API_KEY_ID") or env.get("ALPACA_API_KEY")
    secret = env.get("APCA_API_SECRET_KEY") or env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise LiveSIPSoakBlocked("ALPACA_CREDENTIALS_MISSING")
    return key, secret


def build_evidence(*, started_at, ended_at, duration_requested, symbols_count,
                   runtime, drain, ledger, terminal_error):
    terminal = runtime.last_epoch_diagnostic
    if terminal is None:
        terminal = {
            "schema": "OPR_SIP_EPOCH_TERMINAL_V1",
            "epoch": None,
            "subscription_ack_verified": False,
            "failure_class": "NO_TERMINAL_DIAGNOSTIC",
            "received": 0,
            "handled": 0,
            "received_not_confirmed_handled": 0,
            "continuity_proven": False,
            "direct_handoff_authorized": False,
        }
    drain_snapshot = drain.snapshot()
    ledger_snapshot = ledger.snapshot()
    received = terminal.get("received", 0)
    handled = terminal.get("handled", 0)
    acked = drain_snapshot["acked_messages"]
    body = {
        "schema": SCHEMA,
        "started_at_utc": started_at,
        "ended_at_utc": ended_at,
        "duration_requested_sec": duration_requested,
        "real_alpaca_sip_endpoint": SIP_STREAM_URL,
        "real_connection_attempted": True,
        "symbols_requested": symbols_count,
        "terminal_error_type": (
            type(terminal_error).__name__ if terminal_error is not None else None),
        "terminal": terminal,
        "metrics_drain": drain_snapshot,
        "payload_free_ledger": ledger_snapshot,
        "observations": {
            "subscription_ack_verified": bool(
                terminal.get("subscription_ack_verified")),
            "received_equals_handled": received == handled,
            "handled_equals_metrics_acked": handled == acked,
            "dispatch_overflow_observed": (
                terminal.get("failure_class") == "DISPATCH_QUEUE_OVERFLOW"),
            "capture_overflow_observed": (
                terminal.get("failure_class") == "CAPTURE_OVERFLOW"),
            "alpaca_error_frame_407_observed": (
                terminal.get("failure_class") == "SIP_ERROR_FRAME_407"),
        },
        "claims": {
            "transport_measurement_only": True,
            "production_generation_fence_proven": False,
            "full_session_coverage_proven": False,
            "sip_continuity_proven": False,
            "direct_handoff_authorized": False,
            "shadow_deploy_authorized": False,
            "retroactive_entries_allowed": False,
        },
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return {**body, "evidence_sha256": hashlib.sha256(
        canonical.encode("utf-8")).hexdigest()}


async def run_live_soak(*, duration_sec, max_symbols, output_path,
                        env=os.environ, connector=None):
    if not isinstance(duration_sec, int) or not 60 <= duration_sec <= 21600:
        raise LiveSIPSoakBlocked("DURATION_MUST_BE_60_TO_21600_SECONDS")
    if not isinstance(max_symbols, int) or not 1 <= max_symbols <= 12000:
        raise LiveSIPSoakBlocked("MAX_SYMBOLS_MUST_BE_1_TO_12000")
    key, secret = _require_live_gate(env)
    if connector is None:
        import websockets
        connector = websockets.connect

    rest = AlpacaREST(AlpacaCredentials(key, secret))
    symbols = await asyncio.to_thread(build_operational_universe, rest)
    symbols = symbols[:max_symbols]
    if not symbols:
        raise LiveSIPSoakBlocked("OPERATIONAL_UNIVERSE_EMPTY")

    capture = BoundedEpochCapture(max_messages=4096, max_bytes=8 * 1024 * 1024)
    ledger = PayloadFreeLedger()
    drain = BoundedSIPDrainCoordinator(
        capture, _MetricsLeadership(), ledger.reconcile, batch_size=256)
    runtime = None

    async def on_message(_message):
        if capture.phase == capture.CAPTURING:
            capture.begin_drain(runtime.connection_epoch)
        drain.drain_available(runtime.connection_epoch, max_batches=16)

    async def on_disconnect():
        return None

    runtime = WebSocketRuntime(
        connector, AlpacaSIPProtocol, on_message, on_disconnect,
        epoch_capture=capture, dispatch_queue_max=1024)
    started = datetime.now(timezone.utc).isoformat()
    terminal_error = None
    task = asyncio.create_task(runtime.run_once(
        SIP_STREAM_URL, key, secret, symbols))
    try:
        await asyncio.wait_for(runtime.connected_event.wait(), timeout=45)
        await asyncio.sleep(duration_sec)
    except BaseException as exc:
        terminal_error = exc
    finally:
        runtime.stop()
        if not task.done():
            task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        if terminal_error is None and result and isinstance(result[0], BaseException):
            terminal_error = result[0]

    evidence = build_evidence(
        started_at=started,
        ended_at=datetime.now(timezone.utc).isoformat(),
        duration_requested=duration_sec,
        symbols_count=len(symbols),
        runtime=runtime,
        drain=drain,
        ledger=ledger,
        terminal_error=terminal_error)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-sec", type=int, default=1800)
    parser.add_argument("--max-symbols", type=int, default=12000)
    parser.add_argument("--output", default="LIVE_SIP_SOAK_EVIDENCE.json")
    args = parser.parse_args()
    evidence = asyncio.run(run_live_soak(
        duration_sec=args.duration_sec,
        max_symbols=args.max_symbols,
        output_path=args.output))
    print(json.dumps({
        "schema": evidence["schema"],
        "subscription_ack_verified": evidence["observations"][
            "subscription_ack_verified"],
        "received": evidence["terminal"].get("received"),
        "handled": evidence["terminal"].get("handled"),
        "metrics_acked": evidence["metrics_drain"]["acked_messages"],
        "failure_class": evidence["terminal"].get("failure_class"),
        "evidence_sha256": evidence["evidence_sha256"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
