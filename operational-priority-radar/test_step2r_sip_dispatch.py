"""Step 2R: bounded ordered SIP receive queue, fail-closed on overload."""
import asyncio
import json
import unittest
from test_step2o_websocket_epoch import FakeWS
from websocket_runtime import WebSocketRuntime,WebSocketProtocolError
from alpaca_production_market import AlpacaSIPProtocol
from sip_epoch_capture import BoundedEpochCapture

def trade(n):
 return {"T":"t","S":"A","t":f"2026-09-22T15:00:{n:02d}Z","p":n}

class SlowWS(FakeWS):
 def __init__(self,events=()):
  super().__init__(events)
  self.keep_open=asyncio.Event()
 async def _events(self):
  for msg in self.events:
   yield json.dumps([msg])
  await self.keep_open.wait()

class TestBoundedDispatch(unittest.IsolatedAsyncioTestCase):
 async def test_overload_never_silently_drops_events(self):
  ws=FakeWS([trade(1),trade(2),trade(3)])
  processed=[]
  async def on_message(msg):
   processed.append(msg["p"])
   await asyncio.sleep(0.1)
  rt=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,on_message,
                      lambda:asyncio.sleep(0),dispatch_queue_max=1)
  with self.assertRaisesRegex(WebSocketProtocolError,"QUEUE_OVERFLOW"):
   await rt.run_once("u","k","s",["A"])
  stats=rt.performance_snapshot()
  self.assertEqual(stats["dispatch_queue"]["overflows"],1)
  self.assertFalse(rt.connected_event.is_set())
  self.assertLessEqual(stats["handled"],stats["received"])
 async def test_worker_failure_interrupts_idle_socket(self):
  ws=SlowWS([trade(1)])
  seen=[]
  async def fail(msg):
   seen.append(msg["p"])
   raise ValueError("SIP_HANDLER_FAILED")
  rt=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,fail,
                      lambda:asyncio.sleep(0),dispatch_queue_max=4)
  with self.assertRaisesRegex(ValueError,"SIP_HANDLER_FAILED"):
   await asyncio.wait_for(rt.run_once("u","k","s",["A"]),timeout=1)
  self.assertEqual(seen,[1])
  self.assertFalse(rt.connected_event.is_set())
 async def test_ordered_dispatch_without_overflow(self):
  ws=SlowWS([trade(1),trade(2),trade(3)])
  processed=[];done=asyncio.Event()
  async def handler(msg):
   processed.append(msg["p"])
   if len(processed)==3:done.set()
  rt=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,handler,
                      lambda:asyncio.sleep(0),dispatch_queue_max=4)
  task=asyncio.create_task(rt.run_once("u","k","s",["A"]))
  try:
   await asyncio.wait_for(done.wait(),timeout=1)
   self.assertEqual(processed,[1,2,3])
   self.assertEqual(rt.performance_snapshot()["handled"],3)
   self.assertLessEqual(rt.performance_snapshot()["dispatch_queue"]["high_water"],4)
  finally:
   rt.stop();task.cancel();await asyncio.gather(task,return_exceptions=True)
 async def test_capture_overflow_preempts_dispatch(self):
  ws=FakeWS([trade(1),trade(2)])
  capture=BoundedEpochCapture(max_messages=1,max_bytes=1024)
  rt=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,
                      lambda msg:asyncio.sleep(0),lambda:asyncio.sleep(0),
                      epoch_capture=capture,dispatch_queue_max=4)
  with self.assertRaisesRegex(RuntimeError,"CAPTURE_OVERFLOW"):
   await rt.run_once("u","k","s",["A"])
  self.assertEqual(capture.phase,capture.INVALID)
  self.assertFalse(rt.connected_event.is_set())

if __name__=="__main__":unittest.main()
