"""Step 2T: no SIP data loss in frames sharing a subscription ACK."""
import asyncio,json,unittest
from test_step2o_websocket_epoch import FakeWS
from websocket_runtime import WebSocketRuntime,WebSocketProtocolError
from alpaca_production_market import AlpacaSIPProtocol
from sip_epoch_capture import BoundedEpochCapture

def t(n):
 return {"T":"t","S":"A","p":n,"t":f"2026-09-22T15:00:{n:02d}Z"}
def ack():
 return {"T":"subscription","trades":["A"],"bars":["A"],"statuses":["*"]}

class SlowWS(FakeWS):
 def __init__(self):
  super().__init__([])
  self.keep_open=asyncio.Event()
 async def _events(self):
  await self.keep_open.wait()

class TestMixedAckFrame(unittest.IsolatedAsyncioTestCase):
 async def test_inline_post_ack_market_data_is_processed(self):
  ws=FakeWS([])
  ws.controls[-1]=json.dumps([ack(),t(1)])
  seen=[];capture=BoundedEpochCapture()
  async def on_message(msg):
   seen.append(msg["p"])
   self.assertEqual(capture.snapshot()["buffered"],1)
  rt=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,on_message,
                      lambda:asyncio.sleep(0),epoch_capture=capture)
  with self.assertRaisesRegex(WebSocketProtocolError,"SIP_STREAM_EOF"):
   await rt.run_once("u","k","s",["A"])
  self.assertEqual(seen,[1])
  self.assertEqual(rt.performance_snapshot()["received"],1)
 async def test_queued_post_ack_market_data_is_ordered(self):
  ws=SlowWS()
  ws.controls[-1]=json.dumps([ack(),t(1),t(2)])
  seen=[];done=asyncio.Event()
  async def on_message(msg):
   seen.append((msg["p"],msg["_sip_epoch"]))
   if len(seen)==2:done.set()
  rt=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,on_message,
                      lambda:asyncio.sleep(0),epoch_capture=BoundedEpochCapture(),
                      dispatch_queue_max=4)
  task=asyncio.create_task(rt.run_once("u","k","s",["A"]))
  try:
   await asyncio.wait_for(done.wait(),1)
   self.assertEqual(seen,[(1,1),(2,1)])
   self.assertEqual(rt.performance_snapshot()["received"],2)
  finally:
   rt.stop();task.cancel();await asyncio.gather(task,return_exceptions=True)
 async def test_pre_ack_data_fails_closed(self):
  ws=FakeWS([])
  ws.controls[-1]=json.dumps([t(1),ack()])
  capture=BoundedEpochCapture()
  rt=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,
                      lambda msg:asyncio.sleep(0),lambda:asyncio.sleep(0),
                      epoch_capture=capture)
  with self.assertRaisesRegex(WebSocketProtocolError,"SIP_DATA_BEFORE_SUBSCRIPTION_ACK"):
   await rt.run_once("u","k","s",["A"])
  self.assertEqual(rt.connection_epoch,0)
  self.assertEqual(capture.phase,capture.INVALID)
 async def test_data_before_auth_fails_closed(self):
  ws=FakeWS([])
  ws.controls[0]=json.dumps([{"T":"success","msg":"connected"},t(1)])
  rt=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,
                      lambda msg:asyncio.sleep(0),lambda:asyncio.sleep(0))
  with self.assertRaisesRegex(WebSocketProtocolError,"SIP_DATA_BEFORE_AUTH"):
   await rt.run_once("u","k","s",["A"])
  self.assertEqual(rt.connection_epoch,0)

if __name__=="__main__":unittest.main()
