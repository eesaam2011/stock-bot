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
from production_sip_semantic_journal import semantic_row
from sip_semantic_digest import (
    ZERO_MULTISET, add_bar_members, canonical_sha256, normalized_bar_member,
)


SCHEMA = "OPR_LIVE_SIP_SOAK_EVIDENCE_V1"
RECONNECT_SCHEMA = "OPR_LIVE_SIP_RECONNECT_PROBE_V1"


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


class LiveSemanticLedger(PayloadFreeLedger):
    """Validate all payloads, then retain only read-only semantic summaries."""

    SCHEMA = "OPR_LIVE_SIP_SEMANTIC_LEDGER_V1"

    def __init__(self, symbols, session, window_start, window_end):
        super().__init__()
        if (not symbols or len(set(symbols)) != len(symbols)
                or not isinstance(session, str) or not session):
            raise LiveSIPSoakBlocked("SEMANTIC_LEDGER_SCOPE_INVALID")
        self.scope = set(symbols)
        self.session = session
        self.symbol_count = len(symbols)
        self.symbols_sha256 = canonical_sha256(sorted(symbols))
        self.window_start = self._time(window_start)
        self.window_end = self._time(window_end)
        if not self.window_start < self.window_end:
            raise LiveSIPSoakBlocked("SEMANTIC_LEDGER_WINDOW_INVALID")
        self.semantic_chain_sha256 = ZERO_MULTISET
        self.bar_multiset_sha256 = ZERO_MULTISET
        self.bar_window_count = 0
        self.epoch = None

    @staticmethod
    def _time(value):
        if not isinstance(value, str):
            raise LiveSIPSoakBlocked("SEMANTIC_LEDGER_TIME_INVALID")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LiveSIPSoakBlocked("SEMANTIC_LEDGER_TIME_INVALID") from exc
        if parsed.tzinfo is None:
            raise LiveSIPSoakBlocked("SEMANTIC_LEDGER_TIME_INVALID")
        return parsed.astimezone(timezone.utc)

    def reconcile(self, items, context):
        rows = [semantic_row(item, self.scope) for item in items]
        epoch = context.get("epoch")
        if type(epoch) is not int or epoch < 1:
            raise LiveSIPSoakBlocked("SEMANTIC_LEDGER_EPOCH_INVALID")
        if self.epoch is not None and epoch != self.epoch:
            raise LiveSIPSoakBlocked("SEMANTIC_LEDGER_EPOCH_CHANGED")
        members = []
        for row, item in zip(rows, items):
            if row["kind"] != "BAR":
                continue
            event = self._time(row["event_ts"])
            if self.window_start <= event < self.window_end:
                members.append(normalized_bar_member(
                    row["symbol"], row["event_ts"], item.payload))
        proof = super().reconcile(items, context)
        self.epoch = epoch
        self.semantic_chain_sha256 = canonical_sha256({
            "previous_sha256": self.semantic_chain_sha256,
            "epoch": epoch,
            "first_sequence": items[0].sequence,
            "last_sequence": items[-1].sequence,
            "rows": rows,
        })
        self.bar_multiset_sha256 = add_bar_members(
            self.bar_multiset_sha256, members)
        self.bar_window_count += len(members)
        return {**proof, "sip_semantics_validated": True,
                "sip_semantics_reconciled": False}

    def semantic_snapshot(self):
        body = {
            "schema": self.SCHEMA,
            "session": self.session,
            "epoch": self.epoch,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "item_count": sum(self.counts.values()),
            "bar_count": self.counts["BAR"],
            "trade_count": self.counts["TRADE"],
            "status_count": self.counts["STATUS"],
            "bar_window_count": self.bar_window_count,
            "bar_window_start_utc": self.window_start.isoformat(),
            "bar_window_end_utc": self.window_end.isoformat(),
            "symbol_count": self.symbol_count,
            "symbols_sha256": self.symbols_sha256,
            "semantic_chain_sha256": self.semantic_chain_sha256,
            "bar_multiset_sha256": self.bar_multiset_sha256,
            "payloads_retained": 0,
            "entry_processing_enabled": False,
            "active_trade_scope_count": 0,
            "halted_trade_scope_count": 0,
            "pipeline_writes": 0,
            "trade_transition_commits": 0,
            "status_transition_commits": 0,
            "sip_semantics_validated": True,
            "sip_semantics_reconciled": False,
            "full_session_coverage_proven": False,
            "direct_handoff_authorized": False,
            "retroactive_entries_allowed": False,
        }
        body["snapshot_sha256"] = canonical_sha256(body)
        return body

def _require_live_gate(env):
    if env.get("OPR_LIVE_SIP_SOAK") != "I_UNDERSTAND_READ_ONLY_SIP":
        raise LiveSIPSoakBlocked("LIVE_SIP_SOAK_EXPLICIT_GATE_REQUIRED")
    key = env.get("APCA_API_KEY_ID") or env.get("ALPACA_API_KEY")
    secret = env.get("APCA_API_SECRET_KEY") or env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise LiveSIPSoakBlocked("ALPACA_CREDENTIALS_MISSING")
    return key, secret


def build_evidence(*, started_at, ended_at, duration_requested, symbols_count,
                   runtime, drain, ledger, terminal_error,
                   subscription_ack_at=None, expected_session_start=None,
                   expected_session_end=None):
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
    session_window = None
    if expected_session_start is not None or expected_session_end is not None:
        def parsed(value):
            if not isinstance(value, str):
                raise LiveSIPSoakBlocked("SESSION_WINDOW_INVALID")
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise LiveSIPSoakBlocked("SESSION_WINDOW_INVALID")
            return dt.astimezone(timezone.utc)
        start = parsed(expected_session_start)
        end = parsed(expected_session_end)
        ack = parsed(subscription_ack_at) if subscription_ack_at else None
        stopped = parsed(ended_at)
        if start >= end:
            raise LiveSIPSoakBlocked("SESSION_WINDOW_INVALID")
        session_window = {
            "expected_start_utc": start.isoformat(),
            "expected_end_utc": end.isoformat(),
            "subscription_ack_at_utc": ack.isoformat() if ack else None,
            "ack_before_or_at_session_start": bool(ack and ack <= start),
            "observed_through_session_end": stopped >= end,
        }
    body = {
        "schema": SCHEMA,
        "started_at_utc": started_at,
        "ended_at_utc": ended_at,
        "duration_requested_sec": duration_requested,
        "real_alpaca_sip_endpoint": SIP_STREAM_URL,
        "real_connection_attempted": True,
        "symbols_requested": symbols_count,
        "session_window": session_window,
        "terminal_error_type": (
            type(terminal_error).__name__ if terminal_error is not None else None),
        "terminal": terminal,
        "metrics_drain": drain_snapshot,
        "payload_free_ledger": ledger_snapshot,
        "semantic_ledger": (ledger.semantic_snapshot()
                            if hasattr(ledger, "semantic_snapshot") else None),
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
            "semantic_validation_only": hasattr(ledger, "semantic_snapshot"),
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
                        env=os.environ, connector=None,
                        expected_session_start=None,
                        expected_session_end=None):
    # 8 hours permits a 10-minute pre-open connection, the full 6.5-hour
    # regular session, and a bounded post-close tail.
    if not isinstance(duration_sec, int) or not 60 <= duration_sec <= 28800:
        raise LiveSIPSoakBlocked("DURATION_MUST_BE_60_TO_28800_SECONDS")
    if (expected_session_start is None) != (expected_session_end is None):
        raise LiveSIPSoakBlocked("SESSION_WINDOW_BOTH_ENDPOINTS_REQUIRED")
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
    ledger = (LiveSemanticLedger(
        symbols, datetime.fromisoformat(expected_session_start.replace(
            "Z", "+00:00")).date().isoformat(), expected_session_start,
        expected_session_end) if expected_session_start is not None
        else PayloadFreeLedger())
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
    subscription_ack_at = None
    terminal_error = None
    task = asyncio.create_task(runtime.run_once(
        SIP_STREAM_URL, key, secret, symbols))
    connect_wait = asyncio.create_task(runtime.connected_event.wait())
    duration_wait = None
    try:
        done, _ = await asyncio.wait(
            {task, connect_wait}, timeout=45,
            return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()
            raise LiveSIPSoakBlocked("SIP_TASK_ENDED_BEFORE_ACK")
        if connect_wait not in done:
            raise LiveSIPSoakBlocked("SIP_SUBSCRIPTION_ACK_TIMEOUT")
        subscription_ack_at = datetime.now(timezone.utc).isoformat()
        duration_wait = asyncio.create_task(asyncio.sleep(duration_sec))
        done, _ = await asyncio.wait(
            {task, duration_wait}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            # An early EOF/error is terminal evidence. Do not sleep until the
            # nominal session end and accidentally imply continuous coverage.
            task.result()
    except BaseException as exc:
        terminal_error = exc
    finally:
        for waiter in (connect_wait, duration_wait):
            if waiter is not None and not waiter.done():
                waiter.cancel()
        await asyncio.gather(
            *(w for w in (connect_wait, duration_wait) if w is not None),
            return_exceptions=True)
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
        terminal_error=terminal_error,
        subscription_ack_at=subscription_ack_at,
        expected_session_start=expected_session_start,
        expected_session_end=expected_session_end)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    return evidence


async def run_live_reconnect_probe(*, duration_sec, max_symbols, output_path,
                                   env=os.environ, connector=None,
                                   symbols_override=None):
    """Read-only reconnection diagnostic; each ACK starts a fresh capture epoch.

    A disconnected epoch can never be merged into the next one without a
    separate historical gap proof. Therefore this probe never asserts session
    coverage, continuity, DIRECT, or canonical recovery.
    """
    if type(duration_sec) is not int or not 60 <= duration_sec <= 28800:
        raise LiveSIPSoakBlocked("DURATION_MUST_BE_60_TO_28800_SECONDS")
    if type(max_symbols) is not int or not 1 <= max_symbols <= 12000:
        raise LiveSIPSoakBlocked("MAX_SYMBOLS_MUST_BE_1_TO_12000")
    key, secret = _require_live_gate(env)
    if connector is None:
        import websockets
        connector = websockets.connect
    if symbols_override is None:
        rest = AlpacaREST(AlpacaCredentials(key, secret))
        symbols = (await asyncio.to_thread(build_operational_universe, rest))[:max_symbols]
    else:
        symbols = list(symbols_override)[:max_symbols]
    if not symbols or len(set(symbols)) != len(symbols):
        raise LiveSIPSoakBlocked("OPERATIONAL_UNIVERSE_INVALID")
    capture = BoundedEpochCapture(max_messages=4096, max_bytes=8 * 1024 * 1024)
    epochs = []
    current = None
    drain = None
    runtime = None
    stopped_for_safety = False

    def on_ack(epoch):
        nonlocal current, drain
        if len(epochs) >= 128:
            runtime.stop()
            return
        ledger = PayloadFreeLedger()
        drain = BoundedSIPDrainCoordinator(
            capture, _MetricsLeadership(), ledger.reconcile, batch_size=256)
        current = {"epoch": epoch, "subscription_ack_at": datetime.now(timezone.utc).isoformat(),
                   "ledger": ledger, "drain": drain}
        epochs.append(current)

    async def on_message(_message):
        if capture.phase == capture.CAPTURING:
            capture.begin_drain(runtime.connection_epoch)
        drain.drain_available(runtime.connection_epoch, max_batches=16)

    async def on_disconnect():
        nonlocal current, stopped_for_safety
        if current is None:
            return
        terminal = runtime.last_epoch_diagnostic
        current["terminal"] = terminal
        current["metrics_acked"] = current["ledger"].last_sequence or 0
        current["ledger"] = current["ledger"].snapshot()
        current.pop("drain")
        current = None
        if terminal["failure_class"] in {"CAPTURE_OVERFLOW", "DISPATCH_QUEUE_OVERFLOW"}:
            stopped_for_safety = True
            runtime.stop()

    runtime = WebSocketRuntime(
        connector, AlpacaSIPProtocol, on_message, on_disconnect,
        epoch_capture=capture, dispatch_queue_max=1024,
        on_subscription_ack=on_ack)
    started = datetime.now(timezone.utc).isoformat()
    worker = asyncio.create_task(runtime.reconnect_loop(
        SIP_STREAM_URL, key, secret, symbols))
    timer = asyncio.create_task(asyncio.sleep(duration_sec))
    worker_error = None
    try:
        done, _ = await asyncio.wait({worker, timer}, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            worker.result()
    except Exception as exc:
        worker_error = f"{type(exc).__name__}:{exc}"
    finally:
        runtime.stop()
        timer.cancel()
        worker.cancel()
        await asyncio.gather(timer, worker, return_exceptions=True)
    exact = bool(epochs) and all(
        row.get("terminal", {}).get("subscription_ack_verified")
        and row["terminal"]["received"] == row["terminal"]["handled"]
        == row["metrics_acked"] == row["ledger"]["last_sequence"]
        and row["ledger"]["first_sequence"] == (1 if row["metrics_acked"] else None)
        and row["terminal"]["dispatch_queue"]["overflows"] == 0
        and row["terminal"]["capture_before_teardown"]["invalid_reason"] is None
        for row in epochs)
    body = {
        "schema": RECONNECT_SCHEMA, "started_at": started,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "duration_requested_sec": duration_sec, "symbols_count": len(symbols),
        "subscription_ack_epochs": len(epochs), "reconnect_ack_observed": len(epochs) > 1,
        "each_recorded_epoch_exact": exact,
        "stopped_for_safety": stopped_for_safety,
        "worker_error": worker_error, "epochs": epochs,
        "full_session_coverage_proven": False, "continuity_proven": False,
        "direct_handoff_authorized": False, "retroactive_entries_allowed": False,
        "production_leadership_proven": False,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["evidence_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-sec", type=int, default=1800)
    parser.add_argument("--max-symbols", type=int, default=12000)
    parser.add_argument("--output", default="LIVE_SIP_SOAK_EVIDENCE.json")
    parser.add_argument("--session-start-utc")
    parser.add_argument("--session-end-utc")
    parser.add_argument("--reconnect-probe", action="store_true")
    args = parser.parse_args()
    if args.reconnect_probe:
        if args.session_start_utc or args.session_end_utc:
            parser.error("reconnect probe cannot attest full-session coverage")
        evidence = asyncio.run(run_live_reconnect_probe(
            duration_sec=args.duration_sec, max_symbols=args.max_symbols,
            output_path=args.output))
        print(json.dumps({"schema": evidence["schema"],
                          "subscription_ack_epochs": evidence["subscription_ack_epochs"],
                          "reconnect_ack_observed": evidence["reconnect_ack_observed"],
                          "each_recorded_epoch_exact": evidence["each_recorded_epoch_exact"],
                          "evidence_sha256": evidence["evidence_sha256"]}, sort_keys=True))
        return
    evidence = asyncio.run(run_live_soak(
        duration_sec=args.duration_sec,
        max_symbols=args.max_symbols,
        output_path=args.output,
        expected_session_start=args.session_start_utc,
        expected_session_end=args.session_end_utc))
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
