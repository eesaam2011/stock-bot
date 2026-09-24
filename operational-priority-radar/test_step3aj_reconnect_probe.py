"""The live diagnostic uses the actual reconnect loop and scopes epochs."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from live_sip_soak import run_live_reconnect_probe
from websocket_runtime import SIPErrorFrame, WebSocketRuntime


class FakeSocket:
    def __init__(self, number):
        self.number = number
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
        if self.number == 1:
            raise TimeoutError("sent 1011 keepalive ping timeout")
        await asyncio.Future()


class TestReconnectProbe(unittest.IsolatedAsyncioTestCase):
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
