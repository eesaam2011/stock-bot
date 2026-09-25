"""EHR G5 batch-25 runner. After-close only; no Redis/trading/returns."""
import datetime as dt, hashlib, json, os, threading, time
from pathlib import Path
from zoneinfo import ZoneInfo
from ehr_g5_isolated_collector import collect, atomic_json

NY=ZoneInfo("America/New_York")
lock=threading.Lock(); thread=None
state={"status":"IDLE","completed":0,"total":0,"failures":[],"elapsed_seconds":0.0,
       "returns_computed":False,"backtest_authorized":False}

def after_close(now=None):
    x=(now or dt.datetime.now(dt.timezone.utc)).astimezone(NY)
    return x.weekday()>=5 or (x.hour,x.minute)>=(17,30)

def register(app,authorized):
    from flask import jsonify
    @app.post("/ehr-g5/start-batch25")
    def start():
        global thread
        if not authorized(): return jsonify({"ok":False,"error":"unauthorized"}),401
        if os.getenv("EHR_G5_ENABLED")!="1": return jsonify({"ok":False,"error":"disabled"}),403
        if not after_close(): return jsonify({"ok":False,"error":"outside_after_close_window"}),409
        folder=Path(os.getenv("EHR_G5_OUTPUT_DIR","/tmp/ehr_g5_pilot"))
        source=Path(os.getenv("EHR_G5_REQUIREMENTS_PATH") or folder/"private_requirements.json")
        try:
            from ehr_g5_preflight import verify
            manifest=verify(source); req=json.loads(source.read_text())["requirements"]
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return jsonify({"ok":False,"error":"requirements_preflight_failed","error_type":type(exc).__name__}),503
        folder.mkdir(parents=True,exist_ok=True)
        pending=[]
        for symbol in sorted({r["symbol"] for r in req}):
            p=folder/(hashlib.sha256(symbol.encode()).hexdigest()+".json")
            try: old=json.loads(p.read_text()) if p.exists() else {}
            except (OSError,ValueError): old={}
            if old.get("symbol")==symbol and old.get("feed")=="sip" and old.get("end")=="2026-09-22": continue
            pending.append(symbol)
        symbols=pending[:25]
        if not symbols:return jsonify({"ok":True,"status":"NO_PENDING_SYMBOLS","total":0}),200
        first=min(r["next_regular_entry_session"] for r in req)
        with lock:
            if thread and thread.is_alive():return jsonify({"ok":False,"error":"already_running"}),409
            state.update(status="RUNNING",completed=0,total=len(symbols),failures=[],elapsed_seconds=0.0,
                         requirements_sha256=manifest["file_sha256"])
            thread=threading.Thread(target=_run,args=(symbols,first,folder),daemon=True,name="ehr-g5-batch25")
            thread.start()
        return jsonify({"ok":True,"status":"RUNNING","total":len(symbols),"status_url":"/ehr-g5/batch25-status"}),202

    @app.get("/ehr-g5/batch25-status")
    def status():
        if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
        with lock:return jsonify(dict(state))

def _run(symbols,first,folder):
    from urllib.parse import urlencode
    from urllib.request import Request,urlopen
    def fetch(symbol,start,end,token):
        q={"symbols":symbol,"timeframe":"1Day","start":start+"T00:00:00Z","end":end+"T23:59:59Z",
           "feed":"sip","adjustment":"raw","sort":"asc","limit":1000}
        if token:q["page_token"]=token
        h={"APCA-API-KEY-ID":os.environ["ALPACA_API_KEY"],"APCA-API-SECRET-KEY":os.environ["ALPACA_SECRET_KEY"]}
        with urlopen(Request("https://data.alpaca.markets/v2/stocks/bars?"+urlencode(q),headers=h),timeout=25) as r:return json.load(r)
    failures=[]; began=time.monotonic()
    budget=min(180.0,max(30.0,float(os.getenv("EHR_G5_MAX_BATCH_SECONDS","120"))))
    try:
        for symbol in symbols:
            if time.monotonic()-began>=budget:
                with lock:state.update(status="STOPPED_SAFE",stop_reason="time_budget_exhausted")
                return
            if not after_close():
                with lock:state.update(status="STOPPED_SAFE",stop_reason="market_window_closed")
                return
            try:
                bars=collect(symbol,first,"2026-09-22",fetch)
                p=folder/(hashlib.sha256(symbol.encode()).hexdigest()+".json")
                atomic_json(p,{"symbol":symbol,"start":first,"end":"2026-09-22","feed":"sip","adjustment":"raw","bars":bars})
            except Exception as exc:failures.append({"symbol":symbol,"error_type":type(exc).__name__})
            with lock:state.update(completed=state["completed"]+1,failures=list(failures),elapsed_seconds=round(time.monotonic()-began,3))
            time.sleep(1)
        with lock:state["status"]="COMPLETED" if not failures else "PARTIAL"
    except Exception as exc:
        with lock:state.update(status="FAILED",error_type=type(exc).__name__)
