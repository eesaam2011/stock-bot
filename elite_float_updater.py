"""Update the existing shared Gist without importing the Early Explosion bot."""

import json
import math
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


# Copied verbatim from the deployed Early Explosion float producer.
SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS",
    "AXP", "USB", "TFC", "PNC", "COF", "DFS",
    "MET", "PRU", "ALL", "AIG", "AFL", "TRV", "HIG", "CB",

    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR",

    "BUD", "TAP", "STZ", "DEO",
    "PM", "MO", "BTI",

    "CGC", "TLRY", "ACB", "SNDL", "CRON",

    "NCLH", "CCL", "RCL",

    "AMC", "CNK", "IMAX", "HITI",

    "GPRE", "SKLZ", "PGY", "JELD",
    "TWO", "PGEN", "GENI", "TRC", "SVA", "SHOE",
}

BAD_NAME_KEYWORDS = [
    "etf", "fund", "trust", "warrant", "unit", "right", "preferred",
    "bond", "notes", "income", "index", "acquisition", "blank check",
    "spac", "bank", "bancorp", "credit", "lending", "loan", "mortgage",
    "insurance", "casino", "gambling", "betting", "sportsbook",
    "alcohol", "beer", "wine", "tobacco", "cannabis", "marijuana",
    "hemp", "cruise", "cinema", "movie", "theater", "reit", "cbd",
]


class DailyFloatUpdater:
    def __init__(self, *, gist_id, token, finnhub_key, filename,
                 list_assets, wait_slot, blacklist, bad_names, log,
                 publish_cache, now=None, http=requests):
        self.gist_id = gist_id
        self.token = token
        self.finnhub_key = finnhub_key
        self.filename = filename
        self.list_assets = list_assets
        self.wait_slot = wait_slot
        self.blacklist = blacklist
        self.bad_names = bad_names
        self.log = log
        self.publish_cache = publish_cache
        self.now = now or (lambda: datetime.now(ZoneInfo("Asia/Riyadh")))
        self.http = http
        self.lock = threading.Lock()
        self.completed_date = None

    @property
    def headers(self):
        return {"Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json"}

    @property
    def url(self):
        return f"https://api.github.com/gists/{self.gist_id}"

    def read_cache(self):
        response = self.http.get(self.url, headers=self.headers, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"Gist read HTTP={response.status_code}")
        file_data = response.json().get("files", {}).get(self.filename)
        if not isinstance(file_data, dict):
            raise RuntimeError("Existing float file missing; refusing to replace it")
        if file_data.get("truncated"):
            # GitHub may truncate the API's inline file content.
            raw_url = file_data.get("raw_url", "")
            if not raw_url.startswith("https://gist.githubusercontent.com/"):
                raise RuntimeError("Invalid Gist raw URL")
            raw = self.http.get(raw_url, timeout=30)
            if raw.status_code != 200:
                raise RuntimeError(f"Gist raw read HTTP={raw.status_code}")
            content = raw.text
        else:
            content = file_data.get("content", "")
        cache = json.loads(content)
        if not isinstance(cache, dict) or not cache:
            raise RuntimeError("Invalid or empty shared float cache")
        return cache

    def save_cache(self, cache):
        response = self.http.patch(
            self.url, headers=self.headers,
            json={"files": {self.filename: {"content": json.dumps(cache, indent=4)}}},
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Gist write HTTP={response.status_code}")
        # A successful HTTP response alone is insufficient to retire the old bot.
        if self.read_cache() != cache:
            raise RuntimeError("Gist read-back mismatch")
        self.publish_cache(cache)

    def fetch_float(self, symbol):
        self.wait_slot()  # Shared with news, not an independent API quota.
        try:
            response = self.http.get(
                "https://finnhub.io/api/v1/stock/profile2",
                params={"symbol": symbol, "token": self.finnhub_key}, timeout=10,
            )
            if response.status_code != 200:
                return None
            value = response.json().get("floatingShare")
            if value is None:
                return None
            value = float(value) * 1_000_000
            return value if math.isfinite(value) and value >= 0 else None
        except Exception:
            return None

    def run_once(self):
        current = self.now()
        today = current.strftime("%Y-%m-%d")
        if (current.hour, current.minute) < (7, 50) or self.completed_date == today:
            return False
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if not all((self.gist_id, self.token, self.finnhub_key)):
                raise RuntimeError("Missing GIST_ID, GITHUB_TOKEN or FINNHUB_API_KEY")
            if self.filename != "float_cache.json":
                raise RuntimeError("Expected existing filename float_cache.json")
            cache = self.read_cache()
            # Keep Early Explosion's asset scope: active assets, original name
            # and symbol exclusions, no news-universe/price/tradability filter.
            assets = self.list_assets()
            weekly = current.weekday() == 0
            self.log(f"[FLOAT_UPDATE] START date={today} weekly={weekly} total={len(cache)}")
            loaded = updated = skipped = missing = 0
            pending = 0
            for asset in assets:
                symbol = asset.symbol
                old = cache.get(symbol)
                if symbol in cache and not weekly:
                    skipped += 1
                    continue
                # A restart on Monday resumes already persisted progress.
                if isinstance(old, dict) and old.get("updated") == today:
                    skipped += 1
                    continue
                name = (getattr(asset, "name", "") or "").lower()
                if symbol in self.blacklist or any(kw in name for kw in self.bad_names):
                    skipped += 1
                    continue
                value = self.fetch_float(symbol)
                if value is None:
                    missing += 1
                    continue
                old_value = old.get("float") if isinstance(old, dict) else None
                cache[symbol] = {"float": value, "updated": today}
                if old_value is None:
                    loaded += 1
                elif float(old_value) != value:
                    updated += 1
                else:
                    skipped += 1
                pending += 1
                if pending >= 25:
                    self.save_cache(cache)
                    pending = 0
                    self.log(f"[FLOAT_UPDATE] CHECKPOINT total={len(cache)}")
            self.save_cache(cache)
            self.completed_date = today
            self.log(f"[FLOAT_UPDATE] VERIFIED date={today} loaded={loaded} "
                     f"updated={updated} skipped={skipped} missing={missing} total={len(cache)}")
            return True
        except Exception as exc:
            # Do not print request exception text, which can contain API tokens.
            detail = str(exc) if type(exc) is RuntimeError else type(exc).__name__
            self.log(f"[FLOAT_UPDATE] FAILED reason={detail}")
            return False
        finally:
            self.lock.release()

    def loop(self):
        self.log("[FLOAT_UPDATE] scheduler started | 07:50 Asia/Riyadh | Monday full refresh")
        while True:
            self.run_once()
            # A failed read/write retries later and never becomes a completed day.
            threading.Event().wait(300)
