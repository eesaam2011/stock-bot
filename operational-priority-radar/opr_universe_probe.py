import os,json,urllib.request,urllib.parse,urllib.error
def emit(name,ok,detail): print(json.dumps({"check":name,"ok":ok,"detail":detail}),flush=True)
k=os.getenv("APCA_API_KEY_ID",""); s=os.getenv("APCA_API_SECRET_KEY","")
h={"APCA-API-KEY-ID":k,"APCA-API-SECRET-KEY":s,"User-Agent":"opr-universe-probe/1.0"}

# Probe Market Data endpoints that could support universe discovery without Trading API.
tests=[
 ("snapshots_wildcard","https://data.alpaca.markets/v2/stocks/snapshots?symbols=*"),
 ("most_actives","https://data.alpaca.markets/v1beta1/screener/stocks/most-actives?top=100"),
]
for name,url in tests:
 try:
  req=urllib.request.Request(url,headers=h)
  with urllib.request.urlopen(req,timeout=20) as r:
   body=r.read(2048)
   emit(name,200<=r.status<300,{"status":r.status,"sample_bytes":len(body),"content_type":r.headers.get("content-type")})
 except urllib.error.HTTPError as e:
  emit(name,False,{"status":e.code,"error":e.read(300).decode("utf-8","replace")})
 except Exception as e:
  emit(name,False,{"error_type":type(e).__name__,"error":str(e)[:250]})
