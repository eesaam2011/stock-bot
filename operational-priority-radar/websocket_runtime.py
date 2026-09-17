import asyncio,json
from market_trust import TrustState
class WebSocketRuntime:
 def __init__(self,connector,protocol,on_message,on_disconnect):
  self.connector=connector;self.protocol=protocol;self.on_message=on_message;self.on_disconnect=on_disconnect;self.stopping=False;self.connected_event=asyncio.Event()
 async def run_once(self,url,key,secret,symbols):
  try:
   async with self.connector(url) as ws:
    await ws.send(json.dumps(self.protocol.auth(key,secret)))
    await ws.send(json.dumps(self.protocol.subscribe(symbols)))
    self.connected_event.set()
    async for raw in ws:
     if self.stopping:break
     for msg in json.loads(raw) if raw.startswith("[") else [json.loads(raw)]:
      await self.on_message(msg)
  except Exception:
   self.connected_event.clear();await self.on_disconnect();raise
 async def reconnect_loop(self,*args,base_delay=1,max_delay=30):
  delay=base_delay
  while not self.stopping:
   try:await self.run_once(*args);delay=base_delay
   except Exception:
    if self.stopping:return
    await asyncio.sleep(delay);delay=min(delay*2,max_delay)
 def stop(self):self.stopping=True
