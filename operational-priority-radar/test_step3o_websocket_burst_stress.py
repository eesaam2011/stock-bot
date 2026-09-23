"""Step3O integrated synthetic WebSocketRuntime queue/capture + BASE_READY tests."""
import unittest
from websocket_burst_stress import run_case

class TestIntegratedWebSocketBurst(unittest.IsolatedAsyncioTestCase):
 async def test_paced_real_base_ready_no_silent_drop(self):
  r=await run_case(symbols=24,cycles=2,frame_size=8,pacing=True,preseed=True)
  self.assertEqual((r["received"],r["handled"],r["processed_symbols"]),(48,48,48))
  self.assertEqual(r["dispatch_queue"]["overflows"],0)
  self.assertLessEqual(r["dispatch_queue"]["high_water"],1024)
  self.assertEqual(r["processing_ms"]["BAR"]["count"],48)
  self.assertIsNotNone(r["processing_ms"]["BAR"]["p95"])
  self.assertEqual(r["resident_tuple_bars"],24*60)
  self.assertIn("SIP_STREAM_EOF_UNTRUSTED",r["error"])
  self.assertEqual(r["disconnects"],[{"ack_cleared":True,"capture_invalid":True}])
  self.assertFalse(r["real_alpaca_407_reproduced"])
  self.assertEqual(r["production_recovery_eb_writes"],0)
 async def test_single_frame_overflow_fails_closed_no_drop_oldest(self):
  r=await run_case(symbols=1500,cycles=1,frame_size=1500,pacing=False,preseed=False)
  self.assertIn("SIP_DISPATCH_QUEUE_OVERFLOW_FAIL_CLOSED",r["error"])
  self.assertEqual(r["dispatch_queue"]["high_water"],1024)
  self.assertEqual(r["dispatch_queue"]["overflows"],1)
  self.assertEqual(r["received"],1025)
  self.assertLessEqual(r["handled"],r["received"])
  self.assertEqual(r["disconnects"],[{"ack_cleared":True,"capture_invalid":True}])
 async def test_capture_overflow_even_when_queue_paced(self):
  r=await run_case(symbols=500,cycles=10,frame_size=64,pacing=True,preseed=False)
  self.assertIn("SIP_CAPTURE_OVERFLOW_FAIL_CLOSED",r["error"])
  self.assertEqual(r["dispatch_queue"]["overflows"],0)
  self.assertEqual(r["received"],4096)
  self.assertEqual(r["disconnects"],[{"ack_cleared":True,"capture_invalid":True}])
  self.assertFalse(r["shadow_deploy_authorized"])
 async def test_injected_407_disconnects_after_paced_processing(self):
  r=await run_case(symbols=12,cycles=3,frame_size=12,pacing=True,preseed=False,
                   final_error=RuntimeError("407 slow client synthetic"))
  self.assertIn("407 slow client synthetic",r["error"])
  self.assertEqual((r["received"],r["handled"]),(36,36))
  self.assertEqual(r["disconnects"],[{"ack_cleared":True,"capture_invalid":True}])
  self.assertFalse(r["real_alpaca_407_reproduced"])
 async def test_empty_capture_after_disconnect_does_not_imply_trust(self):
  r=await run_case(symbols=3,cycles=1,frame_size=3,pacing=True,preseed=False)
  self.assertFalse(r["full_session_coverage_proven"])
  self.assertFalse(r["shadow_deploy_authorized"])
  self.assertEqual(r["capture"]["phase"],"INVALID")
  self.assertEqual(r["production_recovery_eb_writes"],0)

if __name__=="__main__":unittest.main()
