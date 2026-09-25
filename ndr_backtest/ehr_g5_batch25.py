"""EHR G5 bounded after-close batch collector.

Imported by the existing Flask service. It never writes Redis, never trades,
never computes returns, and only runs after 17:30 New York time/weekends.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo
from ehr_g5_isolated_collector import collect, atomic_json

_NY=ZoneInfo("America/New_York")
_lock=threading.Lock()
_thread=None
_state={"status":"IDLE","completed":0,"total":0,"batch_size":0,"failures":[],
        "elapsed_seconds":0.0,"stop_reason":None,"returns_computed":False,
        "backtest_authorized":False}

def after_close(now=None):
    local=(now or dt.datetime.now(dt.timezone.utc)).astimezone(_NY)
    return local.weekday()>=5 or (local.hour,local.minute)>=(17,30)

def register_batch25(app, authorized):
    from flask import jsonify
    @app.post("/ehr-g5/start-batch25")
    def start_batch25():
        global _thread
        if not authorized(): return jsonify({"ok":False,"error":"unauthorized"}),401
        if os.getenv("EHR_G5_ENABLED")!="1": return jsonify({"ok":False,"error":"disabled"}),403
        if not after_close(): return jsonify({"ok":False,"error":"outside_after_close_window"}),409
        out=Path(os.getenv("EHR_G5_OUTPUT_DIR","/tmp/ehr_g5_pilot"))
        req_path=Path(os.getenv("EHR_G5_REQUIREMENTS_PATH") or out/"private_requirements.json")
        if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
            return jsonify({"ok":False,"error":"alpaca_credentials_not_configured"}),503
        try:
            from ehr_g5_preflight import verify
            manifest=verify(req_path)
            req=json.loads(req_path.read_text())["requirements"]
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return jsonify({"ok":False,"error":"requirements_preflight_failed",
                            "error_type":type(exc).__name__}),503
        out.mkdir(parents=True,exist_ok=True)
        pending=[]
        for symbol in sorted({r["symbol"] for r in req}):
            target=out/(hashlib.sha256(symbol.encode()).hexdigest()+".json")
            try: old=json.loads(target.read_text()) if target.exists() else {}
            except (OSError,ValueError): old={}
            if old.get("symbol")==symbol and old.get("feed")=="sip" and old.get("end")=="2026-09-22":
                continue
            pending.append(symbol)
        symbols=pending[:25]
        if not symbols:
            return jsonify({"ok":True,"status":"NO_PENDING_SYMBOLS","total":0}),200
        start=min(r["next_regular_entry_session"] for r in req)
        with _lock:
            if _thread and _thread.is_alive():
                return jsonify({"ok":False,"error":"already_running"}),409
            _state.update(status="RUNNING",completed=0,total=len(symbols),batch_size=len(symbols),
                          failures=[],elapsed_seconds=0.0,stop_reason=None,
                          requirements_sha256=manifest["file_sha256"])
            _thread=threading.Thread(target=_run,args=(symbols,start,out),
                                     name="ehr-g5-batch25",daemon=True)
            _thread.start()
        return jsonify({"ok":True,"status":"RUNNING","total":len(symbols),
                        "status_url":"/ehr-g5/status"}),202

def _run(symbols,start,out):
    from urllib.parse import urlencode
    from urllib.request import Request,urlopen
    def fetch(symbol,first,last,token):
        params={"symbols":symbol,"timeframe":"1Day","start":first+"T00:00:00Z",
                "end":last+"T23:59:59Z","feed":"sip","adjustment":"raw",
                "sort":"asc","limit":1000}
        if token: params["page_token"]=token
        headers={"APCA-API-KEY-ID":os.environ["ALPACA_API_KEY"],
                 "APCA-API-SECRET-KEY":os.environ["ALPACA_SECRET_KEY"]}
        with urlopen(Request("https://data.alpaca.markets/v2/stocks/bars?"+urlencode(params),
                             headers=headers),timeout=25) as response:
            return json.load(response)
    failures=[]
    began=time.monotonic()
    budget=min(180.0,max(30.0,float(os.getenv("EHR_G5_MAX_BATCH_SECONDS","120"))))
    try:
        for symbol in symbols:
            if time.monotonic()-began>=budget:
                with _lock:_state["stop_reason"]="time_budget_exhausted"
                break
            if not after_close():
                with _lock:_state["stop_reason"]="market_window_closed"
                break
            target=out/(hashlib.sha256(symbol.encode()).hexdigest()+".json")
            try:
                bars=collect(symbol,start,"2026-09-22",fetch)
                atomic_json(target,{"symbol":symbol,"start":start,"end":"2026-09-22",
                                   "feed":"sip","adjustment":"raw","bars":bars})
            except Exception as exc:
                failures.append({"symbol":symbol,"error_type":type(exc).__name__})
            with _lock:
                _state["completed"]+=1
                _state["failures"]=list(failures)
                _state["elapsed_seconds"]=round(time.monotonic()-began,3)
            time.sleep(1)
        with _lock:
            _state["elapsed_seconds"]=round(time.monotonic()-began,3)
            _state["status"]="STOPPED_SAFE" if _state.get("stop_reason") else ("COMPLETED" if not failures else "PARTIAL")
    except Exception as exc:
        with _lock:_state.update(status="FAILED",error_type=type(exc).__name__,
                                 elapsed_seconds=round(time.monotonic()-began,3))

def status():
    with _lock:return dict(_state)
