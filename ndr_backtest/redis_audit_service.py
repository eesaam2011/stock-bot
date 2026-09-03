"""Temporary, read-only Redis inventory and export service.

Start with: gunicorn redis_audit_service:app

Required environment variables:
  AUDIT_ADMIN_TOKEN  Long random token used only by the audit page.
  REDIS_URL          Redis connection URL (or any env var ending in REDIS_URL).

The application deliberately contains no Redis write/delete commands.
"""

from __future__ import annotations

import gzip
import hashlib
import fnmatch
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, render_template_string, request
import redis


app = Flask(__name__)

MAX_RECORDS_PER_KEY = max(100, int(os.getenv("AUDIT_MAX_RECORDS_PER_KEY", "50000")))
MAX_STRING_BYTES = max(1024, int(os.getenv("AUDIT_MAX_STRING_BYTES", "5000000")))
SCAN_COUNT = max(100, int(os.getenv("AUDIT_SCAN_COUNT", "1000")))
SAMPLE_RECORDS = max(2, min(100, int(os.getenv("AUDIT_SAMPLE_RECORDS", "20"))))

SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[:_\-.])(password|passwd|secret|token|api[_-]?key|credential|auth)(?:$|[:_\-.])",
    re.IGNORECASE,
)
REDIS_ENV_RE = re.compile(r"(?:^REDIS_URL$|REDIS_URL$|^REDIS_URL_)", re.IGNORECASE)
DATE_FIELD_RE = re.compile(
    r"(?:time|timestamp|datetime|created|updated|opened|closed|entered|exited|date|session|alert)",
    re.IGNORECASE,
)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?$")

PAGE = """
<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>جرد Redis التاريخي</title>
<style>
body{font-family:system-ui;background:#101114;color:#eee;max-width:820px;margin:35px auto;padding:18px}
.box{background:#191b20;border:1px solid #343741;border-radius:14px;padding:20px;margin:14px 0}
input,button{font-size:17px;padding:13px;border-radius:9px;border:1px solid #555;width:100%;box-sizing:border-box;margin:7px 0}
button{background:#6d28d9;color:white;font-weight:700;cursor:pointer}.muted{color:#aaa}code{color:#93c5fd}
</style></head><body>
<h1>جرد Redis التاريخي — قراءة فقط</h1>
<div class="box"><p>يفحص المفاتيح والأعداد والفترات الزمنية دون تعديل أو حذف البيانات.</p>
<form method="post" action="/audit/summary"><input type="password" name="token" placeholder="AUDIT_ADMIN_TOKEN" required>
<button type="submit">عرض ملخص الجرد</button></form>
<form method="post" action="/audit/export"><input type="password" name="token" placeholder="AUDIT_ADMIN_TOKEN" required>
<button type="submit">تنزيل البيانات JSON.GZ</button></form></div>
<p class="muted">بعد تنزيل النتائج أوقف هذه الخدمة المؤقتة.</p>
</body></html>
"""


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _safe_json(value: Any) -> Any:
    if isinstance(value, bytes):
        value = _text(value)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _redacted_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key))


def _namespace(key: str) -> str:
    lower = key.lower()
    known = (
        "live_trade_manager", "market_radar", "elite_catalyst", "elite_explosion",
        "early_explosion", "hunter", "direct_entry", "next_day", "ndr",
        "master_list", "news_scanner", "elite",
    )
    for prefix in known:
        if lower.startswith(prefix):
            return prefix
    return re.split(r"[:._-]", lower, maxsplit=1)[0] or "other"


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        if 946684800 <= number <= 4102444800:
            try:
                return datetime.fromtimestamp(number, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    if isinstance(value, str) and ISO_RE.match(value.strip()):
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _timestamps(obj: Any, field: str = "") -> Iterable[datetime]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _timestamps(value, _text(key))
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _timestamps(value, field)
    elif DATE_FIELD_RE.search(field):
        parsed = _parse_timestamp(obj)
        if parsed:
            yield parsed


def _source_urls() -> dict[str, str]:
    sources: dict[str, str] = {}
    for name, value in os.environ.items():
        if value and REDIS_ENV_RE.search(name):
            sources[name] = value
    return dict(sorted(sources.items()))


def _source_specs() -> dict[str, dict[str, str]]:
    specs = {name: {"kind": "redis_url", "url": url} for name, url in _source_urls().items()}
    rest_url = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
    rest_token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    if rest_url and rest_token:
        specs["UPSTASH_REDIS_REST_URL"] = {
            "kind": "upstash_rest",
            "url": rest_url,
            "token": rest_token,
        }
    return dict(sorted(specs.items()))


def _authorized() -> bool:
    expected = os.getenv("AUDIT_ADMIN_TOKEN", "")
    supplied = request.headers.get("X-Admin-Token", "") or request.form.get("token", "")
    return bool(expected and supplied and hashlib.sha256(supplied.encode()).digest() == hashlib.sha256(expected.encode()).digest())


def _client(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, socket_connect_timeout=8, socket_timeout=20, health_check_interval=30)


class UpstashRESTClient:
    """Small read-only adapter for the Redis commands used by this audit."""

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def command(self, *parts: Any) -> Any:
        req = Request(
            self.url,
            data=json.dumps([_text(part) for part in parts]).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=90) as response:
            payload = json.load(response)
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        return payload.get("result")

    def ping(self): return self.command("PING")
    def scan(self, cursor=0, count=1000):
        result = self.command("SCAN", cursor, "COUNT", count) or [0, []]
        return int(result[0]), result[1] or []
    def type(self, key): return self.command("TYPE", key)
    def pttl(self, key): return int(self.command("PTTL", key))
    def strlen(self, key): return int(self.command("STRLEN", key))
    def getrange(self, key, start, end): return self.command("GETRANGE", key, start, end)
    def llen(self, key): return int(self.command("LLEN", key))
    def lrange(self, key, start, end): return self.command("LRANGE", key, start, end) or []
    def hlen(self, key): return int(self.command("HLEN", key))
    def scard(self, key): return int(self.command("SCARD", key))
    def zcard(self, key): return int(self.command("ZCARD", key))
    def xlen(self, key): return int(self.command("XLEN", key))

    def hscan(self, key, cursor=0, count=1000):
        result = self.command("HSCAN", key, cursor, "COUNT", count) or [0, []]
        flat = result[1] or []
        return int(result[0]), {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}

    def sscan(self, key, cursor=0, count=1000):
        result = self.command("SSCAN", key, cursor, "COUNT", count) or [0, []]
        return int(result[0]), result[1] or []

    def zrange(self, key, start, end, withscores=False):
        parts = ["ZRANGE", key, start, end]
        if withscores:
            parts.append("WITHSCORES")
        flat = self.command(*parts) or []
        if not withscores:
            return flat
        return [(flat[i], float(flat[i + 1])) for i in range(0, len(flat) - 1, 2)]

    def xrange(self, key, min="-", max="+", count=None):
        parts = ["XRANGE", key, min, max]
        if count is not None:
            parts.extend(["COUNT", count])
        result = self.command(*parts) or []
        rows = []
        for row in result:
            row_id, flat = row[0], row[1]
            rows.append((row_id, {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}))
        return rows


def _client_from_spec(spec: dict[str, str]):
    if spec["kind"] == "upstash_rest":
        return UpstashRESTClient(spec["url"], spec["token"])
    return _client(spec["url"])


def _key_size(r: redis.Redis, key: bytes, kind: str) -> int | None:
    if kind == "string":
        return r.strlen(key)
    if kind == "list":
        return r.llen(key)
    if kind == "hash":
        return r.hlen(key)
    if kind == "set":
        return r.scard(key)
    if kind == "zset":
        return r.zcard(key)
    if kind == "stream":
        return r.xlen(key)
    return None


def _scan_collection(r: redis.Redis, key: bytes, kind: str, limit: int) -> tuple[list[Any], bool]:
    """Read at most limit logical records, never writing to Redis."""
    if kind == "string":
        raw = r.getrange(key, 0, MAX_STRING_BYTES - 1)
        truncated = r.strlen(key) > len(raw)
        return [_safe_json(raw)], truncated
    if kind == "list":
        size = r.llen(key)
        return [_safe_json(v) for v in r.lrange(key, 0, limit - 1)], size > limit
    if kind == "hash":
        rows: list[Any] = []
        cursor = 0
        while True:
            cursor, values = r.hscan(key, cursor=cursor, count=min(SCAN_COUNT, limit - len(rows)))
            rows.extend({"field": _text(k), "value": _safe_json(v)} for k, v in values.items())
            if cursor == 0 or len(rows) >= limit:
                break
        return rows[:limit], r.hlen(key) > limit
    if kind == "set":
        rows = []
        cursor = 0
        while True:
            cursor, values = r.sscan(key, cursor=cursor, count=min(SCAN_COUNT, limit - len(rows)))
            rows.extend(_safe_json(v) for v in values)
            if cursor == 0 or len(rows) >= limit:
                break
        return rows[:limit], r.scard(key) > limit
    if kind == "zset":
        size = r.zcard(key)
        values = r.zrange(key, 0, limit - 1, withscores=True)
        return [{"value": _safe_json(v), "score": score} for v, score in values], size > limit
    if kind == "stream":
        values = r.xrange(key, min="-", max="+", count=limit)
        rows = [{"id": _text(row_id), "fields": {_text(k): _safe_json(v) for k, v in fields.items()}} for row_id, fields in values]
        return rows, r.xlen(key) > limit
    return [], False


def _sample_records(r: redis.Redis, key: bytes, kind: str, size: int | None) -> list[Any]:
    half = max(1, SAMPLE_RECORDS // 2)
    if kind == "list" and size:
        values = r.lrange(key, 0, half - 1) + r.lrange(key, max(0, size - half), -1)
        return [_safe_json(v) for v in values]
    if kind == "zset" and size:
        values = r.zrange(key, 0, half - 1, withscores=True) + r.zrange(key, max(0, size - half), -1, withscores=True)
        return [{"value": _safe_json(v), "score": score} for v, score in values]
    records, _ = _scan_collection(r, key, kind, SAMPLE_RECORDS)
    return records


def audit_source(
    source_name: str,
    spec: dict[str, str],
    include_data: bool,
    progress=None,
    key_patterns: list[str] | None = None,
) -> dict[str, Any]:
    r = _client_from_spec(spec)
    server = r.ping()
    items: list[dict[str, Any]] = []
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, count=SCAN_COUNT)
        for raw_key in keys:
            key = _text(raw_key)
            if key_patterns and not any(fnmatch.fnmatchcase(key, pattern) for pattern in key_patterns):
                continue
            kind = _text(r.type(raw_key))
            ttl_ms = r.pttl(raw_key)
            size = _key_size(r, raw_key, kind)
            item: dict[str, Any] = {
                "key": key,
                "namespace": _namespace(key),
                "type": kind,
                "records_or_bytes": size,
                "ttl_ms": ttl_ms,
                "persistent": ttl_ms == -1,
                "content_redacted": _redacted_key(key),
            }
            if not item["content_redacted"]:
                if include_data:
                    records, truncated = _scan_collection(r, raw_key, kind, MAX_RECORDS_PER_KEY)
                    item["records"] = records
                    item["truncated"] = truncated
                    time_source = records
                else:
                    time_source = _sample_records(r, raw_key, kind, size)
                found = sorted(set(t.astimezone(timezone.utc).isoformat() for t in _timestamps(time_source)))
                item["earliest_sampled_timestamp"] = found[0] if found else None
                item["latest_sampled_timestamp"] = found[-1] if found else None
            items.append(item)
        if progress:
            progress(source_name, len(items), cursor)
        if cursor == 0:
            break
    items.sort(key=lambda x: x["key"].lower())
    namespaces: dict[str, dict[str, int]] = {}
    for item in items:
        bucket = namespaces.setdefault(item["namespace"], {"keys": 0, "records_or_bytes": 0})
        bucket["keys"] += 1
        bucket["records_or_bytes"] += item["records_or_bytes"] or 0
    return {
        "source_env": source_name,
        "connected": bool(server),
        "key_count": len(items),
        "namespaces": namespaces,
        "keys": items,
    }


def build_audit(include_data: bool, progress=None, key_patterns: list[str] | None = None) -> dict[str, Any]:
    sources = _source_specs()
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "include_data": include_data,
        "limits": {
            "max_records_per_key": MAX_RECORDS_PER_KEY,
            "max_string_bytes": MAX_STRING_BYTES,
        },
        "key_patterns": key_patterns,
        "sources": [],
        "warnings": [],
    }
    if not sources:
        report["warnings"].append("No environment variable matching REDIS_URL was found")
        return report
    for name, spec in sources.items():
        try:
            report["sources"].append(audit_source(
                name,
                spec,
                include_data,
                progress=progress,
                key_patterns=key_patterns,
            ))
        except Exception as exc:  # preserve other Redis sources if one fails
            report["sources"].append({"source_env": name, "connected": False, "error": f"{type(exc).__name__}: {exc}"})
    return report


@app.get("/")
def index() -> str:
    return render_template_string(PAGE)


@app.get("/health")
def health() -> Response:
    return jsonify({"ok": True, "read_only": True, "redis_sources_detected": list(_source_specs())})


@app.post("/audit/summary")
def summary() -> Response:
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(build_audit(include_data=False))


@app.post("/audit/export")
def export() -> Response:
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    payload = json.dumps(build_audit(include_data=True), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(payload, compresslevel=6)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        compressed,
        mimetype="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="redis_audit_{stamp}.json.gz"'},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
