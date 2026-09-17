import os,json,urllib.request,urllib.error

key=os.getenv("APCA_API_KEY_ID","")
secret=os.getenv("APCA_API_SECRET_KEY","")
headers={
 "APCA-API-KEY-ID":key,
 "APCA-API-SECRET-KEY":secret,
 "User-Agent":"opr-paper-assets-probe/1.0",
}
url="https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity"
try:
 req=urllib.request.Request(url,headers=headers,method="GET")
 with urllib.request.urlopen(req,timeout=20) as r:
  body=r.read()
  data=json.loads(body.decode())
  print(json.dumps({
   "check":"paper_trading_assets",
   "ok":200 <= r.status < 300 and isinstance(data,list),
   "status":r.status,
   "assets_count":len(data) if isinstance(data,list) else None
  }),flush=True)
except urllib.error.HTTPError as e:
 print(json.dumps({
  "check":"paper_trading_assets","ok":False,"status":e.code,
  "alpaca_error":e.read(300).decode("utf-8","replace")
 }),flush=True)
except Exception as e:
 print(json.dumps({
  "check":"paper_trading_assets","ok":False,
  "error_type":type(e).__name__,"error":str(e)[:250]
 }),flush=True)
