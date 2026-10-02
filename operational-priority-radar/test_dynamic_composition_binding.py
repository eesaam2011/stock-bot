"""Offline binding evidence, NOT a live WebSocket/continuity certification.

Use the production factory and its actual isolation callback. Subscription ACKs
and input frames are synthetic; network, Redis and runtime startup are forbidden.
The active-trade test records a limitation, rather than declaring it resolved.
"""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from production_composition import compose_shadow_runtime
from sip_epoch_capture import EpochCaptureError

NOW = datetime(2026, 10, 2, 14, tzinfo=timezone.utc)


def forbidden(*args, **kwargs):
    raise AssertionError("OFFLINE_AUDIT_IO_FORBIDDEN")


class DenyRedis:
    def __getattr__(self, name):
        forbidden(name)


def revision(kind="x", symbol="AAPL", action="C"):
    frame = {"T": kind, "S": symbol, "t": NOW.isoformat(), "x": "V", "z": "C"}
    if kind == "x":
        frame.update(i=1, p=10.5, s=100, a=action)
    else:
        frame.update(oi=1, op=10.5, os=100, oc=[], ci=2, cp=11.0, cs=80, cc=[])
    return frame


class DynamicCompositionBindingTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("urllib.request.urlopen", side_effect=forbidden)
        self.network.start()
        self.addCleanup(self.network.stop)
        self.runtime = compose_shadow_runtime(
            SimpleNamespace(redis_url="unused", alpaca_key="offline", alpaca_secret="offline"),
            redis_client=DenyRedis(), websocket_connector=forbidden,
            symbols=["AAPL", "MSFT"])
        self.ws = self.runtime.websocket_runtime
        self.pipeline = self.runtime.decision_pipeline
        self.capture = self.ws.epoch_capture
        self.ws.connection_epoch = 1
        self.ws.trade_scope.start_epoch(1, ["AAPL", "MSFT"])
        self.ws.trade_scope.acknowledge(1, ["AAPL", "MSFT"])
        self.ws._epoch_ack_verified = True  # synthetic transport state only
        self.capture.start(1)

    def assert_closed(self):
        self.assertFalse(self.pipeline.decision_gate())
        self.assertNotEqual(self.capture.phase, self.capture.DIRECT)

    def check_isolation(self, frame):
        self.assertIsNone(self.capture.ingest(1, frame, received_at=NOW))
        self.assertTrue(self.pipeline.symbol_isolation.blocked("AAPL"))
        self.assertFalse(self.pipeline.symbol_isolation.blocked("MSFT"))
        self.assertEqual(self.capture.phase, self.capture.CAPTURING)
        self.assertEqual(self.capture.snapshot()["isolated_revisions"], 1)
        self.assert_closed()

    def check_epoch_refusal(self, frame):
        with self.assertRaisesRegex(EpochCaptureError, "SIP_TRADE_REVISION_UNRECONCILED"):
            self.capture.ingest(1, frame, received_at=NOW)
        self.assertEqual(self.capture.phase, self.capture.INVALID)
        self.assert_closed()

    def test_documented_cancel_actual_composition(self):
        self.check_isolation(revision())

    def test_documented_error_action_actual_composition(self):
        self.check_isolation(revision(action="E"))

    def test_documented_correction_actual_composition(self):
        self.check_isolation(revision(kind="c"))

    def test_unknown_action_invalidates_epoch(self):
        self.check_epoch_refusal(revision(action="1"))

    def test_out_of_scope_invalidates_epoch(self):
        self.check_epoch_refusal(revision(symbol="IWM"))

    def test_unacknowledged_symbol_invalidates_epoch(self):
        self.ws.trade_scope.acknowledge(1, ["MSFT"])
        self.check_epoch_refusal(revision())

    def test_closed_transport_ack_invalidates_epoch(self):
        self.ws._epoch_ack_verified = False
        self.check_epoch_refusal(revision())

    def test_old_scope_epoch_cannot_authorize_current_epoch(self):
        self.ws.connection_epoch = 2
        self.capture.start(2)
        with self.assertRaises(EpochCaptureError):
            self.capture.ingest(2, revision(), received_at=NOW)
        self.assertEqual(self.capture.phase, self.capture.INVALID)
        self.assert_closed()

    def test_expired_original_does_not_become_reconciled(self):
        original = {"T": "t", "S": "AAPL", "t": NOW.isoformat(),
                    "i": 1, "p": 10.5, "s": 100, "c": [], "x": "V", "z": "C"}
        self.capture.ingest(1, original, received_at=NOW)
        self.capture.ingest(1, revision(), received_at=NOW + timedelta(minutes=16))
        self.assertTrue(self.pipeline.symbol_isolation.blocked("AAPL"))
        self.assertEqual(self.capture.phase, self.capture.CAPTURING)
        self.assert_closed()

    def test_active_trade_monitor_disposition_remains_unproven(self):
        # Seed only in-memory sentinels, never canonical trade state.
        active = object()
        self.pipeline._active_by_symbol["AAPL"] = active
        self.pipeline.trades["AAPL"] = object()
        self.pipeline._entry_opportunities["AAPL"] = object()
        self.check_isolation(revision())
        self.assertIs(self.pipeline._active_by_symbol["AAPL"], active)
        self.assertNotIn("AAPL", self.pipeline.trades)
        self.assertNotIn("AAPL", self.pipeline._entry_opportunities)
        self.assertIn("AAPL", self.ws.trade_scope.desired())

    def test_connected_ack_alone_does_not_open_decision_gate(self):
        self.ws.connected_event.set()
        self.assertTrue(self.ws.trade_authorized("AAPL"))
        self.assert_closed()

    def test_isolated_symbol_still_reaches_active_trade_monitor(self):
        self.check_isolation(revision())
        # Verify real on_trade routing without invoking canonical commit IO.
        with patch.object(self.pipeline, "_monitor_trade") as monitor:
            self.pipeline.on_trade("AAPL", {"p": 11.0, "t": NOW.isoformat()}, NOW, True)
        monitor.assert_called_once()
        self.assertEqual(monitor.call_args.args[0], "AAPL")
        self.assertNotIn("AAPL", self.pipeline.trades)
        self.assert_closed()

    def test_default_recovery_explicitly_refuses_continuity(self):
        self.assertFalse(self.runtime.orchestrator.c.recovery.continuity_verified(1))
        self.assert_closed()

    def test_isolation_capacity_exhaustion_invalidates_epoch(self):
        self.pipeline.symbol_isolation.max_symbols = 1
        self.pipeline.symbol_isolation.block("MSFT", "TEST_LIMIT", NOW)
        self.check_epoch_refusal(revision())


if __name__ == "__main__":
    unittest.main()
