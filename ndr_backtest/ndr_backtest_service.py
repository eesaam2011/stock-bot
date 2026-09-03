from __future__ import annotations

import gzip
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
from market_radar_backtest_engine import MarketRadarBacktest
from evidence_first_engine import EvidenceFirstEngine
from redis_audit_service import build_audit as build_redis_audit

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
temporal_hypotheses_thread = None
temporal_hypotheses_state = {"status":"IDLE","message":"H1/H2 confirmatory test has not started.","processed":0,"total":356,"session":None,"updated_at":None}
pcprofit_thread = None
pcprofit_state = {"status":"IDLE","message":"Price-change profitability simulation has not started.","processed":0,"total":0,"session":None,"updated_at":None}
er45profit_thread = None
er45profit_state = {"status":"IDLE","message":"ER45 profitability simulation has not started.","processed":0,"total":0,"session":None,"updated_at":None}
entrycompare_thread = None
entrycompare_state = {"status":"IDLE","message":"Entry comparison (ready vs confirmed) has not started.","processed":0,"total":0,"session":None,"updated_at":None}
weekday_thread = None
weekday_state = {"status":"IDLE","message":"All-signal weekday analysis has not started.","rows_scanned":0,"session":None,"updated_at":None}
market_radar_thread = None
market_radar_stop = threading.Event()
market_radar_diagnostic_thread = None
market_radar_diagnostic_state = {"status":"IDLE","message":"Stored Market Radar diagnostic has not started.","rows_scanned":0,"updated_at":None}
market_radar_ablation_thread = None
market_radar_ablation_state = {"status":"IDLE","message":"Market Radar scoring-layer ablation has not started.","rows_scanned":0,"updated_at":None}
evidence_first_thread = None
evidence_first_state = {
    "status": "IDLE",
    "message": "Frozen development-only ORB/retest research has not started.",
    "processed": 0,
    "total": 0,
    "session": None,
    "updated_at": None,
}
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


def temporal_hypotheses_loop():
    global temporal_hypotheses_thread
    try:
        engine=BacktestCollector()
        def progress(processed,total,session):
            payload={"status":"RUNNING","message":"Fetching bars and computing H1/H2 features","processed":processed,"total":total,"session":session,"updated_at":stamp()};engine.redis.set_json(engine.key("temporal_hypotheses:status"),payload)
            with lock:temporal_hypotheses_state.update(payload)
        engine.temporal_hypotheses_report(progress=progress);payload={"status":"COMPLETED","message":"H1/H2 confirmatory test completed","processed":356,"total":356,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("temporal_hypotheses:status"),payload)
        with lock:temporal_hypotheses_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("temporal_hypotheses:status"),{**temporal_hypotheses_state,**payload})
        except Exception:pass
        with lock:temporal_hypotheses_state.update(payload)
    finally:temporal_hypotheses_thread=None


def pcprofit_loop():
    global pcprofit_thread
    try:
        engine=BacktestCollector()
        total_candidates=len(engine.price_change_profitability_candidates())
        def progress(processed,total,session):
            payload={"status":"RUNNING","message":"Fetching bars and simulating trades for all BREAKOUT_READY REGULAR cases","processed":processed,"total":total,"session":session,"updated_at":stamp()};engine.redis.set_json(engine.key("pcprofit:v2:status"),payload)
            with lock:pcprofit_state.update(payload)
        engine.price_change_profitability_report(progress=progress);payload={"status":"COMPLETED","message":"Price-change profitability simulation completed","processed":total_candidates,"total":total_candidates,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("pcprofit:v2:status"),payload)
        with lock:pcprofit_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("pcprofit:v2:status"),{**pcprofit_state,**payload})
        except Exception:pass
        with lock:pcprofit_state.update(payload)
    finally:pcprofit_thread=None


def er45profit_loop():
    global er45profit_thread
    try:
        engine=BacktestCollector()
        total_candidates=len(engine.price_change_profitability_candidates())
        def progress(processed,total,session):
            payload={"status":"RUNNING","message":"Fetching bars and simulating trades for the ER45 filter (same BREAKOUT_READY REGULAR universe)","processed":processed,"total":total,"session":session,"updated_at":stamp()};engine.redis.set_json(engine.key("pcprofit_er45:v1:status"),payload)
            with lock:er45profit_state.update(payload)
        engine.er45_profitability_report(progress=progress);payload={"status":"COMPLETED","message":"ER45 profitability simulation completed","processed":total_candidates,"total":total_candidates,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("pcprofit_er45:v1:status"),payload)
        with lock:er45profit_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("pcprofit_er45:v1:status"),{**er45profit_state,**payload})
        except Exception:pass
        with lock:er45profit_state.update(payload)
    finally:er45profit_thread=None


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

def weekday_loop():
    global weekday_thread
    engine=BacktestCollector()
    try:
        def progress(rows,session):
            payload={"status":"RUNNING","message":"Analyzing weekdays across all stored approx signals","rows_scanned":rows,"session":session,"updated_at":stamp()};engine.redis.set_json(engine.key("weekday:signals:status"),payload)
            with lock:weekday_state.update(payload)
        result=engine.weekday_signal_report(progress);payload={"status":"COMPLETED","message":"All-signal weekday analysis completed","rows_scanned":result["source_rows_scanned"],"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("weekday:signals:status"),payload)
        with lock:weekday_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("weekday:signals:status"),{**weekday_state,**payload})
        except Exception:pass
        with lock:weekday_state.update(payload)
    finally:weekday_thread=None

def market_radar_loop():
    global market_radar_thread
    engine=MarketRadarBacktest()
    try:
        while not market_radar_stop.is_set():
            result=engine.step()
            if result.get("status")=="COMPLETED":break
        if market_radar_stop.is_set():engine.save_status(status="PAUSED",message="Market Radar backtest paused by user")
    except Exception as exc:
        engine.save_status(status="ERROR",phase="ERROR",message=f"{type(exc).__name__}: {exc}")
    finally:market_radar_thread=None

def market_radar_diagnostic_loop():
    global market_radar_diagnostic_thread
    engine=MarketRadarBacktest()
    try:
        def progress(rows):
            payload={"status":"RUNNING","message":"Diagnosing stored Market Radar signals","rows_scanned":rows,"updated_at":stamp()};engine.redis.set_json(engine.key("stored_diagnostic:status"),payload)
            with lock:market_radar_diagnostic_state.update(payload)
        result=engine.stored_diagnostic_report(progress);payload={"status":"COMPLETED","message":"Stored Market Radar diagnostic completed","rows_scanned":result["source_rows_scanned"],"updated_at":stamp()};engine.redis.set_json(engine.key("stored_diagnostic:status"),payload)
        with lock:market_radar_diagnostic_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("stored_diagnostic:status"),{**market_radar_diagnostic_state,**payload})
        except Exception:pass
        with lock:market_radar_diagnostic_state.update(payload)
    finally:market_radar_diagnostic_thread=None

def market_radar_ablation_loop():
    global market_radar_ablation_thread
    engine=MarketRadarBacktest()
    try:
        def progress(rows):
            payload={"status":"RUNNING","message":"Comparing simplified Market Radar scoring layers","rows_scanned":rows,"updated_at":stamp()};engine.redis.set_json(engine.key("stored_ablation:status"),payload)
            with lock:market_radar_ablation_state.update(payload)
        result=engine.stored_ablation_report(progress);payload={"status":"COMPLETED","message":"Market Radar scoring-layer ablation completed","rows_scanned":result["source_rows_scanned"],"updated_at":stamp()};engine.redis.set_json(engine.key("stored_ablation:status"),payload)
        with lock:market_radar_ablation_state.update(payload)
    except Exception as exc:
        payload={"status":"ERROR","message":f"{type(exc).__name__}: {exc}","updated_at":stamp()}
        try:engine.redis.set_json(engine.key("stored_ablation:status"),{**market_radar_ablation_state,**payload})
        except Exception:pass
        with lock:market_radar_ablation_state.update(payload)
    finally:market_radar_ablation_thread=None


def evidence_first_loop():
    global evidence_first_thread
    engine = EvidenceFirstEngine()
    try:
        def progress(processed, total, session):
            payload = {
                "status": "RUNNING",
                "message": "Running frozen development-only ORB/retest research",
                "processed": processed,
                "total": total,
                "session": session,
                "protocol_sha256": engine.protocol_sha256,
                "updated_at": stamp(),
            }
            engine.redis.set_json(engine.key("status"), payload)
            with lock:
                evidence_first_state.update(payload)

        result = engine.run_development(progress)
        payload = {
            "status": "COMPLETED",
            "message": "Frozen development-only ORB/retest research completed",
            "processed": result["symbols_processed"],
            "total": result["symbols_processed"],
            "session": None,
            "protocol_sha256": engine.protocol_sha256,
            "result_url": "/evidence-first/result",
            "live_approved": False,
            "updated_at": stamp(),
        }
        engine.redis.set_json(engine.key("status"), payload)
        with lock:
            evidence_first_state.update(payload)
    except Exception as exc:
        payload = {
            "status": "ERROR",
            "message": f"{type(exc).__name__}: {exc}",
            "updated_at": stamp(),
            "live_approved": False,
        }
        try:
            engine.redis.set_json(engine.key("status"), payload)
        except Exception:
            pass
        with lock:
            evidence_first_state.update(payload)
    finally:
        evidence_first_thread = None


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
        "weekday_url": "/weekday/result",
        "market_radar_backtest_url": "/market-radar/report",
        "evidence_first_protocol_url": "/evidence-first/protocol",
        "evidence_first_readiness_url": "/evidence-first/readiness",
        "evidence_first_status_url": "/evidence-first/status",
        "evidence_first_result_url": "/evidence-first/result",
        "redis_audit_summary_url": "/redis-audit/summary",
        "redis_audit_export_url": "/redis-audit/export",
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
    <form method="post" action="/temporal-hypotheses/start"><input name="token" type="password" placeholder="Admin token" required><button>Run H1/H2 confirmatory test (356 cases)</button></form>
    <form method="post" action="/price-change-profitability/start"><input name="token" type="password" placeholder="Admin token" required><button>Run price-change profitability simulation (ALL BREAKOUT_READY)</button></form>
    <form method="post" action="/er45-profitability/start"><input name="token" type="password" placeholder="Admin token" required><button>Run ER45 profitability simulation</button></form>
    <form method="post" action="/entry-compare/start"><input name="token" type="password" placeholder="Admin token" required><button>Compare READY vs CONFIRMED entry</button></form>
    <form method="post" action="/weekday/start"><input name="token" type="password" placeholder="Admin token" required><button>Analyze weekdays on all stored signals</button></form>
    <form method="post" action="/market-radar/start"><input name="token" type="password" placeholder="Admin token" required><button>Start / Resume Market Radar backtest</button></form>
    <form method="post" action="/market-radar/pause"><input name="token" type="password" placeholder="Admin token" required><button>Pause Market Radar backtest</button></form>
    <form method="post" action="/market-radar/diagnostic/start"><input name="token" type="password" placeholder="Admin token" required><button>Analyze stored Market Radar results</button></form>
    <form method="post" action="/market-radar/ablation/start"><input name="token" type="password" placeholder="Admin token" required><button>Compare simplified Market Radar layers</button></form>
    <hr><h3>Evidence-First Phase 1</h3>
    <p>Check readiness first. This run is Development-only and can never send alerts or orders.</p>
    <form method="post" action="/evidence-first/start"><input name="token" type="password" placeholder="Admin token" required><button>Start frozen ORB / Retest research</button></form>
    <p><a style="color:#a78bfa" href="/evidence-first/readiness">Evidence-First readiness</a> · <a style="color:#a78bfa" href="/evidence-first/protocol">Frozen protocol</a> · <a style="color:#a78bfa" href="/evidence-first/status">Evidence-First status</a> · <a style="color:#a78bfa" href="/evidence-first/result">Evidence-First result</a></p>
    <hr><h3>Redis Historical Audit — Read Only</h3>
    <p>Uses the current NDR admin token. It scans the Redis connections configured on this service and never changes or deletes data.</p>
    <form method="post" action="/redis-audit/summary"><input name="token" type="password" placeholder="Admin token" required><button>View Redis audit summary</button></form>
    <form method="post" action="/redis-audit/export"><input name="token" type="password" placeholder="Admin token" required><button>Download Redis historical data (JSON.GZ)</button></form>
    <p><a style="color:#a78bfa" href="/status">View status</a> · <a style="color:#a78bfa" href="/report">View report</a> · <a style="color:#a78bfa" href="/analysis/status">Analysis status</a> · <a style="color:#a78bfa" href="/simulation/status">Simulation status</a> · <a style="color:#a78bfa" href="/diagnostic/status">Diagnostic status</a> · <a style="color:#a78bfa" href="/explosions/status">Explosion catalog</a> · <a style="color:#a78bfa" href="/big-moves/status">Big moves</a> · <a style="color:#a78bfa" href="/stop-width/status">Stop-width test</a> · <a style="color:#a78bfa" href="/entry-compare/status">Entry compare</a> · <a style="color:#a78bfa" href="/weekday/status">Weekday analysis</a> · <a style="color:#a78bfa" href="/market-radar/status">Market Radar backtest</a> · <a style="color:#a78bfa" href="/explosions/download">Download full explosions JSON</a> · <a style="color:#a78bfa" href="/temporal-hypotheses/status">H1/H2 confirmatory test</a> · <a style="color:#a78bfa" href="/price-change-profitability/status">Price-change profitability</a> · <a style="color:#a78bfa" href="/er45-profitability/status">ER45 profitability</a></p></body></html>
    """


@app.post("/redis-audit/summary")
def redis_audit_summary():
    if not authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        return jsonify(build_redis_audit(include_data=False))
    except Exception as exc:
        return jsonify({
            "ok": False,
            "read_only": True,
            "error": f"{type(exc).__name__}: {exc}",
        }), 503


@app.post("/redis-audit/export")
def redis_audit_export():
    if not authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        report = build_redis_audit(include_data=True)
        payload = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = gzip.compress(payload, compresslevel=6)
        filename = f"redis_audit_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json.gz"
        return Response(
            compressed,
            mimetype="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return jsonify({
            "ok": False,
            "read_only": True,
            "error": f"{type(exc).__name__}: {exc}",
        }), 503


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


@app.get("/er45-profitability/status")
def er45profit_status():
    engine=BacktestCollector();stored=engine.er45_profitability_status()
    with lock:payload=dict(stored or er45profit_state)
    payload["result_ready"]=engine.er45_profitability_result() is not None;payload["result_url"]="/er45-profitability/result";return jsonify(payload)


@app.get("/er45-profitability/result")
def er45profit_result():
    result=BacktestCollector().er45_profitability_result()
    if not result:return jsonify({"ready":False,"status_url":"/er45-profitability/status"}),202
    return jsonify(result)


@app.post("/er45-profitability/start")
def start_er45profit():
    global er45profit_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    with lock:
        if engine.er45_profitability_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/er45-profitability/result"})
        if er45profit_thread and er45profit_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/er45-profitability/status"})
        payload={"status":"RUNNING","message":"Starting ER45 profitability simulation","processed":0,"total":0,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("pcprofit_er45:v1:status"),payload);er45profit_state.update(payload);er45profit_thread=threading.Thread(target=er45profit_loop,name="ndr-er45profit",daemon=True);er45profit_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/er45-profitability/status","result_url":"/er45-profitability/result"})


@app.get("/price-change-profitability/status")
def pcprofit_status():
    engine=BacktestCollector();stored=engine.price_change_profitability_status()
    with lock:payload=dict(stored or pcprofit_state)
    payload["result_ready"]=engine.price_change_profitability_result() is not None;payload["result_url"]="/price-change-profitability/result";return jsonify(payload)


@app.get("/price-change-profitability/result")
def pcprofit_result():
    result=BacktestCollector().price_change_profitability_result()
    if not result:return jsonify({"ready":False,"status_url":"/price-change-profitability/status"}),202
    return jsonify(result)


@app.post("/price-change-profitability/start")
def start_pcprofit():
    global pcprofit_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    with lock:
        if engine.price_change_profitability_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/price-change-profitability/result"})
        if pcprofit_thread and pcprofit_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/price-change-profitability/status"})
        payload={"status":"RUNNING","message":"Starting price-change profitability simulation","processed":0,"total":0,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("pcprofit:v2:status"),payload);pcprofit_state.update(payload);pcprofit_thread=threading.Thread(target=pcprofit_loop,name="ndr-pcprofit",daemon=True);pcprofit_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/price-change-profitability/status","result_url":"/price-change-profitability/result"})


@app.get("/temporal-hypotheses/status")
def temporal_hypotheses_status():
    engine=BacktestCollector();stored=engine.temporal_hypotheses_status()
    with lock:payload=dict(stored or temporal_hypotheses_state)
    payload["result_ready"]=engine.temporal_hypotheses_result() is not None;payload["result_url"]="/temporal-hypotheses/result";return jsonify(payload)


@app.get("/temporal-hypotheses/result")
def temporal_hypotheses_result():
    result=BacktestCollector().temporal_hypotheses_result()
    if not result:return jsonify({"ready":False,"status_url":"/temporal-hypotheses/status"}),202
    return jsonify(result)


@app.post("/temporal-hypotheses/start")
def start_temporal_hypotheses():
    global temporal_hypotheses_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    with lock:
        if engine.temporal_hypotheses_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/temporal-hypotheses/result"})
        if temporal_hypotheses_thread and temporal_hypotheses_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/temporal-hypotheses/status"})
        payload={"status":"RUNNING","message":"Starting H1/H2 confirmatory test (356 cases)","processed":0,"total":356,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("temporal_hypotheses:status"),payload);temporal_hypotheses_state.update(payload);temporal_hypotheses_thread=threading.Thread(target=temporal_hypotheses_loop,name="ndr-temporal-hypotheses",daemon=True);temporal_hypotheses_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/temporal-hypotheses/status","result_url":"/temporal-hypotheses/result"})


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


@app.get("/weekday/status")
def weekday_status():
    engine=BacktestCollector();stored=engine.weekday_signal_status()
    with lock:payload=dict(stored or weekday_state)
    payload["result_ready"]=engine.weekday_signal_result() is not None;payload["result_url"]="/weekday/result";return jsonify(payload)


@app.get("/weekday/result")
def weekday_result():
    result=BacktestCollector().weekday_signal_result();return (jsonify(result),200) if result else (jsonify({"ready":False,"status_url":"/weekday/status"}),202)


@app.post("/weekday/start")
def start_weekday():
    global weekday_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=BacktestCollector()
    with lock:
        if engine.weekday_signal_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/weekday/result"})
        if weekday_thread and weekday_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/weekday/status"})
        payload={"status":"RUNNING","message":"Starting all-signal weekday analysis","rows_scanned":0,"session":None,"updated_at":stamp()};engine.redis.set_json(engine.key("weekday:signals:status"),payload);weekday_state.update(payload);weekday_thread=threading.Thread(target=weekday_loop,name="ndr-weekday-analysis",daemon=True);weekday_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/weekday/status","result_url":"/weekday/result"})


@app.get("/market-radar/status")
def market_radar_status():
    return jsonify(MarketRadarBacktest().status())


@app.get("/market-radar/report")
def market_radar_report():
    result=MarketRadarBacktest().report();return (jsonify(result),200) if result else (jsonify({"ready":False,"status_url":"/market-radar/status"}),202)


@app.post("/market-radar/start")
def market_radar_start():
    global market_radar_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=MarketRadarBacktest()
    with lock:
        if engine.report() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/market-radar/report"})
        if market_radar_thread and market_radar_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/market-radar/status"})
        market_radar_stop.clear();engine.save_status(status="RUNNING",phase="REPLAYING",message="Starting or resuming Market Radar technical backtest");market_radar_thread=threading.Thread(target=market_radar_loop,name="market-radar-backtest",daemon=True);market_radar_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/market-radar/status","result_url":"/market-radar/report"})


@app.post("/market-radar/pause")
def market_radar_pause():
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    market_radar_stop.set();return jsonify({"ok":True,"status":"pause_requested","status_url":"/market-radar/status"})


@app.get("/market-radar/diagnostic/status")
def market_radar_diagnostic_status():
    engine=MarketRadarBacktest();stored=engine.stored_diagnostic_status()
    with lock:payload=dict(stored or market_radar_diagnostic_state)
    payload["result_ready"]=engine.stored_diagnostic_result() is not None;payload["result_url"]="/market-radar/diagnostic/result";return jsonify(payload)


@app.get("/market-radar/diagnostic/result")
def market_radar_diagnostic_result():
    result=MarketRadarBacktest().stored_diagnostic_result();return (jsonify(result),200) if result else (jsonify({"ready":False,"status_url":"/market-radar/diagnostic/status"}),202)


@app.post("/market-radar/diagnostic/start")
def market_radar_diagnostic_start():
    global market_radar_diagnostic_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=MarketRadarBacktest()
    if engine.report() is None:return jsonify({"ok":False,"error":"market_radar_backtest_not_completed"}),409
    with lock:
        if engine.stored_diagnostic_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/market-radar/diagnostic/result"})
        if market_radar_diagnostic_thread and market_radar_diagnostic_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/market-radar/diagnostic/status"})
        payload={"status":"RUNNING","message":"Starting stored Market Radar diagnostic","rows_scanned":0,"updated_at":stamp()};engine.redis.set_json(engine.key("stored_diagnostic:status"),payload);market_radar_diagnostic_state.update(payload);market_radar_diagnostic_thread=threading.Thread(target=market_radar_diagnostic_loop,name="market-radar-stored-diagnostic",daemon=True);market_radar_diagnostic_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/market-radar/diagnostic/status","result_url":"/market-radar/diagnostic/result"})


@app.get("/market-radar/ablation/status")
def market_radar_ablation_status():
    engine=MarketRadarBacktest();stored=engine.stored_ablation_status()
    with lock:payload=dict(stored or market_radar_ablation_state)
    payload["result_ready"]=engine.stored_ablation_result() is not None;payload["result_url"]="/market-radar/ablation/result";return jsonify(payload)


@app.get("/market-radar/ablation/result")
def market_radar_ablation_result():
    result=MarketRadarBacktest().stored_ablation_result();return (jsonify(result),200) if result else (jsonify({"ready":False,"status_url":"/market-radar/ablation/status"}),202)


@app.post("/market-radar/ablation/start")
def market_radar_ablation_start():
    global market_radar_ablation_thread
    if not authorized():return jsonify({"ok":False,"error":"unauthorized"}),401
    engine=MarketRadarBacktest()
    if engine.report() is None:return jsonify({"ok":False,"error":"market_radar_backtest_not_completed"}),409
    with lock:
        if engine.stored_ablation_result() is not None:return jsonify({"ok":True,"status":"already_completed","result_url":"/market-radar/ablation/result"})
        if market_radar_ablation_thread and market_radar_ablation_thread.is_alive():return jsonify({"ok":True,"status":"already_running","status_url":"/market-radar/ablation/status"})
        payload={"status":"RUNNING","message":"Starting Market Radar scoring-layer ablation","rows_scanned":0,"updated_at":stamp()};engine.redis.set_json(engine.key("stored_ablation:status"),payload);market_radar_ablation_state.update(payload);market_radar_ablation_thread=threading.Thread(target=market_radar_ablation_loop,name="market-radar-stored-ablation",daemon=True);market_radar_ablation_thread.start()
    return jsonify({"ok":True,"status":"started","status_url":"/market-radar/ablation/status","result_url":"/market-radar/ablation/result"})


@app.get("/evidence-first/protocol")
def evidence_first_protocol():
    return jsonify(EvidenceFirstEngine().protocol_record())


@app.get("/evidence-first/readiness")
def evidence_first_readiness():
    try:
        return jsonify(EvidenceFirstEngine().readiness())
    except Exception as exc:
        return jsonify({
            "ready_for_decisive_development_run": False,
            "error": f"{type(exc).__name__}: {exc}",
            "alerts_enabled": False,
            "orders_enabled": False,
        }), 503


@app.get("/evidence-first/status")
def evidence_first_status():
    engine = EvidenceFirstEngine()
    stored = engine.status()
    with lock:
        payload = dict(stored or evidence_first_state)
    payload["result_ready"] = engine.development_result() is not None
    payload["result_url"] = "/evidence-first/result"
    payload["protocol_url"] = "/evidence-first/protocol"
    payload["live_approved"] = False
    return jsonify(payload)


@app.get("/evidence-first/result")
def evidence_first_result():
    result = EvidenceFirstEngine().development_result()
    if result is None:
        return jsonify({
            "ready": False,
            "status_url": "/evidence-first/status",
            "protocol_url": "/evidence-first/protocol",
            "live_approved": False,
        }), 202
    return jsonify(result)


@app.post("/evidence-first/start")
def evidence_first_start():
    global evidence_first_thread
    if not authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if BacktestCollector().status().get("phase") != "COMPLETED":
        return jsonify({"ok": False, "error": "immutable_backtest_not_completed"}), 409
    engine = EvidenceFirstEngine()
    with lock:
        if engine.development_result() is not None:
            return jsonify({"ok": True, "status": "already_completed", "result_url": "/evidence-first/result"})
        if evidence_first_thread and evidence_first_thread.is_alive():
            return jsonify({"ok": True, "status": "already_running", "status_url": "/evidence-first/status"})
        payload = {
            "status": "RUNNING",
            "message": "Starting frozen development-only ORB/retest research",
            "processed": 0,
            "total": 0,
            "session": None,
            "protocol_sha256": engine.protocol_sha256,
            "updated_at": stamp(),
            "live_approved": False,
        }
        engine.lock_protocol()
        engine.redis.set_json(engine.key("status"), payload)
        evidence_first_state.update(payload)
        evidence_first_thread = threading.Thread(
            target=evidence_first_loop,
            name="evidence-first-development",
            daemon=True,
        )
        evidence_first_thread.start()
    return jsonify({
        "ok": True,
        "status": "started",
        "status_url": "/evidence-first/status",
        "result_url": "/evidence-first/result",
        "protocol_url": "/evidence-first/protocol",
        "live_approved": False,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
