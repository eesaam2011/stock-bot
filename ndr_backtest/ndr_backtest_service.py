from __future__ import annotations

import json
import os
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
stop_event = threading.Event()
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


@app.get("/control")
def control():
    return """
    <html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{font-family:Arial;background:#111;color:#eee;padding:24px}input,button{font-size:18px;padding:12px;margin:6px 0;width:100%;max-width:520px}button{background:#6d28d9;color:#fff;border:0;border-radius:8px}</style></head>
    <body><h2>NDR Backtest Control</h2><p>Paste the current admin token. It is sent in the form body, not the URL.</p>
    <form method="post" action="/start"><input name="token" type="password" placeholder="Admin token" required><button>Start / Resume backtest</button></form>
    <form method="post" action="/pause"><input name="token" type="password" placeholder="Admin token" required><button>Pause</button></form>
    <p><a style="color:#a78bfa" href="/status">View status</a> · <a style="color:#a78bfa" href="/report">View report</a></p></body></html>
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
