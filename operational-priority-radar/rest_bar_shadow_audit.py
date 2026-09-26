from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from base_ready import phase2_features
from early_core_features import first_early_core_crossing


SCHEMA = "OPR_REST_BAR_SHADOW_AUDIT_V1"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(rows):
    return hashlib.sha256(_canonical(rows).encode()).hexdigest()


class RESTBarShadowAudit:
    """Persists bounded causal inputs and compares them with next-day REST."""

    def __init__(self, redis_client, rest, session, *, due_hours=18,
                 ttl_seconds=604800):
        if not 12 <= due_hours <= 48:
            raise ValueError("invalid shadow audit delay")
        self.r = redis_client; self.rest = rest; self.session = session
        self.due = timedelta(hours=due_hours); self.ttl = ttl_seconds

    def _key(self, lane, symbol, eval_ts):
        ident = hashlib.sha256(f"{lane}|{symbol}|{eval_ts}".encode()).hexdigest()
        return f"operational_priority_radar:v1.1:bar_audit:{self.session}:{ident}"

    def record(self, lane, symbol, rows, eval_ts, observed_at, decision):
        if lane not in {"BASE_1MIN", "EARLY_5MIN"} or not rows:
            raise ValueError("invalid audit observation")
        observed_at = observed_at.astimezone(timezone.utc)
        normalized = sorted(({k: row.get(k) for k in ("t","o","h","l","c","v","vw","n")}
                             for row in rows
                             if datetime.fromisoformat(str(row.get("t")).replace("Z","+00:00")) < eval_ts),
                            key=lambda row: str(row.get("t")))
        if not normalized:raise ValueError("empty causal audit observation")
        body = {"schema": SCHEMA, "session": self.session, "lane": lane,
                "symbol": symbol, "eval_ts": eval_ts.isoformat(),
                "observed_at": observed_at.isoformat(),
                "due_at": (observed_at + self.due).isoformat(),
                "start": normalized[0]["t"], "end": eval_ts.isoformat(),
                "rows": normalized, "observed_sha256": _digest(normalized),
                "observed_decision": bool(decision), "status": "PENDING"}
        key = self._key(lane, symbol, eval_ts.isoformat())
        self.r.setex(key, self.ttl, _canonical(body))
        return key

    def compare(self, key, now):
        raw = self.r.get(key)
        if not raw:
            return None
        body = json.loads(raw)
        now = now.astimezone(timezone.utc)
        if body["status"] != "PENDING" or now < datetime.fromisoformat(body["due_at"]):
            return body
        timeframe = "1Min" if body["lane"] == "BASE_1MIN" else "5Min"
        rows = self.rest.bars(body["symbol"],
                             datetime.fromisoformat(str(body["start"]).replace("Z", "+00:00")),
                             datetime.fromisoformat(body["end"]), timeframe)
        eval_ts = datetime.fromisoformat(body["eval_ts"])
        normalized = sorted(({k: row.get(k) for k in ("t","o","h","l","c","v","vw","n")}
                             for row in rows
                             if datetime.fromisoformat(str(row.get("t")).replace("Z","+00:00")) < eval_ts),
                            key=lambda row: str(row.get("t")))
        if body["lane"] == "BASE_1MIN":
            result = phase2_features(normalized, eval_ts)
            decision = bool(result and result[1]["base_ready"])
        else:
            decision = first_early_core_crossing(normalized) is not None
        body.update(status="COMPARED", compared_at=now.isoformat(),
                    compared_sha256=_digest(normalized),
                    bars_changed=_digest(normalized) != body["observed_sha256"],
                    compared_decision=decision,
                    decision_flipped=decision != body["observed_decision"])
        self.r.setex(key, self.ttl, _canonical(body))
        return body

    def compare_due(self, now, max_items=100):
        if type(max_items) is not int or not 1 <= max_items <= 1000:
            raise ValueError("invalid audit comparison limit")
        pattern=f"operational_priority_radar:v1.1:bar_audit:{self.session}:*"
        cursor=0;checked=compared=changed=flipped=0
        while checked < max_items:
            cursor,keys=self.r.scan(cursor=cursor,match=pattern,count=min(200,max_items-checked))
            for key in keys:
                if checked>=max_items:break
                checked+=1
                raw=self.r.get(key)
                if not raw:continue
                before=json.loads(raw)
                result=self.compare(key,now)
                if before.get("status")=="PENDING" and result.get("status")=="COMPARED":
                    compared+=1;changed+=int(bool(result.get("bars_changed")))
                    flipped+=int(bool(result.get("decision_flipped")))
            if int(cursor)==0:break
        return {"checked":checked,"compared":compared,"bars_changed":changed,
                "decision_flipped":flipped,"finality_proven":False}
