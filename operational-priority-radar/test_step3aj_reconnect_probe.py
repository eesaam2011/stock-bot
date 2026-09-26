"""The live diagnostic uses the actual reconnect loop and scopes epochs."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_sip_soak import run_live_reconnect_probe
from websocket_runtime import SIPErrorFrame, WebSocketRuntime
from alpaca_production_market import AlpacaSIPProtocol
from sip_epoch_capture import BoundedEpochCapture, EpochCaptureError


class FakeSocket:
    def __init__(self, number):
        self.number = number
        self.sent_trade = False
        self.frames = iter([
            json.dumps([{"T": "success", "msg": "connected"}]),
            json.dumps([{"T": "success", "msg": "authenticated"}]),
            json.dumps([{"T": "subscription", "trades": ["AAPL"],
                         "bars": ["AAPL"], "statuses": ["*"]}]),
        ])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, value):
        pass

    async def recv(self):
        return next(self.frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.sent_trade:
            self.sent_trade = True
            return json.dumps([{"T": "t", "S": "AAPL",
                                "t": "2026-09-24T13:30:01Z", "p": 10.0,
                                "s": 1}])
        if self.number == 1:
            await asyncio.sleep(0.03)
            raise TimeoutError("sent 1011 keepalive ping timeout")
        await asyncio.Future()


class TestReconnectProbe(unittest.IsolatedAsyncioTestCase):
    async def test_trade_revisions_invalidate_epoch_without_acking(self):
        for revision in ("x", "c"):
            with self.subTest(revision=revision):
                capture = BoundedEpochCapture()
                capture.start(1)
                capture.ingest(1, {"T": "t", "S": "AAPL",
                                   "t": "2026-09-24T13:30:01Z", "p": 10, "s": 1})
                with self.assertRaisesRegex(EpochCaptureError,
                                            "SIP_TRADE_REVISION_UNRECONCILED"):
                    capture.ingest(1, {"T": revision, "S": "AAPL",
                                       "t": "2026-09-24T13:30:02Z", "i": 1})
                snapshot = capture.snapshot()
                self.assertEqual(snapshot["phase"], capture.INVALID)
                self.assertEqual(snapshot["invalid_reason"],
                                 "SIP_TRADE_REVISION_UNRECONCILED")
                self.assertEqual(snapshot["acked_upto"], 0)

    async def test_intentional_stop_drains_already_received_queue(self):
        class BurstSocket(FakeSocket):
            def __init__(self):
                super().__init__(1)
                self.sent = False

            async def __anext__(self):
                if not self.sent:
                    self.sent = True
                    return json.dumps([{"T": "t", "S": "AAPL",
                                        "t": "2026-09-24T13:30:01Z", "p": 10,
                                        "s": 1} for _ in range(10)])
                await asyncio.Future()

        handled = []

        async def on_message(msg):
            await asyncio.sleep(0.02)
            handled.append(msg)

        async def on_disconnect():
            pass

        runtime = WebSocketRuntime(lambda url: BurstSocket(), AlpacaSIPProtocol,
                                   on_message, on_disconnect,
                                   dispatch_queue_max=20)
        task = asyncio.create_task(runtime.run_once("url", "key", "secret", ["AAPL"]))
        await asyncio.wait_for(runtime.connected_event.wait(), 1)
        for _ in range(50):
            if runtime.performance_snapshot()["received"] == 10:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(runtime.performance_snapshot()["received"], 10)
        runtime.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(handled), 10)
        self.assertEqual(runtime.last_epoch_diagnostic["received"], 10)
        self.assertEqual(runtime.last_epoch_diagnostic["handled"], 10)

    async def test_controlled_disconnect_invokes_actual_reconnect(self):
        class ControlledSocket(FakeSocket):
            def __init__(self, number):
                super().__init__(number)
                self.closed = asyncio.Event()
                self.sent_control = False

            async def close(self, *, code, reason):
                self.closed.set()

            async def __anext__(self):
                if not self.sent_trade:
                    self.sent_trade = True
                    return json.dumps([{"T": "t", "S": "AAPL",
                                        "t": "2026-09-24T13:30:01Z",
                                        "p": 10.0, "s": 1}])
                if self.number == 1:
                    await self.closed.wait()
                    raise StopAsyncIteration
                if not self.sent_control:
                    self.sent_control = True
                    return json.dumps([{"T": "subscription", "trades": ["AAPL"],
                                        "bars": ["AAPL"], "statuses": ["*"]}])
                await asyncio.Future()

        connections = []

        def connector(url):
            ws = ControlledSocket(len(connections) + 1)
            connections.append(ws)
            return ws

        real_sleep = asyncio.sleep

        async def short_probe_sleep(seconds):
            return await real_sleep(1.25 if seconds == 60 else
                                    0.03 if seconds == 5 else seconds)

        with tempfile.TemporaryDirectory() as folder:
            with patch("live_sip_soak.asyncio.sleep", short_probe_sleep):
                result = await run_live_reconnect_probe(
                    duration_sec=60, max_symbols=1,
                    controlled_disconnect_after_sec=5,
                    output_path=str(Path(folder) / "evidence.json"),
                    connector=connector, symbols_override=["AAPL"],
                    env={"OPR_LIVE_SIP_SOAK": "I_UNDERSTAND_READ_ONLY_SIP",
                         "APCA_API_KEY_ID": "test", "APCA_API_SECRET_KEY": "test"})
        self.assertTrue(connections[0].closed.is_set())
        self.assertTrue(result["controlled_close_attempted"])
        self.assertTrue(result["reconnect_ack_observed"])
        self.assertTrue(result["each_recorded_epoch_exact"])
        self.assertEqual(result["epochs"][0]["terminal"]["failure_class"],
                         "STREAM_EOF")
        self.assertEqual([e["metrics_acked"] for e in result["epochs"]], [1, 1])
        self.assertEqual(result["epochs"][1]["terminal"]["received"], 2)
        self.assertEqual(result["epochs"][1]["terminal"]["known_control_received"], 1)
        self.assertFalse(result["continuity_proven"])

    async def test_real_reconnect_loop_acknowledges_new_epoch_without_coverage(self):
        connections = []

        def connector(url):
            connections.append(url)
            return FakeSocket(len(connections))

        real_sleep = asyncio.sleep

        async def short_probe_sleep(seconds):
            return await real_sleep(1.25 if seconds == 60 else seconds)

        with tempfile.TemporaryDirectory() as folder:
            with patch("live_sip_soak.asyncio.sleep", short_probe_sleep):
                proof = await run_live_reconnect_probe(
                    duration_sec=60, max_symbols=1,
                    output_path=str(Path(folder) / "evidence.json"),
                    connector=connector, symbols_override=["AAPL"],
                    env={"OPR_LIVE_SIP_SOAK": "I_UNDERSTAND_READ_ONLY_SIP",
                         "APCA_API_KEY_ID": "test", "APCA_API_SECRET_KEY": "test"})
            self.assertEqual(json.loads((Path(folder) / "evidence.json").read_text()), proof)
        self.assertGreaterEqual(len(connections), 2)
        self.assertTrue(proof["reconnect_ack_observed"])
        self.assertTrue(proof["each_recorded_epoch_exact"])
        self.assertEqual([row["terminal"]["received"] for row in proof["epochs"]], [1, 1])
        self.assertEqual([row["metrics_acked"] for row in proof["epochs"]], [1, 1])
        self.assertEqual([row["ledger"]["first_sequence"] for row in proof["epochs"]], [1, 1])
        self.assertEqual(proof["epochs"][1]["previous_disconnect_at"],
                         proof["epochs"][0]["disconnected_at"])
        self.assertGreater(proof["epochs"][1]["previous_disconnect_to_ack_ms"], 0)
        self.assertEqual(proof["epochs"][0]["terminal"]["failure_class"],
                         "OTHER_OR_CANCELLED")
        self.assertFalse(proof["full_session_coverage_proven"])
        self.assertFalse(proof["continuity_proven"])
        self.assertFalse(proof["direct_handoff_authorized"])

    async def test_407_string_is_not_a_sip_error_frame(self):
        class FakeProtocol:
            @staticmethod
            def auth(*args):
                return {}

            @staticmethod
            def subscribe(*args):
                return {}

        async def callback(*args):
            pass

        class SpoofSocket(FakeSocket):
            async def __anext__(self):
                raise RuntimeError("ALPACA_WS_ERROR_407 is just text")

        runtime = WebSocketRuntime(lambda url: SpoofSocket(1), FakeProtocol,
                                   callback, callback)
        with self.assertRaises(RuntimeError):
            await runtime.run_once("url", "key", "secret", ["AAPL"])
        self.assertEqual(runtime.last_epoch_diagnostic["failure_class"],
                         "OTHER_407_EXCEPTION_NOT_ALPACA_PROOF")
        self.assertEqual(SIPErrorFrame(407, "denied").code, 407)
