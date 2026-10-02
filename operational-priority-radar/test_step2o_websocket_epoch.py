"""Step 2O: ACK epoch, normal EOF and 407 both invalidate trust."""
import asyncio
import json
import unittest
from alpaca_production_market import AlpacaSIPProtocol
from websocket_runtime import WebSocketRuntime,WebSocketProtocolError

def packet(x):return json.dumps([x])
class FakeWS:
 def __init__(self,events=(),failure=None):
  self.controls=[
   packet({"T":"success","msg":"connected"}),
   packet({"T":"success","msg":"authenticated"}),
   packet({"T":"subscription","trades":["A"],"bars":["A"],"statuses":["*"]})]
  self.events=list(events);self.failure=failure;self.sent=[]
 async def __aenter__(self):return self
 async def __aexit__(self,*args):return False
 async def recv(self):return self.controls.pop(0)
 async def send(self,msg):self.sent.append(json.loads(msg))
 def __aiter__(self):return self._events()
 async def _events(self):
  for msg in self.events:
   yield packet(msg)
  if self.failure:raise self.failure

class TestWebSocketEpoch(unittest.IsolatedAsyncioTestCase):
 async def test_normal_eof_clears_ack_and_notifies_disconnect(self):
  ws=FakeWS([{"T":"b","S":"A","c":10}]);seen=[];disconnect=[]
  runtime=None
  async def on_message(m):seen.append((m,runtime.connection_epoch,runtime.connected_event.is_set()))
  async def on_disconnect():disconnect.append(runtime.connected_event.is_set())
  runtime=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,on_message,on_disconnect)
  with self.assertRaisesRegex(WebSocketProtocolError,"SIP_STREAM_EOF_UNTRUSTED"):
   await runtime.run_once("u","k","s",["A"])
  self.assertEqual(runtime.connection_epoch,1)
  self.assertEqual(seen[0][1:],(1,True))
  self.assertEqual(disconnect,[False])
  self.assertFalse(runtime.connected_event.is_set())
  self.assertIn("SIP_STREAM_EOF_UNTRUSTED",runtime.last_error)
  snap=runtime.performance_snapshot()
  self.assertEqual((snap["received"],snap["handled"],snap["processing_ms"]["BAR"]["count"]),(1,1,1))
 async def test_407_then_successor_ack_has_new_epoch_and_never_reuses_trust(self):
  sockets=[FakeWS(failure=RuntimeError("407 slow client")),FakeWS()]
  seen=[];runtime=None
  async def on_disconnect():seen.append((runtime.connection_epoch,runtime.connected_event.is_set()))
  runtime=WebSocketRuntime(lambda url:sockets.pop(0),AlpacaSIPProtocol,lambda m:asyncio.sleep(0),on_disconnect)
  with self.assertRaisesRegex(RuntimeError,"407 slow client"):
   await runtime.run_once("u","k","s",["A"])
  self.assertFalse(runtime.connected_event.is_set())
  with self.assertRaisesRegex(WebSocketProtocolError,"SIP_STREAM_EOF_UNTRUSTED"):
   await runtime.run_once("u","k","s",["A"])
  self.assertEqual(runtime.connection_epoch,2)
  self.assertEqual(seen,[(1,False),(2,False)])
 async def test_subscription_incomplete_never_increments_epoch(self):
  ws=FakeWS()
  ws.controls[-1]=packet({"T":"subscription","trades":["A"],"bars":[],"statuses":["*"]})
  disconnected=[]
  async def on_disconnect():disconnected.append(True)
  runtime=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,lambda m:asyncio.sleep(0),on_disconnect)
  with self.assertRaisesRegex(WebSocketProtocolError,"SUBSCRIPTION_INCOMPLETE"):
   await runtime.run_once("u","k","s",["A"])
  self.assertEqual(runtime.connection_epoch,0)
  self.assertEqual(disconnected,[True])
 async def test_processing_latency_is_bounded_sample(self):
  ws=FakeWS([{"T":"t","S":"A","p":1,"t":"2026-09-22T15:00:00Z"}]*3)
  async def on_message(m):await asyncio.sleep(.002)
  runtime=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,on_message,lambda:asyncio.sleep(0))
  with self.assertRaises(WebSocketProtocolError):
   await runtime.run_once("u","k","s",["A"])
  stats=runtime.performance_snapshot()
  self.assertEqual(stats["processing_ms"]["TRADE"]["count"],3)
  self.assertGreaterEqual(stats["processing_ms"]["TRADE"]["p50"],1.0)
  self.assertIsNotNone(stats["observed_rx_per_sec"])

if __name__=="__main__":unittest.main()
