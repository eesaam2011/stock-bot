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

from flask import Flask, jsonify, request, Response
from ndr_backtest_engine import BacktestCollector

app = Flask(__name__)
UTC = timezone.utc
lock = threading.RLock()
worker_thread = None
analysis_thread = None
simulation_thread = None
diagnostic_thread = None
explosions_thread = None
big_moves_thread = None
stop_event = threading.Event()
analysis_state = {"status": "IDLE", "message": "Frozen 93/35 analysis has not started.", "rows_scanned": 0, "updated_at": None}
simulation_state = {"status":"IDLE","message":"Trade simulation has not started.","processed":0,"total":169,"session":None,"updated_at":None}
diagnostic_state = {"status":"IDLE","message":"Stop diagnostic has not started.","processed":0,"total":169,"session":None,"updated_at":None}
explosions_state = {"status":"IDLE","message":"Explosion catalog has not started.","rows_scanned":0,"updated_at":None}
big_moves_state = {"status":"IDLE","message":"Big-move review has not started.","rows_scanned":0,"updated_at":None}
stopwidth_thread = None
stopwidth_state = {"status":"IDLE","message":"Stop-width sensitivity test has not started.","processed":0,"total":169,"session":None,"updated_at":None}
entrycompare_thread = None
entrycompare_state = {"status":"IDLE","message":"Entry comparison (ready vs confirmed) has not started.","processed":0,"total":0,"session":None,"updated_at":None}
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


def diagnostic_loop():
    global diagnostic_thread
    try:
        engine=BacktestCollector()
        def progress(processed,total,session):
            payload={"status":"RUNNING","message":"Diagnosing winners, failed entries and post-stop recoveries","processed":processed,"total":total,"session":session,"updated_at":stamp()};engine.redis.set_json(engine.key("diagnostic:93:35:status"),payload)
            with lock:diagnostic_state.update(payload)
        result=engine.run_stop_diagnostic(progress);payload={"status":"COMPLETED","message":"Stop diagnostic completed","processed":len(result["cases"]),"total":len(result["cases"]),"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("diagnostic:93:35:status"),payload)
        with lock:diagnostic_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("diagnostic:93:35:status"),{**diagnostic_state,**payload})
        except Exception:pass
        with lock:diagnostic_state.update(payload)
    finally:diagnostic_thread=None


def explosions_loop():
    global explosions_thread
    try:
        engine=BacktestCollector()
        def progress(rows):
            payload={"status":"RUNNING","message":"Cataloging stored READY and ENTRY explosions","rows_scanned":rows,"updated_at":stamp()};engine.redis.set_json(engine.key("explosions:status"),payload)
            with lock:explosions_state.update(payload)
        result=engine.build_explosion_catalog(progress);payload={"status":"COMPLETED","message":"Explosion catalog completed","rows_scanned":result["source_rows_scanned"],"catalog_cases":len(result["cases"]),"updated_at":stamp()};engine.redis.set_json(engine.key("explosions:status"),payload)
        with lock:explosions_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("explosions:status"),{**explosions_state,**payload})
        except Exception:pass
        with lock:explosions_state.update(payload)
    finally:explosions_thread=None


def big_moves_loop():
    global big_moves_thread
    try:
        engine=BacktestCollector()
        def progress(rows):
            payload={"status":"RUNNING","message":"Reviewing stored +20% and +50% READY cases","rows_scanned":rows,"updated_at":stamp()};engine.redis.set_json(engine.key("big_moves:status"),payload)
            with lock:big_moves_state.update(payload)
        result=engine.build_big_move_review(progress);payload={"status":"COMPLETED","message":"Big-move review completed","rows_scanned":result["source_rows_scanned"],"cases":len(result["cases"]),"updated_at":stamp()};engine.redis.set_json(engine.key("big_moves:status"),payload)
        with lock:big_moves_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("big_moves:status"),{**big_moves_state,**payload})
        except Exception:pass
        with lock:big_moves_state.update(payload)
    finally:big_moves_thread=None


def stopwidth_loop():
    global stopwidth_thread
    try:
        engine=BacktestCollector()
        def progress(processed,total,session):
            payload={"status":"RUNNING","message":"Running stop-width sensitivity simulation","processed":processed,"total":total,"session":session,"updated_at":stamp()};engine.redis.set_json(engine.key("stopwidth:93:35:status"),payload)
            with lock:stopwidth_state.update(payload)
        result=engine.stop_width_report(progress=progress);payload={"status":"COMPLETED","message":"Stop-width sensitivity test completed","processed":169,"total":169,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("stopwidth:93:35:status"),payload)
        with lock:stopwidth_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("stopwidth:93:35:status"),{**stopwidth_state,**payload})
        except Exception:pass
        with lock:stopwidth_state.update(payload)
    finally:stopwidth_thread=None


def entrycompare_loop():
    global entrycompare_thread
    try:
        engine=BacktestCollector()
        def progress(processed,total,session):
            payload={"status":"RUNNING","message":"Comparing BREAKOUT_READY vs CONFIRMED_ENTRY","processed":processed,"total":total,"session":session,"updated_at":stamp()};engine.redis.set_json(engine.key("entrycompare:93:35:status"),payload)
            with lock:entrycompare_state.update(payload)
        result=engine.entry_compare_report(progress=progress);total=sum(result["candidate_counts"].values());payload={"status":"COMPLETED","message":"Entry comparison completed","processed":total,"total":total,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("entrycompare:93:35:status"),payload)
        with lock:entrycompare_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("entrycompare:93:35:status"),{**entrycompare_state,**payload})
        except Exception:pass
        with lock:entrycompare_state.update(payload)
    finally:entrycompare_thread=None


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
        "diagnostic_url": "/diagnostic/result",
        "explosions_url": "/explosions/result",
        "big_moves_url": "/big-moves/result",
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


@app.get("/diagnostic/status")
def diagnostic_status():
    engine=BacktestCollector();stored=engine.stop_diagnostic_status()
    with lock:payload=dict(stored or diagnostic_state)
    payload["result_ready"]=engine.stop_diagnostic_result() is not None;payload["result_url"]="/diagnostic/result";return jsonify(payload)


@app.get("/diagnostic/result")
def diagnostic_result():
    result=BacktestCollector().stop_diagnostic_result();return (jsonify(result),200) if result else (jsonify({"ready":False,"status_url":"/diagnostic/status"}),202)


@app.get("/explosions/status")
def explosions_status():
    engine=BacktestCollector();stored=engine.explosion_catalog_status()
    with lock:payload=dict(stored or explosions_state)
    payload["result_ready"]=engine.explosion_catalog() is not None;payload["result_url"]="/explosions/result";return jsonify(payload)


@app.get("/explosions/download")
def explosions_download():
    result=BacktestCollector().explosion_catalog()
    if not result:return jsonify({"ready":False,"status_url":"/explosions/status"}),202
    body=json.dumps(result,ensure_ascii=False)
    return Response(body,mimetype="application/json",headers={"Content-Disposition":"attachment; filename=explosions_full.json"})


@app.get("/explosions/result")
def explosions_result():
    result=BacktestCollector().explosion_catalog()
    if not result:return jsonify({"ready":False,"status_url":"/explosions/status"}),202
    try:offset=max(0,int(request.args.get("offset","0")));limit=max(1,min(200,int(request.args.get("limit","100"))));min_mfe=max(5,float(request.args.get("min_mfe","5")))
    except ValueError:return jsonify({"ok":False,"error":"invalid_filter"}),400
    signal_type=request.args.get("signal_type");partition=request.args.get("partition");phase=request.args.get("phase");symbol=request.args.get("symbol","").upper()
    if signal_type and signal_type not in {"breakout_ready","confirmed_entry"}:return jsonify({"ok":False,"error":"invalid_signal_type"}),400
    if partition and partition not in {"development","holdout"}:return jsonify({"ok":False,"error":"invalid_partition"}),400
    if phase and phase not in {"AFTER_HOURS","OVERNIGHT","PREMARKET","REGULAR"}:return jsonify({"ok":False,"error":"invalid_phase"}),400
    if symbol and not re.fullmatch(r"[A-Z]{1,5}",symbol):return jsonify({"ok":False,"error":"invalid_symbol"}),400
    filtered=[x for x in result["cases"] if float(x["mfe_pct"])>=min_mfe and (not signal_type or x["signal_type"]==signal_type) and (not partition or x["partition"]==partition) and (not phase or x.get("phase")==phase) and (not symbol or x["symbol"]==symbol)];page=filtered[offset:offset+limit];next_offset=offset+len(page);params={"offset":next_offset,"limit":limit,"min_mfe":min_mfe}
    if signal_type:params["signal_type"]=signal_type
    if partition:params["partition"]=partition
    if phase:params["phase"]=phase
    if symbol:params["symbol"]=symbol
    return jsonify({"schema":result["schema"],"generated_at":result["generated_at"],"summary":result["summary"],"filters":{"signal_type":signal_type,"partition":partition,"phase":phase,"symbol":symbol or None,"min_mfe":min_mfe},"offset":offset,"limit":limit,"total_filtered":len(filtered),"cases":page,"next_url":"/explosions/result?"+urlencode(params) if next_offset<len(filtered) else None,"split_warning":"This raw catalog uses unadjusted source outcomes. Use the split-adjusted /big-moves/result for validated +20%/+50% cases."})


@app.get("/big-moves/status")
def big_moves_status():
    engine=BacktestCollector();stored=engine.big_move_status()
    with lock:payload=dict(stored or big_moves_state)
    payload["result_ready"]=engine.big_move_review() is not None;payload["result_url"]="/big-moves/result";return jsonify(payload)


@app.get("/big-moves/result")
def big_moves_result():
    result=BacktestCollector().big_move_review();return (jsonify(result),200) if result else (jsonify({"ready":False,"status_url":"/big-moves/status"}),202)


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
    <form method="post" action="/diagnostic/start"><input name="token" type="password" placeholder="Admin token" required><button>Diagnose stops and missed recoveries</button></form>
    <form method="post" action="/explosions/start"><input name="token" type="password" placeholder="Admin token" required><button>Build explosion catalog</button></form>
    <form method="post" action="/big-moves/start"><input name="token" type="password" placeholder="Admin token" required><button>Review +20% and +50% moves</button></form>
    <form method="post" action="/stop-width/start"><input name="token" type="password" placeholder="Admin token" required><button>Run stop-width sensitivity test</button></form>
    <form method="post" action="/entry-compare/start"><input name="token" type="password" placeholder="Admin token" required><button>Compare READY vs CONFIRMED entry</button></form>
    <p><a style="color:#a78bfa" href="/status">View status</a> · <a style="color:#a78bfa" href="/report">View report</a> · <a style="color:#a78bfa" href="/analysis/status">Analysis status</a> · <a style="color:#a78bfa" href="/simulation/status">Simulation status</a> · <a style="color:#a78bfa" href="/diagnostic/status">Diagnostic status</a> · <a style="color:#a78bfa" href="/explosions/status">Explosion catalog</a> · <a style="color:#a78bfa" href="/big-moves/status">Big moves</a> · <a style="color:#a78bfa" href="/stop-width/status">Stop-width test</a> · <a style="color:#a78bfa" href="/entry-compare/status">Entry compare</a> · <a style="color:#a78bfa" href="/explosions/download">Download full explosions JSON</a></p></body></html>
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


@app.post("/diagnostic/start")
def start_diagnostic():
    global diagnostic_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    if engine.trade_simulation_result() is None:return jsonify({"ok":False,"error":"trade_simulation_not_completed"}),409
    with lock:
        if engine.stop_diagnostic_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/diagnostic/result"})
        if diagnostic_thread and diagnostic_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/diagnostic/status"})
        payload={"status":"RUNNING","message":"Preparing stop and recovery diagnosis","processed":0,"total":169,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("diagnostic:93:35:status"),payload);diagnostic_state.update(payload);diagnostic_thread=threading.Thread(target=diagnostic_loop,name="ndr-stop-diagnostic",daemon=True);diagnostic_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/diagnostic/status","result_url":"/diagnostic/result"})


@app.post("/explosions/start")
def start_explosions():
    global explosions_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    with lock:
        if engine.explosion_catalog() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/explosions/result"})
        if explosions_thread and explosions_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/explosions/status"})
        payload={"status":"RUNNING","message":"Starting catalog from stored results only","rows_scanned":0,"updated_at":stamp()};engine.redis.set_json(engine.key("explosions:status"),payload);explosions_state.update(payload);explosions_thread=threading.Thread(target=explosions_loop,name="ndr-explosion-catalog",daemon=True);explosions_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/explosions/status","result_url":"/explosions/result"})


@app.post("/big-moves/start")
def start_big_moves():
    global big_moves_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    if engine.explosion_catalog() is None:return jsonify({"ok":False,"error":"explosion_catalog_not_completed"}),409
    with lock:
        if engine.big_move_review() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/big-moves/result"})
        if big_moves_thread and big_moves_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/big-moves/status"})
        payload={"status":"RUNNING","message":"Starting focused +20% and +50% review","rows_scanned":0,"updated_at":stamp()};engine.redis.set_json(engine.key("big_moves:status"),payload);big_moves_state.update(payload);big_moves_thread=threading.Thread(target=big_moves_loop,name="ndr-big-moves",daemon=True);big_moves_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/big-moves/status","result_url":"/big-moves/result"})


@app.get("/stop-width/status")
def stopwidth_status():
    engine=BacktestCollector();stored=engine.stop_width_status()
    with lock:payload=dict(stored or stopwidth_state)
    payload["result_ready"]=engine.stop_width_result() is not None;payload["result_url"]="/stop-width/result";return jsonify(payload)


@app.get("/stop-width/result")
def stopwidth_result():
    result=BacktestCollector().stop_width_result()
    if not result:return jsonify({"ready":False,"status_url":"/stop-width/status"}),202
    return jsonify(result)


@app.post("/stop-width/start")
def start_stopwidth():
    global stopwidth_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    if engine.trade_simulation_result() is None:return jsonify({"ok":False,"error":"trade_simulation_not_completed"}),409
    with lock:
        if engine.stop_width_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/stop-width/result"})
        if stopwidth_thread and stopwidth_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/stop-width/status"})
        payload={"status":"RUNNING","message":"Starting stop-width sensitivity test","processed":0,"total":169,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("stopwidth:93:35:status"),payload);stopwidth_state.update(payload);stopwidth_thread=threading.Thread(target=stopwidth_loop,name="ndr-stopwidth-test",daemon=True);stopwidth_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/stop-width/status","result_url":"/stop-width/result"})


@app.get("/entry-compare/status")
def entrycompare_status():
    engine=BacktestCollector();stored=engine.entry_compare_status()
    with lock:payload=dict(stored or entrycompare_state)
    payload["result_ready"]=engine.entry_compare_result() is not None;payload["result_url"]="/entry-compare/result";return jsonify(payload)


@app.get("/entry-compare/result")
def entrycompare_result():
    result=BacktestCollector().entry_compare_result()
    if not result:return jsonify({"ready":False,"status_url":"/entry-compare/status"}),202
    return jsonify(result)


@app.post("/entry-compare/start")
def start_entrycompare():
    global entrycompare_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    with lock:
        if engine.entry_compare_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/entry-compare/result"})
        if entrycompare_thread and entrycompare_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/entry-compare/status"})
        payload={"status":"RUNNING","message":"Starting entry comparison (ready vs confirmed)","processed":0,"total":0,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("entrycompare:93:35:status"),payload);entrycompare_state.update(payload);entrycompare_thread=threading.Thread(target=entrycompare_loop,name="ndr-entry-compare",daemon=True);entrycompare_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/entry-compare/status","result_url":"/entry-compare/result"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
