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
  self.connection_epoch=0;self._epoch_ack_verified=False
  # Retain one bounded, payload-free terminal record across teardown/reconnect.
  self.last_epoch_diagnostic=None
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
 async def _receive_queued(self,ws,initial_messages=()):
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
  def enqueue(msg):
   if msg.get("T")=="error":
    raise WebSocketProtocolError(f"ALPACA_WS_ERROR_{msg.get('code')}:{msg.get('msg')}")
   if self.epoch_capture:self.epoch_capture.ingest(self.connection_epoch,msg)
   self._received+=1
   try:q.put_nowait({**msg,"_sip_epoch":self.connection_epoch})
   except asyncio.QueueFull:
    self._queue_overflows+=1
    self.connected_event.clear()
    raise WebSocketProtocolError("SIP_DISPATCH_QUEUE_OVERFLOW_FAIL_CLOSED")
   self._queue_depth=q.qsize()
   self._queue_high_water=max(self._queue_high_water,self._queue_depth)
  iterator=ws.__aiter__()
  next_frame=None
  try:
   for msg in initial_messages:enqueue(msg)
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
    for msg in self._decode(raw):enqueue(msg)
  finally:
   # Supervisor cancellation may arrive while waiting for the next socket
   # frame. Cancel and join that task too; otherwise a pending __anext__
   # can survive the ACK epoch and consume a frame after disconnect.
   self.connected_event.clear()
   if next_frame is not None and not next_frame.done():
    next_frame.cancel()
    await asyncio.gather(next_frame,return_exceptions=True)
   worker.cancel()
   await asyncio.gather(worker,return_exceptions=True)
   self._queue_depth=0
 async def run_once(self,url,key,secret,symbols):
  self.connected_event.clear();self.subscription_stats={};self.last_error=None
  self._queue_depth=0;self._queue_high_water=0;self._queue_overflows=0
  # Pre-ACK failure must never inherit prior epoch throughput evidence.
  self._epoch_ack_verified=False;self._rx_started=None
  self._received=0;self._handled=0
  for samples in self._processing_ns.values():samples.clear()
  if self.epoch_capture:self.epoch_capture.invalidate("STARTING_NEW_CONNECTION")
  try:
   async with self.connector(url) as ws:
    welcome=await self._recv_control(ws)
    if not self._has_success(welcome,"connected"):
     raise WebSocketProtocolError("ALPACA_WS_CONNECTED_ACK_MISSING")
    if any(m.get("T") in {"b","t","s"} for m in welcome):
     raise WebSocketProtocolError("SIP_DATA_BEFORE_AUTH")
    await ws.send(json.dumps(self.protocol.auth(key,secret)))
    auth=await self._recv_control(ws)
    if not self._has_success(auth,"authenticated"):
     raise WebSocketProtocolError("ALPACA_WS_AUTH_ACK_MISSING")
    if any(m.get("T") in {"b","t","s"} for m in auth):
     raise WebSocketProtocolError("SIP_DATA_BEFORE_SUBSCRIPTION")
    requested=list(symbols)
    await ws.send(json.dumps(self.protocol.subscribe(requested)))
    sub_msgs=await self._recv_control(ws)
    sub_index=next((i for i,m in enumerate(sub_msgs) if m.get("T")=="subscription"),None)
    if sub_index is None:raise WebSocketProtocolError("ALPACA_WS_SUBSCRIPTION_ACK_MISSING")
    if any(m.get("T") in {"b","t","s"} for m in sub_msgs[:sub_index]):
     raise WebSocketProtocolError("SIP_DATA_BEFORE_SUBSCRIPTION_ACK")
    sub=sub_msgs[sub_index]
    # Alpaca may combine subscription ACK and market data in one frame.
    # Post-ACK events must enter this epoch; never silently discard them.
    initial_messages=[m for m in sub_msgs[sub_index+1:]
                      if m.get("T") in {"b","t","s"}]
    req=set(requested);got_trades=set(sub.get("trades") or []);got_bars=set(sub.get("bars") or [])
    statuses=set(sub.get("statuses") or [])
    missing_trades=req-got_trades;missing_bars=req-got_bars
    if missing_trades or missing_bars or "*" not in statuses:
     raise WebSocketProtocolError(
      f"ALPACA_WS_SUBSCRIPTION_INCOMPLETE:trades_missing={len(missing_trades)},bars_missing={len(missing_bars)},statuses_star={'*' in statuses}")
    self.subscription_stats={"requested":len(req),"trades":len(got_trades),"bars":len(got_bars),"statuses_star":True}
    self.connection_epoch+=1;self._epoch_ack_verified=True
    if self.epoch_capture:self.epoch_capture.start(self.connection_epoch)
    self._rx_started=time.monotonic();self._received=0;self._handled=0
    for samples in self._processing_ns.values():samples.clear()
    self.connected_event.set()  # Means authenticated + subscription ACK verified, not merely TCP-open.
    if self.dispatch_queue_max:
     await self._receive_queued(ws,initial_messages)
     return
    for msg in initial_messages:
     kind={"b":"BAR","t":"TRADE","s":"STATUS"}.get(msg.get("T"),"OTHER")
     self._received+=1;started=time.monotonic_ns()
     try:
      if self.epoch_capture:self.epoch_capture.ingest(self.connection_epoch,msg)
      await self.on_message(msg)
     finally:self._processing_ns[kind].append(time.monotonic_ns()-started)
     self._handled+=1
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
   # Capture's terminal reason is overwritten by SIP_DISCONNECTED during
   # cleanup. Retain bounded cause and queue evidence before invalidation.
   # No event payloads, symbols, credentials, or secrets are retained.
   self.connected_event.clear()
   before=self.performance_snapshot()
   err=self.last_error or ""
   if "SIP_DISPATCH_QUEUE_OVERFLOW_FAIL_CLOSED" in err:
    failure="DISPATCH_QUEUE_OVERFLOW"
   elif "SIP_CAPTURE_OVERFLOW_FAIL_CLOSED" in err:
    failure="CAPTURE_OVERFLOW"
   elif "ALPACA_WS_ERROR_407" in err:
    failure="SIP_ERROR_FRAME_407"
   elif "SIP_STREAM_EOF_UNTRUSTED" in err:
    failure="STREAM_EOF"
   elif "407" in err:
    failure="OTHER_407_EXCEPTION_NOT_ALPACA_PROOF"
   else:
    failure="OTHER_OR_CANCELLED"
   self.last_epoch_diagnostic={
    "schema":"OPR_SIP_EPOCH_TERMINAL_V1",
    "epoch":self.connection_epoch if self._epoch_ack_verified else None,
    "subscription_ack_verified":self._epoch_ack_verified,
    "failure_class":failure,
    "received":before["received"],"handled":before["handled"],
    "received_not_confirmed_handled":max(0,before["received"]-before["handled"]),
    "dispatch_queue":before["dispatch_queue"],
    "capture_before_teardown":before["capture"],
    "processing_ms":before["processing_ms"],
    "observed_rx_per_sec":before["observed_rx_per_sec"],
    "observed_upstream_arrival_rate":False,
    "continuity_proven":False,"direct_handoff_authorized":False}
   print({"stage":"SIP_EPOCH_END","terminal":self.last_epoch_diagnostic},flush=True)
   # Revoke ACK before callback; retain original capture overflow reason
   # only in last_epoch_diagnostic, never restore invalidated epoch.
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
