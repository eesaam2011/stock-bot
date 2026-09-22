import asyncio,json,time
from collections import deque

class WebSocketProtocolError(RuntimeError):pass

class WebSocketRuntime:
 def __init__(self,connector,protocol,on_message,on_disconnect,epoch_capture=None,dispatch_queue_max=0):
  self.connector=connector;self.protocol=protocol;self.on_message=on_message;self.on_disconnect=on_disconnect
  self.epoch_capture=epoch_capture
  if not isinstance(dispatch_queue_max,int) or dispatch_queue_max<0 or dispatch_queue_max>100000:
   raise ValueError("invalid dispatch queue limit")
  self.dispatch_queue_max=dispatch_queue_max
  self._queue_depth=0;self._queue_high_water=0;self._queue_overflows=0
  self.stopping=False;self.connected_event=asyncio.Event();self.last_error=None;self.subscription_stats={}
  self.connection_epoch=0
  self._rx_started=None;self._received=0;self._handled=0
  self._processing_ns={k:deque(maxlen=2048) for k in ("BAR","TRADE","STATUS","OTHER")}

 @staticmethod
 def _decode(raw):
  if isinstance(raw,bytes):raw=raw.decode()
  obj=json.loads(raw)
  return obj if isinstance(obj,list) else [obj]

 async def _recv_control(self,ws,timeout=10.0):
  raw=await asyncio.wait_for(ws.recv(),timeout=timeout)
  msgs=self._decode(raw)
  for m in msgs:
   if m.get("T")=="error":
    raise WebSocketProtocolError(f"ALPACA_WS_ERROR_{m.get('code')}:{m.get('msg')}")
  return msgs

 @staticmethod
 def _has_success(msgs,text):
  return any(m.get("T")=="success" and m.get("msg")==text for m in msgs)

 def performance_snapshot(self):
  # Observed receive throughput is not upstream arrival rate. Samples are
  # bounded and represent on_message wall-clock latency, not socket wait.
  def percentile(xs,p):
   if not xs:return None
   z=sorted(xs);return round(z[min(len(z)-1,int((len(z)-1)*p))]/1e6,3)
  elapsed=max(time.monotonic()-self._rx_started,1e-6) if self._rx_started else None
  return {"epoch":self.connection_epoch,"received":self._received,
          "capture":self.epoch_capture.snapshot() if self.epoch_capture else None,
          "dispatch_queue":{"limit":self.dispatch_queue_max,
                            "depth":self._queue_depth,
                            "high_water":self._queue_high_water,
                            "overflows":self._queue_overflows},
          "handled":self._handled,
          "observed_rx_per_sec":round(self._received/elapsed,2) if elapsed else None,
          "processing_ms":{k:{"count":len(v),"p50":percentile(v,.5),
                              "p95":percentile(v,.95),"p99":percentile(v,.99),
                              "max":round(max(v)/1e6,3) if v else None}
                           for k,v in self._processing_ns.items()}}
 async def _receive_queued(self,ws):
  # Exactly one ordered consumer. The receiver never waits for synchronous
  # REST/Redis processing; full queue is a fatal disconnect, never a drop.
  q=asyncio.Queue(maxsize=self.dispatch_queue_max)
  async def consumer():
   while True:
    msg=await q.get()
    kind={"b":"BAR","t":"TRADE","s":"STATUS"}.get(msg.get("T"),"OTHER")
    started=time.monotonic_ns()
    try:await self.on_message(msg)
    finally:
     self._processing_ns[kind].append(time.monotonic_ns()-started)
     q.task_done();self._queue_depth=q.qsize()
    self._handled+=1
  worker=asyncio.create_task(consumer())
  iterator=ws.__aiter__()
  try:
   while not self.stopping:
    next_frame=asyncio.create_task(iterator.__anext__())
    done,_=await asyncio.wait({next_frame,worker},return_when=asyncio.FIRST_COMPLETED)
    if worker in done:
     next_frame.cancel()
     await asyncio.gather(next_frame,return_exceptions=True)
     worker.result()
     raise WebSocketProtocolError("SIP_DISPATCH_WORKER_ENDED")
    try:raw=next_frame.result()
    except StopAsyncIteration:
     raise WebSocketProtocolError("SIP_STREAM_EOF_UNTRUSTED")
    for msg in self._decode(raw):
     if msg.get("T")=="error":
      raise WebSocketProtocolError(f"ALPACA_WS_ERROR_{msg.get('code')}:{msg.get('msg')}")
     if self.epoch_capture:self.epoch_capture.ingest(self.connection_epoch,msg)
     self._received+=1
     try:q.put_nowait(msg)
     except asyncio.QueueFull:
      self._queue_overflows+=1
      self.connected_event.clear()
      raise WebSocketProtocolError("SIP_DISPATCH_QUEUE_OVERFLOW_FAIL_CLOSED")
     self._queue_depth=q.qsize()
     self._queue_high_water=max(self._queue_high_water,self._queue_depth)
  finally:
   self.connected_event.clear()
   worker.cancel()
   await asyncio.gather(worker,return_exceptions=True)
   self._queue_depth=0
 async def run_once(self,url,key,secret,symbols):
  self.connected_event.clear();self.subscription_stats={};self.last_error=None
  self._queue_depth=0;self._queue_high_water=0;self._queue_overflows=0
  if self.epoch_capture:self.epoch_capture.invalidate("STARTING_NEW_CONNECTION")
  try:
   async with self.connector(url) as ws:
    welcome=await self._recv_control(ws)
    if not self._has_success(welcome,"connected"):
     raise WebSocketProtocolError("ALPACA_WS_CONNECTED_ACK_MISSING")
    await ws.send(json.dumps(self.protocol.auth(key,secret)))
    auth=await self._recv_control(ws)
    if not self._has_success(auth,"authenticated"):
     raise WebSocketProtocolError("ALPACA_WS_AUTH_ACK_MISSING")
    requested=list(symbols)
    await ws.send(json.dumps(self.protocol.subscribe(requested)))
    sub_msgs=await self._recv_control(ws)
    sub=next((m for m in sub_msgs if m.get("T")=="subscription"),None)
    if sub is None:raise WebSocketProtocolError("ALPACA_WS_SUBSCRIPTION_ACK_MISSING")
    req=set(requested);got_trades=set(sub.get("trades") or []);got_bars=set(sub.get("bars") or [])
    statuses=set(sub.get("statuses") or [])
    missing_trades=req-got_trades;missing_bars=req-got_bars
    if missing_trades or missing_bars or "*" not in statuses:
     raise WebSocketProtocolError(
      f"ALPACA_WS_SUBSCRIPTION_INCOMPLETE:trades_missing={len(missing_trades)},bars_missing={len(missing_bars)},statuses_star={'*' in statuses}")
    self.subscription_stats={"requested":len(req),"trades":len(got_trades),"bars":len(got_bars),"statuses_star":True}
    self.connection_epoch+=1
    if self.epoch_capture:self.epoch_capture.start(self.connection_epoch)
    self._rx_started=time.monotonic();self._received=0;self._handled=0
    for samples in self._processing_ns.values():samples.clear()
    self.connected_event.set()  # Means authenticated + subscription ACK verified, not merely TCP-open.
    if self.dispatch_queue_max:
     await self._receive_queued(ws)
     return
    async for raw in ws:
     if self.stopping:break
     for msg in self._decode(raw):
      if msg.get("T")=="error":raise WebSocketProtocolError(f"ALPACA_WS_ERROR_{msg.get('code')}:{msg.get('msg')}")
      kind={"b":"BAR","t":"TRADE","s":"STATUS"}.get(msg.get("T"),"OTHER")
      self._received+=1;start_ns=time.monotonic_ns()
      try:
       if self.epoch_capture:self.epoch_capture.ingest(self.connection_epoch,msg)
       await self.on_message(msg)
      finally:self._processing_ns[kind].append(time.monotonic_ns()-start_ns)
      self._handled+=1
    if not self.stopping:
     raise WebSocketProtocolError("SIP_STREAM_EOF_UNTRUSTED")
  except Exception as exc:
   self.last_error=f"{type(exc).__name__}:{exc}"
   raise
  finally:
   # Preserve the last epoch's bounded 407 diagnostic before the next ACK
   # resets its counters. This is observed receive throughput, not upstream λ.
   print({"stage":"SIP_EPOCH_END","epoch":self.connection_epoch,
          "error":self.last_error,
          "receive_processing":self.performance_snapshot()},flush=True)
   # Normal async-iterator EOF is a disconnect too. Clear trust BEFORE the
   # callback, so no message from a successor epoch can use stale trust.
   self.connected_event.clear()
   if self.epoch_capture:self.epoch_capture.invalidate("SIP_DISCONNECTED")
   await self.on_disconnect()

 async def reconnect_loop(self,*args,base_delay=1,max_delay=30):
  delay=base_delay
  while not self.stopping:
   try:await self.run_once(*args);delay=base_delay
   except Exception as exc:
    if self.stopping:return
    print({"stage":"SIP_WEBSOCKET_RECONNECT","error":f"{type(exc).__name__}:{exc}","retry_sec":delay},flush=True)
    await asyncio.sleep(delay);delay=min(delay*2,max_delay)
 def stop(self):self.stopping=True
