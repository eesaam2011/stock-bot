import asyncio,json

class WebSocketProtocolError(RuntimeError):pass

class WebSocketRuntime:
 def __init__(self,connector,protocol,on_message,on_disconnect):
  self.connector=connector;self.protocol=protocol;self.on_message=on_message;self.on_disconnect=on_disconnect
  self.stopping=False;self.connected_event=asyncio.Event();self.last_error=None;self.subscription_stats={}

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

 async def run_once(self,url,key,secret,symbols):
  self.connected_event.clear();self.subscription_stats={};self.last_error=None
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
    self.connected_event.set()  # Means authenticated + subscription ACK verified, not merely TCP-open.
    async for raw in ws:
     if self.stopping:break
     for msg in self._decode(raw):
      if msg.get("T")=="error":raise WebSocketProtocolError(f"ALPACA_WS_ERROR_{msg.get('code')}:{msg.get('msg')}")
      await self.on_message(msg)
  except Exception as exc:
   self.last_error=f"{type(exc).__name__}:{exc}";self.connected_event.clear();await self.on_disconnect();raise

 async def reconnect_loop(self,*args,base_delay=1,max_delay=30):
  delay=base_delay
  while not self.stopping:
   try:await self.run_once(*args);delay=base_delay
   except Exception as exc:
    if self.stopping:return
    print({"stage":"SIP_WEBSOCKET_RECONNECT","error":f"{type(exc).__name__}:{exc}","retry_sec":delay},flush=True)
    await asyncio.sleep(delay);delay=min(delay*2,max_delay)
 def stop(self):self.stopping=True
