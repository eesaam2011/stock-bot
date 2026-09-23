"""Step3O synthetic full WebSocketRuntime queue/capture and real BASE_READY stress.

No Alpaca credentials, Redis, real upstream arrival-rate measurement, or 407 proof.
"""
import argparse,asyncio,contextlib,json,sys,time
from datetime import datetime,timedelta,timezone
from alpaca_production_market import AlpacaSIPProtocol
from production_adapters import OperationalBaseReady
from sip_epoch_capture import BoundedEpochCapture,EpochCaptureOverflow
from websocket_runtime import WebSocketProtocolError,WebSocketRuntime

START=datetime(2026,9,22,14,30,tzinfo=timezone.utc)
def native_bar(i):
 return {"t":(START+timedelta(minutes=i)).isoformat(),
         "o":10+i*.01,"h":10.3+i*.01,"l":9.9+i*.01,
         "c":10.1+i*.01,"v":1000+i*50,"vw":10.05+i*.01,"n":5}
def messages(symbols,cycles):
 for cycle in range(cycles):
  for i in range(symbols):
   yield {"T":"b","S":f"SYM{i:04d}",**native_bar(60+cycle)}

class SyntheticSIPSocket:
 def __init__(self,events,*,frame_size,pacing,final_error=None):
  self.events=events;self.frame_size=frame_size;self.pacing=pacing
  self.final_error=final_error;self.runtime=None;self.sent=[]
  self.controls=[json.dumps([{"T":"success","msg":"connected"}]),
                 json.dumps([{"T":"success","msg":"authenticated"}])]
 async def __aenter__(self):return self
 async def __aexit__(self,*_):return False
 async def recv(self):return self.controls.pop(0)
 async def send(self,raw):self.sent.append(json.loads(raw))
 def __aiter__(self):return self._frames()
 async def _frames(self):
  for start in range(0,len(self.events),self.frame_size):
   if self.pacing:
    # Consumer-paced feed proves order and loss handling, NOT throughput.
    while self.runtime._handled<start:await asyncio.sleep(.0001)
   yield json.dumps(self.events[start:start+self.frame_size])
  if self.pacing:
   while self.runtime._handled<len(self.events):await asyncio.sleep(.0001)
  if self.final_error is not None:raise self.final_error

async def run_case(*,symbols,cycles,frame_size,pacing,max_messages=4096,
                   final_error=None,preseed=True):
 if not 1<=symbols<=5000 or not 1<=cycles<=30:
  raise ValueError("bounded stress universe")
 events=list(messages(symbols,cycles))
 if not 1<=frame_size<=len(events):raise ValueError("invalid frame size")
 base=OperationalBaseReady()
 if preseed:
  for i in range(symbols):
   sym=f"SYM{i:04d}"
   for j in range(60):
    base.on_completed_native_1m(sym,native_bar(j),
      START+timedelta(minutes=j+1))
 capture=BoundedEpochCapture(max_messages=max_messages,max_bytes=8*1024*1024)
 disconnected=[];processed_symbols=[];runtime=None
 def process(msg):
  symbol=msg["S"]
  base.on_completed_native_1m(symbol,msg,
    datetime.fromisoformat(msg["t"])+timedelta(minutes=1))
  processed_symbols.append(symbol)
 async def on_message(msg):await asyncio.to_thread(process,msg)
 async def on_disconnect():
  disconnected.append({"ack_cleared":not runtime.connected_event.is_set(),
                       "capture_invalid":capture.phase==capture.INVALID})
 sock=SyntheticSIPSocket(events,frame_size=frame_size,pacing=pacing,
                         final_error=final_error)
 runtime=WebSocketRuntime(lambda _:sock,AlpacaSIPProtocol,on_message,
                          on_disconnect,epoch_capture=capture,
                          dispatch_queue_max=1024)
 sock.runtime=runtime
 requested=[f"SYM{i:04d}" for i in range(symbols)]
 sock.controls.append(json.dumps([{"T":"subscription","trades":requested,
                                  "bars":requested,"statuses":["*"]}]))
 started=time.perf_counter();error=None
 try:
  # Runtime logs SIP_EPOCH_END to stdout; keep benchmark JSON parseable.
  with contextlib.redirect_stdout(sys.stderr):
   await runtime.run_once("synthetic://offline","no-key","no-secret",requested)
 except (WebSocketProtocolError,EpochCaptureOverflow,RuntimeError) as exc:
  error=f"{type(exc).__name__}:{exc}"
 elapsed=time.perf_counter()-started;stats=runtime.performance_snapshot()
 return {"type":"SYNTHETIC_OFFLINE_QUEUE_AND_BASE_READY_NOT_REAL_ALPACA_407",
         "symbols":symbols,"cycles":cycles,"offered_messages":len(events),
         "frame_size":frame_size,"paced_by_consumer":pacing,
         "capture_limit":max_messages,"dispatch_limit":1024,
         "elapsed_seconds":round(elapsed,4),
         "observed_handled_per_second":round(runtime._handled/max(elapsed,1e-6),2),
         "error":error,"received":stats["received"],"handled":stats["handled"],
         "processed_symbols":len(processed_symbols),
         "dispatch_queue":stats["dispatch_queue"],
         "processing_ms":stats["processing_ms"],"capture":stats["capture"],
         "disconnects":disconnected,"resident_tuple_bars":base.buffered_bars(),
         "production_recovery_eb_writes":0,"real_alpaca_407_reproduced":False,
         "full_session_coverage_proven":False,"shadow_deploy_authorized":False}

async def benchmark(symbols=300,cycles=10):
 paced=await run_case(symbols=symbols,cycles=cycles,frame_size=64,pacing=True)
 dispatch=await run_case(symbols=1500,cycles=1,frame_size=1500,
                         pacing=False,preseed=False)
 capture=await run_case(symbols=500,cycles=10,frame_size=64,
                        pacing=True,preseed=False)
 error407=await run_case(symbols=10,cycles=3,frame_size=10,pacing=True,
                         preseed=False,
                         final_error=RuntimeError("407 slow client synthetic"))
 assert paced["handled"]==symbols*cycles==paced["received"]
 assert paced["dispatch_queue"]["overflows"]==0
 assert "SIP_STREAM_EOF_UNTRUSTED" in paced["error"]
 assert "SIP_DISPATCH_QUEUE_OVERFLOW_FAIL_CLOSED" in dispatch["error"]
 assert dispatch["dispatch_queue"]["high_water"]==1024
 assert "SIP_CAPTURE_OVERFLOW_FAIL_CLOSED" in capture["error"]
 assert "407 slow client synthetic" in error407["error"]
 for case in (paced,dispatch,capture,error407):
  assert case["disconnects"]==[{"ack_cleared":True,"capture_invalid":True}]
  assert not case["shadow_deploy_authorized"]
 return {"scenario":"OFFLINE_SYNTHETIC_BURST_FOUR_CASES",
         "paced_real_base_ready":paced,
         "single_frame_dispatch_overflow":dispatch,
         "paced_capture_limit_overflow":capture,
         "injected_407_disconnect":error407,
         "observed_upstream_alpaca_arrival_rate":False,
         "real_407_causality_proven":False,"shadow_deploy_authorized":False}

if __name__=="__main__":
 p=argparse.ArgumentParser()
 p.add_argument("--symbols",type=int,default=300)
 p.add_argument("--cycles",type=int,default=10)
 a=p.parse_args()
 print(json.dumps(asyncio.run(benchmark(a.symbols,a.cycles)),indent=2,sort_keys=True))
