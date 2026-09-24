"""Opt-in, bounded EHR G5 pilot endpoints for existing backtest Flask service.

Not enabled unless EHR_G5_ENABLED=1. No Redis writes, no trades, no
automatic scheduling, no work during market hours. One 3-symbol batch maximum
per explicit authenticated start. In-memory status is per web process.
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

_NY = ZoneInfo("America/New_York")
_lock = threading.Lock()
_state = {"status": "DISABLED", "completed": 0, "total": 0,
          "returns_computed": False, "backtest_authorized": False}
_thread = None

def after_close(now=None):
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(_NY)
    return local.weekday() >= 5 or (local.hour, local.minute) >= (17, 30)

def register(app, authorized):
    from flask import jsonify, request
    @app.get("/ehr-g5")
    def ehr_g5_page():
        from flask import send_file
        return send_file(Path(__file__).with_name("ehr_g5_pilot_page.html"), mimetype="text/html")

    @app.get("/ehr-g5/status")
    def ehr_g5_status():
        if not authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        with _lock:
            return jsonify(dict(_state, enabled=os.getenv("EHR_G5_ENABLED") == "1",
                                after_close=after_close()))


    @app.post("/ehr-g5/requirements")
    def ehr_g5_requirements():
        if not authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if os.getenv("EHR_G5_ENABLED") != "1":
            return jsonify({"ok": False, "error": "disabled"}), 403
        if not after_close():
            return jsonify({"ok": False, "error": "outside_after_close_window"}), 409
        raw = request.get_data(cache=False)
        if not raw or len(raw) > 1024 * 1024:
            return jsonify({"ok": False, "error": "invalid_upload_size"}), 413
        try:
            from ehr_g5_preflight import verify
            data = json.loads(raw)
            folder = Path(os.getenv("EHR_G5_OUTPUT_DIR", "/tmp/ehr_g5_pilot"))
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "private_requirements.json"
            if len(data.get("requirements", [])) != 583:
                raise ValueError("wrong_cohort")
            # Do not leave a partial/invalid cohort file behind.
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", dir=folder, prefix=".ehr_g5_", suffix=".json", delete=False) as tmp:
                json.dump(data, tmp, separators=(",", ":"))
                candidate = Path(tmp.name)
            try:
                manifest = verify(candidate)
                with _lock:
                    if _thread and _thread.is_alive():
                        candidate.unlink(missing_ok=True)
                        return jsonify({"ok": False, "error": "pilot_running"}), 409
                    os.replace(candidate, path)
            finally:
                candidate.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return jsonify({"ok": False, "error": "invalid_requirements",
                            "error_type": type(exc).__name__}), 422
        return jsonify({"ok": True, "cases": manifest["cases"],
                        "symbols": manifest["symbols"],
                        "requirements_sha256": manifest["file_sha256"],
                        "ephemeral_storage": True})

    @app.get("/ehr-g5/download")
    def ehr_g5_download():
        if not authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from flask import send_file
        import io
        import zipfile
        with _lock:
            if _state.get("status") not in ("COMPLETED", "PARTIAL"):
                return jsonify({"ok": False, "error": "pilot_not_finished"}), 409
        folder = Path(os.getenv("EHR_G5_OUTPUT_DIR", "/tmp/ehr_g5_pilot"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for path in sorted(folder.glob("*.json")):
                if path.name != "private_requirements.json" and path.stat().st_size < 2000000:
                    z.write(path, path.name)
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                         download_name="ehr_g5_pilot_sip.zip")

    @app.post("/ehr-g5/start")
    def ehr_g5_start():
        global _thread
        if not authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if os.getenv("EHR_G5_ENABLED") != "1":
            return jsonify({"ok": False, "error": "disabled"}), 403
        if not after_close():
            return jsonify({"ok": False, "error": "outside_after_close_window"}), 409
        # Protect the existing web service: a single bounded three-symbol pilot.
        # A full 376-symbol run is deliberately not exposed until durable storage,
        # distributed locking and resource measurements have been verified.
        path = os.getenv("EHR_G5_REQUIREMENTS_PATH") or str(Path(os.getenv("EHR_G5_OUTPUT_DIR", "/tmp/ehr_g5_pilot")) / "private_requirements.json")
        out = os.getenv("EHR_G5_OUTPUT_DIR", "/tmp/ehr_g5_pilot")
        if not path or not out or not os.path.isabs(out):
            return jsonify({"ok": False, "error": "private_requirements_or_output_not_configured"}), 503
        if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
            return jsonify({"ok": False, "error": "alpaca_credentials_not_configured"}), 503
        try:
            from ehr_g5_preflight import verify
            manifest = verify(Path(path))
            req = json.loads(Path(path).read_text())["requirements"]
            symbols = sorted({r["symbol"] for r in req})[:3]
            start = min(r["next_regular_entry_session"] for r in req)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return jsonify({"ok": False, "error": "requirements_preflight_failed",
                            "error_type": type(exc).__name__}), 503
        with _lock:
            if _thread and _thread.is_alive():
                return jsonify({"ok": False, "error": "already_running"}), 409
            if _state.get("status") == "COMPLETED":
                return jsonify({"ok": False, "error": "pilot_already_completed"}), 409
            _state.update(status="RUNNING", completed=0, total=len(symbols),
                          requirements_sha256=manifest["file_sha256"], failures=[])
            _thread = threading.Thread(target=_pilot, args=(symbols, start, out),
                                       name="ehr-g5-three-symbol-pilot", daemon=True)
            _thread.start()
        return jsonify({"ok": True, "status": "RUNNING", "total": len(symbols),
                        "status_url": "/ehr-g5/status"}), 202

def _pilot(symbols, start, output):
    # Collector reads APCA_* names. Adapt only in this thread; no global env mutation.
    # Direct urllib request uses the service's existing ALPACA_* secrets.
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
    def fetch(symbol, first, last, token):
        params = {"symbols": symbol, "timeframe": "1Day",
                  "start": first + "T00:00:00Z", "end": last + "T23:59:59Z",
                  "feed": "sip", "adjustment": "raw", "sort": "asc", "limit": 1000}
        if token:
            params["page_token"] = token
        headers = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
                   "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
        with urlopen(Request("https://data.alpaca.markets/v2/stocks/bars?" +
                             urlencode(params), headers=headers), timeout=25) as response:
            return json.load(response)
    folder = Path(output)
    failures = []
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # Fixed historical cutoff for pilot; no current-day bars.
        end = "2026-09-22"
        for symbol in symbols:
            if not after_close():
                raise RuntimeError("market_window_closed")
            name = hashlib.sha256(symbol.encode()).hexdigest() + ".json"
            target = folder / name
            try:
                bars = collect(symbol, start, end, fetch)
                atomic_json(target, {"symbol": symbol, "start": start, "end": end,
                                     "feed": "sip", "adjustment": "raw", "bars": bars})
            except Exception as exc:
                failures.append({"symbol": symbol, "error_type": type(exc).__name__})
            with _lock:
                _state["completed"] += 1
                _state["failures"] = list(failures)
            time.sleep(1)
        with _lock:
            _state["status"] = "COMPLETED" if not failures else "PARTIAL"
    except Exception as exc:
        with _lock:
            _state.update(status="FAILED", error_type=type(exc).__name__)
