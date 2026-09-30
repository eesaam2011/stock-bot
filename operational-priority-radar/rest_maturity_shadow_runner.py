"""Read-only OPR v1.2 REST maturity measurement runner.

This tool writes audit evidence only.  It never writes canonical E/B state,
never opens a websocket, and never sends an alert.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

from alpaca_production_market import AlpacaCredentials, AlpacaREST
from production_universe import build_operational_universe
from rest_bar_maturity import MaturedREST1MinCoordinator
from rest_bar_shadow_audit import RESTBarShadowAudit

UTC = timezone.utc
CHECKPOINTS = (30, 60, 90, 120, 180)


def _env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"MISSING_{name}")
    return value


def _write(path, body):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(body, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    os.replace(tmp, path)


def _sample(symbols, session, limit):
    return tuple(sorted(symbols,
        key=lambda x: hashlib.sha256(f"{session}|{x}".encode()).digest())[:limit])


def _redis_from_url(url):
    import redis
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)


def observe(args, now_fn=lambda: datetime.now(UTC), sleep_fn=time.sleep):
    creds = AlpacaCredentials(_env("ALPACA_API_KEY"), _env("ALPACA_SECRET_KEY"))
    rest = AlpacaREST(creds)
    r = _redis_from_url(_env("REDIS_URL"))
    if r.ping() is not True:
        raise RuntimeError("REDIS_PING_FAILED")
    symbols = _sample(build_operational_universe(rest), args.session, args.symbols)
    audit = RESTBarShadowAudit(r, rest, args.session, due_hours=args.due_hours)
    keys = set()

    def record(symbol, rows, eval_ts, checkpoint, observed_at):
        keys.add(audit.record_checkpoint("BASE_1MIN", symbol, rows, eval_ts,
                                         observed_at, checkpoint))

    coordinator = MaturedREST1MinCoordinator(
        rest, symbols, lambda *_: None, grace_seconds=90,
        study_observer=record, study_symbols=symbols,
        study_checkpoints=CHECKPOINTS)
    body = {"schema":"OPR_STEP3BI_REST_MATURITY_V1", "mode":"observe",
            "session":args.session, "symbols":list(symbols),
            "checkpoints":list(CHECKPOINTS), "started_at":now_fn().isoformat(),
            "audit_keys":[], "polls":0, "errors":[],
            "canonical_writes":0, "alerts_sent":0,
            "actionable_alerts_authorized":False, "finality_proven":False}
    _write(args.output, body)
    while now_fn() < args.end:
        now = now_fn()
        try:
            coordinator.poll(now, True)
            body["polls"] += 1
            body["audit_keys"] = sorted(keys)
            body["last_poll_at"] = now.isoformat()
            _write(args.output, body)
        except Exception as exc:
            body["errors"].append({"at":now.isoformat(),
                                   "type":type(exc).__name__,"message":str(exc)[:300]})
            _write(args.output, body)
            raise
        sleep_fn(args.interval)
    body["completed_at"] = now_fn().isoformat()
    _write(args.output, body)
    return body


def compare(args, now_fn=lambda: datetime.now(UTC)):
    creds = AlpacaCredentials(_env("ALPACA_API_KEY"), _env("ALPACA_SECRET_KEY"))
    rest = AlpacaREST(creds)
    r = _redis_from_url(_env("REDIS_URL"))
    if r.ping() is not True:
        raise RuntimeError("REDIS_PING_FAILED")
    audit = RESTBarShadowAudit(r, rest, args.session, due_hours=args.due_hours)
    aggregate = {"checked":0,"compared":0,"bars_changed":0,
                 "decision_flipped":0,"by_checkpoint":{}}
    while True:
        result = audit.compare_due(now_fn(), max_items=1000)
        for name in ("checked","compared","bars_changed","decision_flipped"):
            aggregate[name] += result[name]
        for checkpoint, values in result["by_checkpoint"].items():
            bucket=aggregate["by_checkpoint"].setdefault(
                checkpoint,{"compared":0,"bars_changed":0,"decision_flipped":0})
            for name in bucket: bucket[name] += values[name]
        if result["checked"] < 1000 or result["compared"] == 0:
            break
    body = {"schema":"OPR_STEP3BI_REST_MATURITY_V1", "mode":"compare",
            "session":args.session, "compared_at":now_fn().isoformat(), **aggregate,
            "recommended_grace_seconds":None, "finality_proven":False,
            "actionable_alerts_authorized":False}
    _write(args.output, body)
    return body


def parser():
    p=argparse.ArgumentParser()
    p.add_argument("mode", choices=("observe","compare"))
    p.add_argument("--session", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--symbols", type=int, default=16, choices=range(1,65))
    p.add_argument("--due-hours", type=int, default=18, choices=range(12,49))
    p.add_argument("--interval", type=int, default=15, choices=range(5,61))
    p.add_argument("--end", type=lambda x:datetime.fromisoformat(x.replace("Z","+00:00")))
    return p


def main():
    args=parser().parse_args()
    if args.mode == "observe":
        if args.end is None or args.end.tzinfo is None:
            raise SystemExit("--end with timezone is required for observe")
        result=observe(args)
    else:
        result=compare(args)
    print(json.dumps(result,sort_keys=True,separators=(",",":")),flush=True)


if __name__ == "__main__":
    main()
