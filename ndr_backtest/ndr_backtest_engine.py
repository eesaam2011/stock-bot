from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


class RedisREST:
    def __init__(self):
        self.url = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
        self.token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

    def command(self, *parts):
        if not self.url or not self.token:
            raise RuntimeError("Redis environment is missing")
        req = Request(
            self.url,
            data=json.dumps(list(parts)).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=30) as response:
            payload = json.load(response)
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result")

    def set_json(self, key, value):
        return self.command("SET", key, json.dumps(value, ensure_ascii=False))

    def get_json(self, key, default=None):
        raw = self.command("GET", key)
        if raw is None:
            return default
        return json.loads(raw)


class Alpaca:
    def __init__(self):
        self.headers = {
            "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
        }

    def get(self, url: str, params=None):
        target = url + ("?" + urlencode(params, doseq=True) if params else "")
        for attempt in range(7):
            try:
                with urlopen(Request(target, headers=self.headers), timeout=60) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code != 429 or attempt == 6:
                    detail = exc.read(500).decode("utf-8", "replace")
                    raise RuntimeError(f"Alpaca HTTP {exc.code}: {detail}")
                time.sleep(min(30, 2 ** attempt))


class BacktestCollector:
    def __init__(self):
        self.redis = RedisREST()
        self.alpaca = Alpaca()
        self.prefix = os.getenv("NDR_BT_REDIS_PREFIX", "next_day_radar_backtest_v1")
        self.sessions_count = int(os.getenv("NDR_BT_SESSIONS", "60"))
        self.holdout_count = int(os.getenv("NDR_BT_HOLDOUT_SESSIONS", "15"))
        self.batch_size = int(os.getenv("NDR_BT_SYMBOL_BATCH_SIZE", "10"))
        self.delay = float(os.getenv("NDR_BT_REQUEST_DELAY_SECONDS", "0.15"))
        self.max_price = 25.0
        self.min_price = 0.50

    def key(self, suffix):
        return f"{self.prefix}:{suffix}"

    def status(self):
        return self.redis.get_json(self.key("status"), {"phase": "NOT_PREPARED"})

    def save_status(self, **updates):
        current = self.status()
        current.update(updates)
        current["updated_at"] = now_iso()
        self.redis.set_json(self.key("status"), current)
        return current

    def prepare(self):
        self.save_status(phase="PREPARING", message="Loading calendar and universe")
        end = datetime.now(NY).date()
        start = end - timedelta(days=140)
        calendar = self.alpaca.get(
            "https://paper-api.alpaca.markets/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
        )
        sessions = [str(row["date"]) for row in calendar][-self.sessions_count:]
        if len(sessions) < self.sessions_count:
            raise RuntimeError(f"Only {len(sessions)} sessions returned")
        assets = self.alpaca.get(
            "https://paper-api.alpaca.markets/v2/assets",
            {"status": "active", "asset_class": "us_equity"},
        )
        bad = ("warrant", "unit", "right", "preferred", "etf", "etn", "fund", "trust", "acquisition", "blank check")
        symbols = []
        for asset in assets:
            symbol = str(asset.get("symbol") or "").upper()
            name = str(asset.get("name") or "").lower()
            if not asset.get("tradable") or not SYMBOL_RE.fullmatch(symbol):
                continue
            if symbol.endswith("Q") or any(word in name for word in bad):
                continue
            symbols.append(symbol)
        symbols = sorted(set(symbols))
        split = len(sessions) - self.holdout_count
        manifest = {
            "sessions": sessions,
            "development_sessions": sessions[:split],
            "holdout_sessions": sessions[split:],
            "symbols": symbols,
            "created_at": now_iso(),
            "source_version": os.getenv("NDR_BT_SOURCE_VERSION", "unknown"),
            "source_build": os.getenv("NDR_BT_SOURCE_BUILD", "unknown"),
        }
        self.redis.set_json(self.key("manifest"), manifest)
        self.redis.set_json(self.key("coarse_candidates"), {})
        self.redis.set_json(self.key("cursor"), {"feed_index": 0, "batch_index": 0, "page_token": None})
        return self.save_status(
            phase="COARSE_READY", message="Ready for causal 15-minute scan",
            sessions=len(sessions), universe=len(symbols), holdout=self.holdout_count,
            coarse_candidates=0, requests=0,
        )

    @staticmethod
    def phase_key(timestamp: str):
        et = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(NY)
        minutes = et.hour * 60 + et.minute
        if 16 * 60 <= minutes < 20 * 60:
            phase = "AFTER_HOURS"
        elif minutes >= 20 * 60 or minutes < 4 * 60:
            phase = "OVERNIGHT"
        elif 4 * 60 <= minutes < 9 * 60 + 30:
            phase = "PREMARKET"
        elif 9 * 60 + 30 <= minutes < 16 * 60:
            phase = "REGULAR"
        else:
            return None
        logical_day = et.date().isoformat()
        if phase == "OVERNIGHT" and minutes >= 20 * 60:
            logical_day = (et.date() + timedelta(days=1)).isoformat()
        return logical_day, phase

    def coarse_step(self):
        manifest = self.redis.get_json(self.key("manifest"))
        if not manifest:
            return self.prepare()
        cursor = self.redis.get_json(self.key("cursor"), {"feed_index": 0, "batch_index": 0, "page_token": None})
        feeds = ["sip", "boats"]
        symbol_batches = list(chunks(manifest["symbols"], self.batch_size))
        feed_index = int(cursor.get("feed_index", 0))
        batch_index = int(cursor.get("batch_index", 0))
        if feed_index >= len(feeds):
            candidates = self.redis.get_json(self.key("coarse_candidates"), {})
            return self.save_status(phase="DETAIL_READY", message="Coarse scan completed", coarse_candidates=len(candidates))
        if batch_index >= len(symbol_batches):
            cursor = {"feed_index": feed_index + 1, "batch_index": 0, "page_token": None}
            self.redis.set_json(self.key("cursor"), cursor)
            return self.coarse_step()
        sessions = manifest["sessions"]
        start = datetime.fromisoformat(sessions[0]).replace(tzinfo=NY) - timedelta(hours=8)
        end = datetime.fromisoformat(sessions[-1]).replace(tzinfo=NY) + timedelta(days=1, hours=20)
        params = {
            "symbols": ",".join(symbol_batches[batch_index]), "timeframe": "15Min",
            "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "feed": feeds[feed_index], "limit": 10000, "adjustment": "raw", "sort": "asc",
        }
        if cursor.get("page_token"):
            params["page_token"] = cursor["page_token"]
        page = self.alpaca.get("https://data.alpaca.markets/v2/stocks/bars", params)
        candidates = self.redis.get_json(self.key("coarse_candidates"), {})
        first = {}
        for symbol, bars in (page.get("bars") or {}).items():
            for bar in bars:
                key = self.phase_key(bar["t"])
                if not key or key[0] not in sessions:
                    continue
                anchor = (symbol, key[0], key[1])
                first.setdefault(anchor, float(bar["o"]))
                ref = first[anchor]
                high = float(bar["h"])
                if self.min_price <= high <= self.max_price * 1.5 and ref > 0 and (high / ref - 1) * 100 >= 1.5:
                    candidates[f"{key[0]}:{symbol}"] = {"session": key[0], "symbol": symbol, "first_phase": key[1]}
        self.redis.set_json(self.key("coarse_candidates"), candidates)
        token = page.get("next_page_token")
        if token:
            cursor["page_token"] = token
        else:
            cursor = {"feed_index": feed_index, "batch_index": batch_index + 1, "page_token": None}
        self.redis.set_json(self.key("cursor"), cursor)
        status = self.status()
        requests_count = int(status.get("requests", 0)) + 1
        progress_total = len(feeds) * max(1, len(symbol_batches))
        progress_done = feed_index * len(symbol_batches) + batch_index
        result = self.save_status(
            phase="COARSE_SCANNING", message=f"Scanning {feeds[feed_index]} batch {batch_index + 1}/{len(symbol_batches)}",
            requests=requests_count, coarse_candidates=len(candidates),
            progress_pct=round(progress_done / progress_total * 100, 2), cursor=cursor,
        )
        time.sleep(self.delay)
        return result

