"""Step3P: real runtime terminal diagnostic survives teardown, no payloads."""
import json,unittest,contextlib,io
from websocket_burst_stress import run_case,SyntheticSIPSocket
from benchmark_sip_queue_burst import scenario
from alpaca_production_market import AlpacaSIPProtocol
from sip_epoch_capture import BoundedEpochCapture
from websocket_runtime import WebSocketRuntime,WebSocketProtocolError

class TestTerminalForensics(unittest.IsolatedAsyncioTestCase):
 async def test_normal_eof_records_untrusted_not_continuity(self):
  r=await run_case(symbols=8,cycles=1,frame_size=8,pacing=True,preseed=False)
  d=r['last_epoch_diagnostic']
  self.assertEqual(d['failure_class'],'STREAM_EOF')
  self.assertEqual((d['received'],d['handled']),(8,8))
  self.assertEqual(d['capture_before_teardown']['phase'],'CAPTURING')
  self.assertEqual(r['capture']['phase'],'INVALID')
  self.assertEqual(r['forensic_seen_by_disconnect'],[True])
  self.assertFalse(d['continuity_proven'])
  self.assertFalse(d['direct_handoff_authorized'])
 async def test_dispatch_overflow_retains_1024_queue_evidence(self):
  r=await run_case(symbols=1500,cycles=1,frame_size=1500,pacing=False,preseed=False)
  d=r['last_epoch_diagnostic']
  self.assertEqual(d['failure_class'],'DISPATCH_QUEUE_OVERFLOW')
  self.assertEqual(d['dispatch_queue']['high_water'],1024)
  self.assertEqual(d['dispatch_queue']['overflows'],1)
  self.assertEqual(d['received'],1025)
  self.assertEqual(d['capture_before_teardown']['buffered'],1025)
  self.assertEqual(r['capture']['invalid_reason'],'SIP_DISCONNECTED')
 async def test_capture_overflow_root_cause_survives_teardown(self):
  r=await run_case(symbols=500,cycles=10,frame_size=64,pacing=True,preseed=False)
  d=r['last_epoch_diagnostic']
  self.assertEqual(d['failure_class'],'CAPTURE_OVERFLOW')
  self.assertEqual(d['capture_before_teardown']['invalid_reason'],'CAPTURE_OVERFLOW')
  self.assertEqual(d['capture_before_teardown']['phase'],'INVALID')
  self.assertEqual(r['capture']['invalid_reason'],'SIP_DISCONNECTED')
  self.assertEqual(d['received'],4096)
 async def test_synthetic_407_exception_not_mislabeled_alpaca(self):
  r=await run_case(symbols=8,cycles=1,frame_size=8,pacing=True,
                   preseed=False,final_error=RuntimeError('407 synthetic only'))
  self.assertEqual(r['last_epoch_diagnostic']['failure_class'],
                   'OTHER_407_EXCEPTION_NOT_ALPACA_PROOF')
  self.assertFalse(r['real_alpaca_407_reproduced'])
 async def test_synthetic_407_error_frame_is_labeled_frame_only(self):
  r=await scenario(8,mode='injected_407',frame_size=8)
  d=r['last_epoch_diagnostic']
  self.assertEqual(d['failure_class'],'SIP_ERROR_FRAME_407')
  self.assertFalse(r['real_407_reproduced'])
  self.assertEqual(d['capture_before_teardown']['phase'],'CAPTURING')
 async def test_pre_ack_failure_never_reuses_previous_epoch_metrics(self):
  first=SyntheticSIPSocket([{"T":"b","S":"A","t":"2026-09-22T15:30:00Z",
                             "o":10,"h":11,"l":9,"c":10,"v":100}],
                            frame_size=1,pacing=False)
  first.controls.append(json.dumps([{"T":"subscription","trades":["A"],
                                     "bars":["A"],"statuses":["*"]}]))
  second=SyntheticSIPSocket([],frame_size=1,pacing=False)
  second.controls[1]=json.dumps([{"T":"error","code":401,"msg":"synthetic auth failed"}])
  sockets=[first,second]
  capture=BoundedEpochCapture()
  async def on_message(msg):pass
  async def on_disconnect():pass
  rt=WebSocketRuntime(lambda url:sockets.pop(0),AlpacaSIPProtocol,
                      on_message,on_disconnect,epoch_capture=capture,
                      dispatch_queue_max=1024)
  first.runtime=rt;second.runtime=rt
  with contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaisesRegex(WebSocketProtocolError,'SIP_STREAM_EOF_UNTRUSTED'):
    await rt.run_once('synthetic','no-key','no-secret',['A'])
  self.assertEqual(rt.last_epoch_diagnostic['received'],1)
  self.assertTrue(rt.last_epoch_diagnostic['subscription_ack_verified'])
  with contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaisesRegex(WebSocketProtocolError,'ALPACA_WS_ERROR_401'):
    await rt.run_once('synthetic','no-key','no-secret',['A'])
  d=rt.last_epoch_diagnostic
  self.assertFalse(d['subscription_ack_verified'])
  self.assertIsNone(d['epoch'])
  self.assertEqual((d['received'],d['handled']),(0,0))
  self.assertEqual(d['capture_before_teardown']['phase'],'INVALID')
  self.assertEqual(d['capture_before_teardown']['invalid_reason'],
                   'STARTING_NEW_CONNECTION')
  self.assertIsNone(d['observed_rx_per_sec'])
 async def test_forensic_record_is_bounded_and_payload_free(self):
  r=await run_case(symbols=8,cycles=1,frame_size=8,pacing=True,preseed=False)
  d=r['last_epoch_diagnostic'];raw=json.dumps(d)
  self.assertLess(len(raw),2500)
  self.assertNotIn('SYM0000',raw)
  self.assertNotIn('NO_KEY',raw)
  self.assertNotIn('NO_SECRET',raw)
  self.assertNotIn('payload',raw)
  self.assertEqual(d['schema'],'OPR_SIP_EPOCH_TERMINAL_V1')

if __name__=='__main__':unittest.main()
