from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request
from ndr_backtest_engine import BacktestCollector

app = Flask(__name__)
UTC = timezone.utc
lock = threading.RLock()
worker_thread = None
analysis_thread = None
simulation_thread = None
stop_event = threading.Event()
analysis_state = {"status": "IDLE", "message": "Frozen 93/35 analysis has not started.", "rows_scanned": 0, "updated_at": None}
simulation_state = {"status":"IDLE","message":"Trade simulation has not started.","processed":0,"total":169,"session":None,"updated_at":None}
state = {
    "status": "IDLE",
    "phase": "SETUP",
    "message": "Historical BOATS must pass before the backtest can start.",
    "boats_test": None,
    "updated_at": None,
}


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def alpaca_headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }


def authorized() -> bool:
    expected = os.getenv("NDR_BT_ADMIN_TOKEN", "")
    supplied = (
        request.headers.get("X-Admin-Token")
        or request.form.get("token")
        or request.args.get("token")
        or ""
    )
    return bool(expected) and supplied == expected


def collector_status():
    try:
        return BacktestCollector().status()
    except Exception as exc:
        return {"phase": "REDIS_UNAVAILABLE", "message": f"{type(exc).__name__}: {exc}"}


def worker_loop():
    global worker_thread
    try:
        engine = BacktestCollector()
        while not stop_event.is_set():
            current = engine.status()
            phase = current.get("phase")
            if phase in ("DETAIL_READY", "DETAIL_INDEXING"):
                engine.detail_index_step()
            elif phase in ("DETAIL_REPLAY_READY", "DETAIL_DOWNLOAD_READY", "DETAIL_REPLAYING"):
                engine.detail_replay_step()
            elif phase == "COMPLETED":
                with lock:
                    state["status"] = "COMPLETED"
                    state["phase"] = "COMPLETED"
                    state["message"] = "Backtest and report completed."
                    state["updated_at"] = stamp()
                break
            else:
                raise RuntimeError(f"Unsupported or unsafe phase: {phase}. Expected existing v3 DETAIL_READY data.")
        if stop_event.is_set():
            with lock:
                state["status"] = "PAUSED"
                state["message"] = "Paused by user"
                state["updated_at"] = stamp()
    except Exception as exc:
        with lock:
            state["status"] = "ERROR"
            state["phase"] = "ERROR"
            state["message"] = f"{type(exc).__name__}: {exc}"
            state["updated_at"] = stamp()
    finally:
        worker_thread = None


def analysis_loop():
    global analysis_thread
    try:
        engine = BacktestCollector()
        def progress(rows):
            with lock:
                analysis_state.update(status="RUNNING", message="Scanning existing Redis results only", rows_scanned=rows, updated_at=stamp())
        result = engine.threshold_analysis(93, 35, progress)
        with lock:
            analysis_state.update(status="COMPLETED", message="Frozen 93/35 validation completed", rows_scanned=result["source_rows_scanned"], updated_at=stamp())
    except Exception as exc:
        with lock:
            analysis_state.update(status="ERROR", message=f"{type(exc).__name__}: {exc}", updated_at=stamp())
    finally:
        analysis_thread = None


def simulation_loop():
    global simulation_thread
    try:
        engine=BacktestCollector()
        def progress(processed,total,session):
            payload={"status":"RUNNING","message":"Simulating stored 93/35 entries minute by minute","processed":processed,"total":total,"session":session,"updated_at":stamp()};engine.redis.set_json(engine.key("simulation:93:35:status"),payload)
            with lock:simulation_state.update(payload)
        result=engine.run_trade_simulation(progress)
        payload={"status":"COMPLETED","message":"Trade simulation completed","processed":result["candidate_count"],"total":result["candidate_count"],"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("simulation:93:35:status"),payload)
        with lock:simulation_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("simulation:93:35:status"),{**simulation_state,**payload})
        except Exception:pass
        with lock:simulation_state.update(payload)
    finally:simulation_thread=None


def historical_boats_test() -> dict:
    # A liquid symbol and a completed overnight interval. Access is proven only
    # by at least one returned bar, never by HTTP 200 alone.
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    params = {
        "timeframe": "1Min",
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "feed": "boats",
        "limit": 10000,
        "adjustment": "raw",
        "sort": "asc",
    }
    url = "https://data.alpaca.markets/v2/stocks/AAPL/bars?" + urlencode(params)
    try:
        with urlopen(Request(url, headers=alpaca_headers()), timeout=40) as response:
            payload = json.load(response)
        bars = payload.get("bars") or []
        return {
            "ok": len(bars) > 0,
            "http_ok": True,
            "bars_count": len(bars),
            "first_bar": bars[0].get("t") if bars else None,
            "last_bar": bars[-1].get("t") if bars else None,
            "message": "Historical BOATS bars received" if bars else "HTTP succeeded but no BOATS bars were returned",
            "tested_at": stamp(),
        }
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        return {"ok": False, "http_ok": False, "http_status": exc.code,
                "message": detail, "tested_at": stamp()}
    except Exception as exc:
        return {"ok": False, "http_ok": False, "message": f"{type(exc).__name__}: {exc}", "tested_at": stamp()}


@app.get("/")
def home():
    return jsonify({
        "service": "Next-Day Radar Backtest",
        "source_version": os.getenv("NDR_BT_SOURCE_VERSION", "unknown"),
        "source_build": os.getenv("NDR_BT_SOURCE_BUILD", "unknown"),
        "status_url": "/status",
        "health_url": "/health",
        "report_url": "/report",
        "analysis_url": "/analysis/result",
        "simulation_url": "/simulation/result",
        "raw_case_url_template": "/api/results/case/YYYY-MM-DD/SYMBOL/approx",
        "next_step": "Use /start to index and resume the existing v3 detail replay.",
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "ndr-backtest", "status": state["status"]})


@app.get("/status")
def status():
    with lock:
        payload = dict(state)
    payload["collector"] = collector_status()
    return jsonify(payload)


@app.get("/report")
def report():
    return jsonify(BacktestCollector().report())


@app.get("/report/<mode>/<partition>")
def report_slice(mode: str, partition: str):
    if mode not in {"strict", "approx"} or partition not in {"development", "holdout"}:
        return jsonify({"ok": False, "error": "invalid_report_slice"}), 400
    payload = BacktestCollector().report()
    if not payload.get("modes"):
        return jsonify(payload), 202
    return jsonify({"generated_at": payload.get("generated_at"), "mode": mode,
                    "partition": partition, "results": payload["modes"][mode][partition]})


def valid_session(engine, session):
    return session in engine.redis.get_json(engine.key("manifest"),{}).get("sessions",[])


@app.get("/api/results")
def raw_results_index():
    return jsonify({"read_only":True,"case":"/api/results/case/YYYY-MM-DD/SYMBOL/approx","symbol":"/api/results/symbol/SYMBOL?offset=0&limit=10","session":"/api/results/session/YYYY-MM-DD?source=legacy&cursor=0&count=100","modes":["strict","approx"],"notes":["Symbol pages cover at most 15 sessions per request.","For session pages, follow next_source and next_cursor until next_source is null."]})


@app.get("/api/results/case/<session>/<symbol>/<mode>")
def raw_case(session: str, symbol: str, mode: str):
    engine=BacktestCollector();symbol=symbol.upper()
    if mode not in {"strict","approx"} or not valid_session(engine,session) or not re.fullmatch(r"[A-Z]{1,5}",symbol):return jsonify({"ok":False,"error":"invalid_case"}),400
    result=engine.raw_case(session,symbol,mode)
    return (jsonify(result),200) if result else (jsonify({"ok":False,"error":"case_not_found"}),404)


@app.get("/api/results/symbol/<symbol>")
def raw_symbol(symbol: str):
    symbol=symbol.upper()
    if not re.fullmatch(r"[A-Z]{1,5}",symbol):return jsonify({"ok":False,"error":"invalid_symbol"}),400
    try:offset=max(0,int(request.args.get("offset","0")));limit=max(1,min(15,int(request.args.get("limit","10"))))
    except ValueError:return jsonify({"ok":False,"error":"invalid_pagination"}),400
    payload=BacktestCollector().raw_symbol(symbol,offset,limit)
    payload["next_url"]=f"/api/results/symbol/{symbol}?offset={payload['next_offset']}&limit={limit}" if payload["next_offset"] is not None else None
    return jsonify(payload)


@app.get("/api/results/session/<session>")
def raw_session(session: str):
    engine=BacktestCollector();source=request.args.get("source","legacy");cursor=request.args.get("cursor","0")
    if not valid_session(engine,session) or source not in {"legacy","shard"} or not cursor.isdigit():return jsonify({"ok":False,"error":"invalid_session_page"}),400
    try:count=max(1,min(200,int(request.args.get("count","100"))))
    except ValueError:return jsonify({"ok":False,"error":"invalid_pagination"}),400
    payload=engine.raw_session(session,source,cursor,count)
    payload["next_url"]=f"/api/results/session/{session}?source={payload['next_source']}&cursor={payload['next_cursor']}&count={count}" if payload["next_source"] else None
    return jsonify(payload)


@app.get("/analysis/status")
def analysis_status():
    with lock: payload=dict(analysis_state)
    payload["result_ready"]=BacktestCollector().threshold_analysis_result() is not None
    payload["result_url"]="/analysis/result"
    return jsonify(payload)


@app.get("/analysis/result")
def analysis_result():
    result=BacktestCollector().threshold_analysis_result()
    return (jsonify(result),200) if result else (jsonify({"ready":False,"status_url":"/analysis/status"}),202)


@app.get("/simulation/status")
def simulation_status():
    engine=BacktestCollector();stored=engine.trade_simulation_status()
    with lock:payload=dict(stored or simulation_state)
    payload["result_ready"]=engine.trade_simulation_result() is not None;payload["result_url"]="/simulation/result";return jsonify(payload)


@app.get("/simulation/result")
def simulation_result():
    result=BacktestCollector().trade_simulation_result();return (jsonify(result),200) if result else (jsonify({"ready":False,"status_url":"/simulation/status"}),202)


@app.get("/control")
def control():
    return """
    <html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{font-family:Arial;background:#111;color:#eee;padding:24px}input,button{font-size:18px;padding:12px;margin:6px 0;width:100%;max-width:520px}button{background:#6d28d9;color:#fff;border:0;border-radius:8px}</style></head>
    <body><h2>NDR Backtest Control</h2><p>Paste the current admin token. It is sent in the form body, not the URL.</p>
    <form method="post" action="/start"><input name="token" type="password" placeholder="Admin token" required><button>Start / Resume backtest</button></form>
    <form method="post" action="/pause"><input name="token" type="password" placeholder="Admin token" required><button>Pause</button></form>
    <form method="post" action="/analysis/start"><input name="token" type="password" placeholder="Admin token" required><button>Run frozen 93/35 analysis</button></form>
    <form method="post" action="/simulation/start"><input name="token" type="password" placeholder="Admin token" required><button>Run decisive 93/35 trade simulation</button></form>
    <p><a style="color:#a78bfa" href="/status">View status</a> · <a style="color:#a78bfa" href="/report">View report</a> · <a style="color:#a78bfa" href="/analysis/status">Analysis status</a> · <a style="color:#a78bfa" href="/simulation/status">Simulation status</a></p></body></html>
    """


@app.route("/test/boats", methods=["GET", "POST"])
def test_boats():
    if not authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    result = historical_boats_test()
    with lock:
        state["boats_test"] = result
        state["updated_at"] = stamp()
        state["phase"] = "BOATS_VERIFIED" if result["ok"] else "BOATS_BLOCKED"
        state["message"] = result["message"]
    return jsonify(result), (200 if result["ok"] else 422)


@app.route("/start", methods=["GET", "POST"])
def start():
    global worker_thread
    if not authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    result = historical_boats_test()
    if not result.get("ok"):
        return jsonify({"ok": False, "error": "historical_boats_not_verified", "detail": result}), 409
    with lock:
        if worker_thread and worker_thread.is_alive():
            return jsonify({"ok": True, "status": "already_running", "status_url": "/status"})
        stop_event.clear()
        state["boats_test"] = result
        state["status"] = "RUNNING"
        state["phase"] = collector_status().get("phase", "DETAIL_READY")
        state["message"] = "Starting or resuming the current backtest phase"
        state["updated_at"] = stamp()
        worker_thread = threading.Thread(target=worker_loop, name="ndr-backtest-worker", daemon=True)
        worker_thread.start()
    return jsonify({"ok": True, "status": "started", "status_url": "/status"})


@app.route("/pause", methods=["POST"])
def pause():
    if not authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    stop_event.set()
    return jsonify({"ok": True, "status": "pause_requested", "status_url": "/status"})


@app.post("/analysis/start")
def start_analysis():
    global analysis_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    if BacktestCollector().status().get("phase")!="COMPLETED":return jsonify({"ok":False,"error":"backtest_not_completed"}),409
    with lock:
        cached=BacktestCollector().threshold_analysis_result()
        if cached:return jsonify({"ok":True,"status":"already_completed","result_url":"/analysis/result"})
        if analysis_thread and analysis_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/analysis/status"})
        analysis_state.update(status="RUNNING",message="Starting frozen 93/35 validation from stored results",rows_scanned=0,updated_at=stamp())
        analysis_thread=threading.Thread(target=analysis_loop,name="ndr-threshold-analysis",daemon=True);analysis_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/analysis/status"})


@app.post("/simulation/start")
def start_simulation():
    global simulation_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    if engine.threshold_analysis_result() is None:return jsonify({"ok":False,"error":"frozen_analysis_not_completed"}),409
    with lock:
        if engine.trade_simulation_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/simulation/result"})
        if simulation_thread and simulation_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/simulation/status"})
        payload={"status":"RUNNING","message":"Selecting stored 93/35 entries","processed":0,"total":169,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("simulation:93:35:status"),payload);simulation_state.update(payload);simulation_thread=threading.Thread(target=simulation_loop,name="ndr-trade-simulation",daemon=True);simulation_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/simulation/status","result_url":"/simulation/result"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
