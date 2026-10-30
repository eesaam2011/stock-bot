"""No I/O on import. Actual HTTP invocation requires an external stage gate."""
import json,urllib.request,urllib.parse
class TransportRejected(RuntimeError):pass
class FixedGET:
 def __init__(self,authorize,credentials,opener=None):self.authorize=authorize;self.credentials=credentials;self.opener=opener
 def __call__(self,path,params):
  self.authorize()
  if path not in {'/v2/stocks/snapshots','/v2/stocks/bars','/v2/stocks/trades'}:raise TransportRejected('ENDPOINT_FORBIDDEN')
  key,secret=self.credentials()
  if not key or not secret:raise TransportRejected('MISSING_CREDENTIALS')
  req=urllib.request.Request('https://data.alpaca.markets'+path+'?'+urllib.parse.urlencode(params),headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret},method='GET')
  class NoRedirect(urllib.request.HTTPRedirectHandler):
   def redirect_request(self,*a,**k):return None
  opener=self.opener or urllib.request.build_opener(NoRedirect()).open
  try:
   with opener(req,timeout=20) as r:
    if r.status!=200:raise TransportRejected('HTTP_REJECTED')
    return json.loads(r.read())
  except Exception:raise TransportRejected('GET_FAILED_NO_SECRET_DETAILS') from None
class SocketTransport:
 URL='wss://stream.data.alpaca.markets/v2/sip'
 def __init__(self,authorize,credentials,connect=None):self.authorize=authorize;self.credentials=credentials;self.connect=connect
 async def open(self):
  self.authorize()
  connector=self.connect
  if connector is None:
   import websockets
   connector=websockets.connect
  key,secret=self.credentials()
  if not key or not secret:raise TransportRejected('MISSING_CREDENTIALS')
  try:
   ws=await connector(self.URL);await ws.send(json.dumps({'action':'auth','key':key,'secret':secret}));return ws
  except Exception:raise TransportRejected('WS_CONNECT_FAILED_NO_SECRET_DETAILS') from None
