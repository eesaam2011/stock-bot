"""Step3O integrated offline SIP stress safety and mature BASE_READY invariants."""
import unittest
from benchmark_sip_queue_burst import scenario,run

class TestIntegratedSyntheticSIPStress(unittest.IsolatedAsyncioTestCase):
 async def test_paced_mature_bars_all_processed_no_drop(self):
    r=await scenario(96,mode="paced_mature",frame_size=16)
    self.assertEqual(r["error"],"SIP_STREAM_EOF_UNTRUSTED")
    self.assertEqual((r["received"],r["handled"],
                      r["accepted_mature_base_ready"]),(96,96,96))
    self.assertEqual(r["dispatch_queue"]["overflows"],0)
    self.assertLessEqual(r["dispatch_queue"]["high_water"],1024)
    self.assertTrue(r["disconnected"])
    self.assertFalse(r["connected_after_disconnect"])
    self.assertEqual(r["capture"]["phase"],"INVALID")
    self.assertFalse(r["real_alpaca_connection"])
 async def test_1100_message_single_frame_queue_overflow_is_fatal(self):
    r=await scenario(1100,mode="queue_overflow")
    self.assertIn("SIP_DISPATCH_QUEUE_OVERFLOW_FAIL_CLOSED",r["error"])
    self.assertEqual(r["dispatch_queue"]["overflows"],1)
    self.assertEqual(r["dispatch_queue"]["high_water"],1024)
    self.assertLess(r["handled"],r["received"])
    self.assertEqual(r["capture"]["phase"],"INVALID")
    self.assertTrue(r["disconnected"])
 async def test_4100_paced_messages_capture_overflow_is_fatal(self):
    r=await scenario(4100,mode="capture_overflow")
    self.assertIn("SIP_CAPTURE_OVERFLOW_FAIL_CLOSED",r["error"])
    self.assertEqual(r["dispatch_queue"]["overflows"],0)
    self.assertEqual(r["capture"]["phase"],"INVALID")
    self.assertTrue(r["disconnected"])
    self.assertFalse(r["connected_after_disconnect"])
 async def test_injected_407_invalidates_ack_epoch(self):
    r=await scenario(64,mode="injected_407")
    self.assertIn("ALPACA_WS_ERROR_407",r["error"])
    self.assertEqual((r["received"],r["handled"]),(64,64))
    self.assertEqual(r["capture"]["phase"],"INVALID")
    self.assertFalse(r["real_407_reproduced"])
    self.assertFalse(r["shadow_deploy_authorized"])
 async def test_integrated_report_does_not_authorize_shadow(self):
    x=await run(64,4100)
    self.assertEqual(len(x["results"]),4)
    self.assertFalse(x["measured_real_upstream_lambda"])
    self.assertFalse(x["real_alpaca_407_market_session_test"])
    self.assertFalse(x["production_direct_handoff_test"])
    self.assertFalse(x["merge_or_deploy_authorized"])

if __name__=="__main__":unittest.main()
