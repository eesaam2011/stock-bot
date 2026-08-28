from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request


app = Flask(__name__)
UTC = timezone.utc

STATE = {
    "status": "IDLE",
    "phase": "SETUP",
    "message": "بانتظار اختبار Historical BOATS",
    "boats_test": None,
    "updated_at": None,
}


def now_iso():
    return datetime.now(UTC).isoformat()


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }


def authorized():
    expected = os.getenv("NDR_BT_ADMIN_TOKEN", "")
    supplied = (
        request.headers.get("X-Admin-Token")
        or request.args.get("token")
        or ""
    )
    return bool(expected) and supplied == expected


def historical_boats_test():
    end = datetime.now(UTC).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
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

    url = (
        "https://data.alpaca.markets/v2/stocks/AAPL/bars?"
        + urlencode(params)
    )

    try:
        req = Request(url, headers=alpaca_headers())

        with urlopen(req, timeout=40) as response:
            payload = json.load(response)

        bars = payload.get("bars") or []

        return {
            "ok": len(bars) > 0,
            "http_ok": True,
            "bars_count": len(bars),
            "first_bar": bars[0].get("t") if bars else None,
            "last_bar": bars[-1].get("t") if bars else None,
            "message": (
                "Historical BOATS bars received"
                if bars
                else "HTTP succeeded but returned zero BOATS bars"
            ),
            "tested_at": now_iso(),
        }

    except HTTPError as exc:
        detail = exc.read(500).decode(
            "utf-8",
            errors="replace",
        )

        return {
            "ok": False,
            "http_ok": False,
            "http_status": exc.code,
            "message": detail,
            "tested_at": now_iso(),
        }

    except Exception as exc:
        return {
            "ok": False,
            "http_ok": False,
            "message": f"{type(exc).__name__}: {exc}",
            "tested_at": now_iso(),
        }


@app.get("/")
def home():
    return jsonify({
        "service": "Next-Day Radar Backtest",
        "source_version": os.getenv(
            "NDR_BT_SOURCE_VERSION",
            "unknown",
        ),
        "source_build": os.getenv(
            "NDR_BT_SOURCE_BUILD",
            "unknown",
        ),
        "status": STATE["status"],
        "phase": STATE["phase"],
        "health_url": "/health",
        "status_url": "/status",
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "ndr-backtest",
        "status": STATE["status"],
        "phase": STATE["phase"],
    })


@app.get("/status")
def status():
    return jsonify(STATE)


@app.route("/test/boats", methods=["GET", "POST"])
def test_boats():
    if not authorized():
        return jsonify({
            "ok": False,
            "error": "unauthorized",
        }), 401

    result = historical_boats_test()

    STATE["boats_test"] = result
    STATE["updated_at"] = now_iso()

    if result["ok"]:
        STATE["phase"] = "BOATS_VERIFIED"
        STATE["message"] = (
            "Historical BOATS verified successfully"
        )
    else:
        STATE["phase"] = "BOATS_BLOCKED"
        STATE["message"] = result["message"]

    return jsonify(result), (
        200 if result["ok"] else 422
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        threaded=True,
    )
