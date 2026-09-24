"""EHR G5 isolated read-only daily SIP collector. Never imported by the live service.

Run as a separate Render one-off job or background worker. Requires a mounted
requirements JSON and a persistent checkpoint directory. No Redis writes.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = "https://data.alpaca.markets/v2/stocks/bars"

def page(symbol, start, end, token=None, opener=urlopen):
    params = {"symbols": symbol, "timeframe": "1Day",
              "start": start + "T00:00:00Z", "end": end + "T23:59:59Z",
              "feed": "sip", "adjustment": "raw", "sort": "asc", "limit": 1000}
    if token:
        params["page_token"] = token
    headers = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
               "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}
    with opener(Request(API + "?" + urlencode(params), headers=headers), timeout=35) as r:
        return json.load(r)

def collect(symbol, start, end, fetch=page):
    result, dates, tokens, token = [], set(), set(), None
    while True:
        data = fetch(symbol, start, end, token)
        for b in data.get("bars", {}).get(symbol, []):
            day = b["t"][:10]
            if day in dates:
                raise ValueError("duplicate SIP daily bar")
            dates.add(day)
            result.append({"session": day, "open": b["o"], "close": b["c"],
                           "high": b["h"], "low": b["l"], "volume": b["v"],
                           "trade_count": b.get("n")})
        token = data.get("next_page_token")
        if not token:
            break
        if token in tokens:
            raise ValueError("pagination loop")
        tokens.add(token)
    return sorted(result, key=lambda b: b["session"])

def atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")))
    os.replace(tmp, path)

def run(requirements, output, end, max_symbols=0, pause=1.0):
    if not (os.getenv("APCA_API_KEY_ID") and os.getenv("APCA_API_SECRET_KEY")):
        raise RuntimeError("Alpaca credentials missing from environment")
    if not output.is_absolute():
        raise ValueError("checkpoint path must be absolute on persistent disk")
    if end > dt.date.today().isoformat():
        raise ValueError("future end date")
    req = json.loads(requirements.read_text())["requirements"]
    symbols = sorted({r["symbol"] for r in req})
    if max_symbols:
        symbols = symbols[:max_symbols]
    start = min(r["next_regular_entry_session"] for r in req)
    output.mkdir(parents=True, exist_ok=True)
    successes, failures = 0, []
    for symbol in symbols:
        # Never trust a symbol as a filesystem path.
        import hashlib
        path = output / (hashlib.sha256(symbol.encode()).hexdigest() + ".json")
        if path.exists():
            try:
                old = json.loads(path.read_text())
                if (old.get("symbol"), old.get("start"), old.get("end"),
                    old.get("feed")) == (symbol, start, end, "sip"):
                    successes += 1
                    continue
            except (ValueError, OSError):
                pass
        try:
            bars = collect(symbol, start, end)
            atomic_json(path, {"symbol": symbol, "start": start, "end": end,
                               "feed": "sip", "adjustment": "raw", "bars": bars})
            successes += 1
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as exc:
            failures.append({"symbol": symbol, "error_type": type(exc).__name__})
        atomic_json(output / "collection_manifest.json",
                    {"status": "COLLECTING" if successes + len(failures) < len(symbols) else "COLLECTION_COMPLETE_WITH_POSSIBLE_FAILURES",
                     "requested": len(symbols), "completed": successes, "failures": failures,
                     "end": end, "returns_computed": False,
                     "corporate_actions_verified": False, "backtest_authorized": False})
        time.sleep(max(0.0, pause))
    return len(failures) == 0

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--requirements", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--end", default="2026-09-22")
    p.add_argument("--max-symbols", type=int, default=3)
    p.add_argument("--pause", type=float, default=1.0)
    a = p.parse_args()
    raise SystemExit(0 if run(a.requirements, a.output, a.end, a.max_symbols, a.pause) else 2)
