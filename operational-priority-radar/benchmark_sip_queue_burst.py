"""Step3O: synthetic *integrated* SIP socket/queue/capture/BASE_READY stress.

Exercises the real WebSocketRuntime receiver, bounded 1024 dispatch queue,
single async consumer, asyncio.to_thread and compact-tuple BASE_READY. Fake
frames only: no Alpaca connection, no Redis, no deployment and no 407 proof.
Run: python benchmark_sip_queue_burst.py --symbols 3000 --burst 5000
"""
import argparse,asyncio,contextlib,io,json,resource,time
from datetime import datetime,timedelta,timezone
from collections import deque
from production_adapters import OperationalBaseReady
from websocket_runtime import WebSocketRuntime,WebSocketProtocolError
from sip_epoch_capture import BoundedEpochCapture,EpochCaptureOverflow
from alpaca_production_market import AlpacaSIPProtocol

START=datetime(2026,9,22,14,30,tzinfo=timezone.utc)
def bar(i,symbol):
    return {"T":"b","S":symbol,"t":(START+timedelta(minutes=i)).isoformat(),
            "o":10+i*.01,"h":10.3+i*.01,"l":9.9+i*.01,
            "c":10.1+i*.01,"v":1000+i*50,"vw":10.05+i*.01,"n":5}

class SyntheticSocket:
    def __init__(self,symbols,frames,*,paced=False,max_inflight=64,
                 inject_407=False):
        self.symbols=list(symbols);self.frames=frames;self.paced=paced
        self.max_inflight=max_inflight;self.inject_407=inject_407
        self.runtime=None;self.sent=0;self.sent_frames=0
        self.controls=[
            [{"T":"success","msg":"connected"}],
            [{"T":"success","msg":"authenticated"}],
            [{"T":"subscription","trades":self.symbols,"bars":self.symbols,
              "statuses":["*"]}]]
    async def __aenter__(self):return self
    async def __aexit__(self,*args):return False
    async def recv(self):return json.dumps(self.controls.pop(0))
    async def send(self,msg):pass
    def __aiter__(self):return self._frames()
    async def _wait_handled(self,threshold):
        # A synthetic paced producer: bound the inflight backlog without
        # touching production queue limits or suppressing real errors.
        deadline=time.monotonic()+45
        while self.runtime._handled<threshold:
            if time.monotonic()>deadline:
                raise RuntimeError("SYNTHETIC_PRODUCER_TIMED_OUT")
            await asyncio.sleep(.001)
    async def _frames(self):
        for frame in self.frames:
            if self.paced:
                await self._wait_handled(max(0,self.sent-self.max_inflight))
            self.sent+=len(frame);self.sent_frames+=1
            yield json.dumps(frame,separators=(",",":"))
        if self.paced:
            await self._wait_handled(self.sent)
        if self.inject_407:
            yield json.dumps([{"T":"error","code":407,"msg":"synthetic slow client"}])

async def scenario(symbol_count,*,mode,queue_limit=1024,capture_limit=4096,
                   frame_size=64):
    if not 1<=symbol_count<=20000:raise ValueError("invalid symbol count")
    symbols=[f"S{i:05d}" for i in range(symbol_count)]
    engine=OperationalBaseReady()
    # The paced case evaluates a mature 60-bar frozen BASE_READY for every
    # symbol. Overload cases use no-op processing to isolate queue/capture.
    if mode=="paced_mature":
        # Seed the exact production compact 59-bar representation. Do not
        # spend benchmark setup time re-evaluating all earlier 24..59 bars;
        # the measured socket path evaluates the real 60th bar once/symbol.
        for symbol in symbols:
            engine.history[symbol]=deque(
                (engine._compact_bar(bar(i,symbol)) for i in range(59)),
                maxlen=60)
    incoming=[bar(59,s) for s in symbols]
    if mode=="queue_overflow":
        frames=[incoming]  # one Alpaca-like batch: receiver cannot yield
    else:
        frames=[incoming[i:i+frame_size] for i in range(0,len(incoming),frame_size)]
    paced=mode!="queue_overflow"
    ws=SyntheticSocket(symbols,frames,paced=paced,max_inflight=frame_size,
                       inject_407=mode=="injected_407")
    capture=BoundedEpochCapture(max_messages=capture_limit,
                                max_bytes=8*1024*1024)
    processed=[];disconnect=[]
    async def on_message(msg):
        if mode=="paced_mature":
            out=await asyncio.to_thread(
                engine.on_completed_native_1m,msg["S"],msg,
                START+timedelta(minutes=60))
            if out.get("accepted"):processed.append(msg["S"])
        else:
            # Mirrors the production async-to-thread seam; deliberately no
            # REST/Redis/strategy side effects for overload isolation.
            await asyncio.to_thread(lambda:None)
            processed.append(msg["S"])
    async def on_disconnect():
        disconnect.append(True)
    runtime=WebSocketRuntime(lambda url:ws,AlpacaSIPProtocol,on_message,
                             on_disconnect,epoch_capture=capture,
                             dispatch_queue_max=queue_limit)
    ws.runtime=runtime
    started=time.perf_counter()
    # Production runtime emits a Python-dict epoch-end diagnostic on stdout.
    # Capture it as evidence instead of corrupting the benchmark JSON artifact.
    runtime_stdout=io.StringIO()
    with contextlib.redirect_stdout(runtime_stdout):
        try:
            await asyncio.wait_for(runtime.run_once("synthetic","NO_KEY","NO_SECRET",symbols),
                                   timeout=75)
            error="UNEXPECTED_SUCCESS"
        except (WebSocketProtocolError,EpochCaptureOverflow) as exc:
            error=str(exc)
    elapsed=time.perf_counter()-started
    snap=runtime.performance_snapshot()
    return {"scenario":mode,"symbols":symbol_count,"elapsed_seconds":round(elapsed,3),
        "received":snap["received"],"handled":snap["handled"],
        "accepted_mature_base_ready":len(processed) if mode=="paced_mature" else None,
        "dispatch_queue":snap["dispatch_queue"],
        "capture":snap["capture"],"processing_ms":snap["processing_ms"],
        "last_epoch_diagnostic":runtime.last_epoch_diagnostic,
        "observed_rx_per_sec":snap["observed_rx_per_sec"],
        "peak_process_rss_kib_linux":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "error":error,"disconnected":len(disconnect)==1,
        "epoch_end_diagnostic_emitted":"SIP_EPOCH_END" in runtime_stdout.getvalue(),
        "connected_after_disconnect":runtime.connected_event.is_set(),
        "production_queue_limit_used":queue_limit==1024,
        "production_capture_limit_used":capture_limit==4096,
        "real_alpaca_connection":False,"real_407_reproduced":False,
        "full_session_coverage_proven":False,
        "shadow_deploy_authorized":False}

async def run(symbols=3000,burst=5000):
    # A 5k event single-frame burst must fail closed on the real queue limit.
    # A paced 5k epoch without drain must fail closed at capture 4096.
    results=[
        await scenario(symbols,mode="paced_mature"),
        await scenario(burst,mode="queue_overflow"),
        await scenario(burst,mode="capture_overflow"),
        await scenario(128,mode="injected_407")]
    good,queue,cap,code=results
    assert good["error"]=="SIP_STREAM_EOF_UNTRUSTED"
    assert good["handled"]==good["received"]==symbols
    assert good["accepted_mature_base_ready"]==symbols
    assert good["dispatch_queue"]["overflows"]==0
    assert good["dispatch_queue"]["high_water"]<=1024
    assert "SIP_DISPATCH_QUEUE_OVERFLOW_FAIL_CLOSED" in queue["error"]
    assert queue["dispatch_queue"]["overflows"]==1
    assert queue["handled"]<queue["received"]
    assert "SIP_CAPTURE_OVERFLOW_FAIL_CLOSED" in cap["error"]
    assert cap["capture"]["invalid_reason"]=="SIP_DISCONNECTED"
    assert "ALPACA_WS_ERROR_407" in code["error"]
    assert all(x["disconnected"] and not x["connected_after_disconnect"]
               and x["capture"]["phase"]=="INVALID" for x in results)
    assert all(not x["shadow_deploy_authorized"] for x in results)
    return {"schema":"OPR_SYNTHETIC_INTEGRATED_SIP_STRESS_V1",
            "type":"OFFLINE_FAKE_SOCKET_NO_LIVE_SIP_PROOF",
            "queue_limit":1024,"capture_limit":4096,
            "results":results,
            "measured_real_upstream_lambda":False,
            "independent_full_session_proof":False,
            "real_alpaca_407_market_session_test":False,
            "production_direct_handoff_test":False,
            "merge_or_deploy_authorized":False}

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--symbols",type=int,default=3000)
    p.add_argument("--burst",type=int,default=5000)
    a=p.parse_args()
    print(json.dumps(asyncio.run(run(a.symbols,a.burst)),indent=2,
                     ensure_ascii=False,sort_keys=True))
