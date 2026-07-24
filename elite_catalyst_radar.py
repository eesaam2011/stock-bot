# ==============================================================================
# Elite Catalyst Radar
# Version : 2.2
# Build   : VERIFIED-2026-07-24-A
# File    : elite_catalyst_radar.py
# Author  : OpenAI + Essam
#
# Deployment:
#   Render Background Worker
#
# Start Command:
#   python elite_catalyst_radar.py
#
# Architecture:
#   1) Finnhub news is the primary discovery trigger.
#   2) Strong positive catalysts are added to NEWS_WATCHLIST.
#   3) NEWS_WATCHLIST is monitored with Alpaca market data every 7 seconds.
#   4) Entry alerts are sent only after technical confirmation.
# ==============================================================================

import os
import json
import html
try:
    import orjson
except Exception:
    orjson = None
import math
import time
import requests
import threading
import traceback
import zoneinfo
import pandas as pd

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import TimeFrame

# ==============================================================================
# Timezones
# ==============================================================================

saudi_tz = zoneinfo.ZoneInfo("Asia/Riyadh")
ny_tz = zoneinfo.ZoneInfo("America/New_York")

# ==============================================================================
# Environment Variables
# ==============================================================================

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

FLOAT_CACHE_URL = os.getenv("FLOAT_CACHE_URL", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")
FLOAT_CACHE_FILENAME = os.getenv("FLOAT_CACHE_FILENAME", "float_cache.json")

# ==============================================================================
# Configuration
# ==============================================================================

BOT_NAME = "Elite Catalyst Radar"
BOT_NAME_AR = "رادار محفزات النخبة"

PRICE_MIN = 0.50
PRICE_MAX = 25.00
MAX_SPREAD = 2.0
MAX_STOP = 6.0

# News discovery request budget.
FINNHUB_MAX_REQUESTS_PER_MINUTE = 40
FINNHUB_DELAY = 60.0 / FINNHUB_MAX_REQUESTS_PER_MINUTE
NEWS_LOOKBACK_HOURS = 12
NEWS_RECHECK_TTL = 60 * 60
NEWS_WATCH_TTL = 6 * 60 * 60
SERIOUS_NEGATIVE_TTL = 72 * 60 * 60

# News candidates are checked using market data only.
NEWS_WATCH_MONITOR_INTERVAL = 7
TRADE_MONITOR_INTERVAL = 30
UNIVERSE_REFRESH_INTERVAL = 4 * 60 * 60
FLOAT_RELOAD_HOUR_KSA = 10
FLOAT_RELOAD_MINUTE_KSA = 45

# Technical confirmation after a news catalyst.
MIN_CATALYST_SCORE = 55
MIN_ENTRY_SCORE = 78
LAST_HOUR_ENTRY_SCORE = 86
MIN_RVOL = 2.0
MIN_VOLUME_ACCEL = 1.25
MIN_ATR_PCT = 0.80
MAX_RESISTANCE_DISTANCE_PCT = 2.5
SNAPSHOT_BATCH_SIZE = 200
VWAP_FAILURE_CONFIRMATIONS = 2
VOLUME_DEATH_CONFIRMATIONS = 2
NEWS_REVALIDATE_BEFORE_ALERT = True
REDIS_CLEANUP_INTERVAL = 30 * 60
STALE_ACTIVE_TRADE_SECONDS = 18 * 60 * 60
END_OF_DAY_CLOSE_HOUR_NY = 16
END_OF_DAY_CLOSE_MINUTE_NY = 5

FULL_UNIVERSE_REFRESH_TIMES_KSA = {"10:45", "16:20"}

# ==============================================================================
# Redis Keys - new namespace to avoid mixing with old Elite Radar state
# ==============================================================================

REDIS_PREFIX = "elite_catalyst"
KEY_STATE = f"{REDIS_PREFIX}:state"
KEY_UNIVERSE = f"{REDIS_PREFIX}:universe"
KEY_FLOAT = f"{REDIS_PREFIX}:float"
KEY_NEWS_CACHE = f"{REDIS_PREFIX}:news_cache"
KEY_NEWS_WATCHLIST = f"{REDIS_PREFIX}:news_watchlist"
KEY_ACTIVE = f"{REDIS_PREFIX}:active"
KEY_HISTORY = f"{REDIS_PREFIX}:history"
KEY_ALERTS = f"{REDIS_PREFIX}:alerts"
KEY_RUNTIME = f"{REDIS_PREFIX}:runtime"
KEY_BLOCKED_NEWS = f"{REDIS_PREFIX}:blocked_news"

# ==============================================================================
# Runtime State
# ==============================================================================

runtime_stats = {
    "started": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
    "news_requests": 0,
    "news_symbols_checked": 0,
    "positive_catalysts": 0,
    "watchlist_checks": 0,
    "alerts_sent": 0,
    "active_trades": 0,
    "float_records": 0,
    "universe_size": 0,
    "news_watchlist_size": 0,
    "last_news_scan": "Never",
    "last_watch_scan": "Never",
    "last_universe": "Never",
}

FLOAT_CACHE: Dict[str, float] = {}
NEWS_CACHE: Dict[str, dict] = {}
NEWS_WATCHLIST: Dict[str, dict] = {}
ACTIVE_TRADES: Dict[str, dict] = {}
UNIVERSE: List[str] = []
NEWS_CURSOR = 0

ACTIVE_TRADES_LOCK = threading.Lock()
WATCHLIST_LOCK = threading.Lock()
FINNHUB_LOCK = threading.Lock()
LAST_FINNHUB_REQUEST_TIME = 0.0
BAR_CACHE: Dict[str, tuple] = {}

api = tradeapi.REST(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    api_version="v2",
)

# ==============================================================================
# Logging / Serialization
# ==============================================================================

def log(message):
    now = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def json_dumps(data):
    if orjson:
        return orjson.dumps(data, default=str).decode("utf-8")
    return json.dumps(data, ensure_ascii=False, default=str)


def json_loads(data):
    if orjson:
        return orjson.loads(data)
    return json.loads(data)


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def now_ksa():
    return datetime.now(saudi_tz)


def now_ny():
    return datetime.now(ny_tz)


def today_ksa():
    return now_ksa().strftime("%Y-%m-%d")


def fmt_price(value):
    value = safe_float(value)
    return f"${value:.2f}" if value >= 1 else f"${value:.4f}"


def fmt_big_number(value):
    value = safe_float(value)
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram credentials missing; message not sent.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if response.status_code != 200:
            log(f"Telegram error {response.status_code}: {response.text[:200]}")
            return False
        return True
    except Exception as exc:
        log(f"Telegram exception: {exc}")
        return False

# ==============================================================================
# Redis
# ==============================================================================

def redis_available():
    return bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)


def redis_command(command):
    if not redis_available():
        return None
    try:
        response = requests.post(
            UPSTASH_REDIS_REST_URL,
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            json=command,
            timeout=20,
        )
        if response.status_code != 200:
            log(f"Redis error {response.status_code}: {response.text[:200]}")
            return None
        return response.json().get("result")
    except Exception as exc:
        log(f"Redis exception: {exc}")
        return None


def redis_get_json(key, default=None):
    result = redis_command(["GET", key])
    if result is None:
        return default
    try:
        return json_loads(result)
    except Exception:
        return default


def redis_set_json(key, value, expire_seconds=None):
    payload = json_dumps(value)
    command = ["SET", key, payload]
    if expire_seconds:
        command += ["EX", int(expire_seconds)]
    return redis_command(command)


def redis_hset_json(key, field, value):
    return redis_command(["HSET", key, field, json_dumps(value)])


def redis_hget_json(key, field, default=None):
    raw = redis_command(["HGET", key, field])
    if raw is None:
        return default
    try:
        return json_loads(raw)
    except Exception:
        return default


def redis_delete(key):
    return redis_command(["DEL", key])


def redis_type(key):
    result = redis_command(["TYPE", key])
    return str(result or "none")


def redis_hgetall_json(key):
    raw = redis_command(["HGETALL", key])
    output = {}
    if not raw:
        return output
    try:
        for i in range(0, len(raw), 2):
            try:
                output[str(raw[i])] = json_loads(raw[i + 1])
            except Exception:
                continue
    except Exception as exc:
        log(f"Redis HGETALL parse error: {exc}")
    return output


def redis_hdel(key, field):
    return redis_command(["HDEL", key, field])

# ==============================================================================
# Market / Date Helpers
# ==============================================================================

def is_weekend():
    return now_ny().weekday() >= 5


def is_scan_window():
    if is_weekend():
        return False
    current = now_ksa().time()
    start = datetime.strptime("11:00", "%H:%M").time()
    end = datetime.strptime("01:00", "%H:%M").time()
    return current >= start or current <= end


def is_last_market_hour():
    ny = now_ny()
    if ny.weekday() >= 5:
        return False
    return ny.replace(hour=15, minute=0, second=0, microsecond=0) <= ny <= ny.replace(hour=16, minute=0, second=0, microsecond=0)


def unix_to_dt(timestamp):
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    except Exception:
        return None

# ==============================================================================
# Symbol Filters
# ==============================================================================

SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC", "PNC",
    "COF", "DFS", "MET", "PRU", "ALL", "AIG", "AFL", "TRV", "HIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR", "BUD", "TAP", "STZ",
    "DEO", "PM", "MO", "BTI", "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL", "AMC", "CNK", "IMAX", "HITI",
}

BAD_NAME_KEYWORDS = [
    "ETF", "ETN", "FUND", "TRUST", "INDEX", "WARRANT", "UNIT", "RIGHT",
    "SPAC", "ACQUISITION", "BLANK CHECK", "PREFERRED", "NOTE", "BOND",
    "DEBT", "2X", "3X", "ULTRA", "INVERSE", "BEAR", "BULL",
]


def is_clean_symbol(symbol):
    symbol = str(symbol or "").upper().strip()
    if not symbol or len(symbol) > 5 or not symbol.isalpha():
        return False
    if symbol in SYMBOL_BLACKLIST:
        return False
    if len(symbol) >= 5 and symbol[-1] in {"W", "U", "R", "P", "Q", "Z"}:
        return False
    return True


def is_bad_asset_name(name):
    upper = str(name or "").upper()
    return any(keyword in upper for keyword in BAD_NAME_KEYWORDS)

# ==============================================================================
# Float Cache
# ==============================================================================

def normalize_float_cache(raw_data):
    normalized = {}
    if not isinstance(raw_data, dict):
        return normalized
    for symbol, value in raw_data.items():
        symbol = str(symbol).upper().strip()
        if not is_clean_symbol(symbol):
            continue
        if isinstance(value, dict):
            value = value.get("float") or value.get("share_float") or value.get("floatShares") or value.get("value")
        value = safe_float(value)
        if value > 0:
            normalized[symbol] = value
    return normalized


def load_float_from_url():
    if not FLOAT_CACHE_URL:
        return {}
    try:
        response = requests.get(FLOAT_CACHE_URL, headers={"Cache-Control": "no-cache"}, timeout=30)
        if response.status_code == 200:
            return normalize_float_cache(response.json())
        log(f"Float URL failed: {response.status_code}")
    except Exception as exc:
        log(f"Float URL error: {exc}")
    return {}


def load_float_from_gist():
    if not GIST_ID:
        return {}
    try:
        headers = {"Accept": "application/vnd.github+json", "Cache-Control": "no-cache"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        response = requests.get(f"https://api.github.com/gists/{GIST_ID}?t={int(time.time())}", headers=headers, timeout=30)
        if response.status_code != 200:
            log(f"Gist float failed: {response.status_code}")
            return {}
        file_data = response.json().get("files", {}).get(FLOAT_CACHE_FILENAME)
        if not file_data:
            return {}
        return normalize_float_cache(json.loads(file_data.get("content", "{}")))
    except Exception as exc:
        log(f"Gist float error: {exc}")
        return {}


def load_float_cache():
    global FLOAT_CACHE
    log("Loading float cache...")
    data = load_float_from_url() or load_float_from_gist() or normalize_float_cache(redis_get_json(KEY_FLOAT, {}))
    FLOAT_CACHE = data or {}
    runtime_stats["float_records"] = len(FLOAT_CACHE)
    if FLOAT_CACHE:
        redis_set_json(KEY_FLOAT, FLOAT_CACHE)
    log(f"Float records loaded: {len(FLOAT_CACHE)}")


def get_float(symbol):
    return FLOAT_CACHE.get(str(symbol).upper())


def get_float_score(symbol):
    value = get_float(symbol)
    if not value:
        return 0
    if value <= 5_000_000:
        return 10
    if value <= 15_000_000:
        return 8
    if value <= 30_000_000:
        return 6
    if value <= 50_000_000:
        return 4
    if value <= 100_000_000:
        return 2
    return 0


def get_float_label(symbol):
    value = get_float(symbol)
    if not value:
        return "غير متوفر"
    if value <= 5_000_000:
        label = "منخفض جدًا"
    elif value <= 15_000_000:
        label = "منخفض"
    elif value <= 30_000_000:
        label = "جيد"
    elif value <= 50_000_000:
        label = "مقبول"
    else:
        label = "مرتفع"
    return f"{label} ({fmt_big_number(value)})"


def get_min_dollar_volume(float_shares):
    value = safe_float(float_shares)
    if value <= 0:
        return 500_000
    if value <= 10_000_000:
        return 250_000
    if value <= 30_000_000:
        return 500_000
    if value <= 60_000_000:
        return 750_000
    return 1_000_000

# ==============================================================================
# Universe
# ==============================================================================

def build_clean_universe():
    global UNIVERSE
    log("Building clean news universe from Alpaca assets...")
    asset_symbols = []
    try:
        for asset in api.list_assets(status="active"):
            symbol = str(getattr(asset, "symbol", "")).upper()
            exchange = str(getattr(asset, "exchange", "")).upper()
            if not bool(getattr(asset, "tradable", False)):
                continue
            if exchange not in {"NASDAQ", "NYSE", "AMEX", "ARCA"}:
                continue
            if not is_clean_symbol(symbol):
                continue
            if is_bad_asset_name(getattr(asset, "name", "")):
                continue
            asset_symbols.append(symbol)
    except Exception as exc:
        log(f"Universe asset load error: {exc}")

    # Filter by the bot's actual tradable price range before spending Finnhub calls.
    filtered = []
    unique_symbols = sorted(set(asset_symbols))
    for start in range(0, len(unique_symbols), SNAPSHOT_BATCH_SIZE):
        chunk = unique_symbols[start:start + SNAPSHOT_BATCH_SIZE]
        snapshots = get_snapshots_batch(chunk)
        for symbol in chunk:
            price = safe_float(snapshots.get(symbol, {}).get("price"))
            if PRICE_MIN <= price <= PRICE_MAX:
                filtered.append(symbol)
        if start and start % 1000 == 0:
            log(f"Universe price filter progress: {min(start + SNAPSHOT_BATCH_SIZE, len(unique_symbols))}/{len(unique_symbols)}")

    # Preserve an existing healthy universe if a temporary snapshot outage returns almost nothing.
    previous = list(UNIVERSE) or (redis_get_json(KEY_UNIVERSE, []) or [])
    if previous and len(filtered) < max(50, int(len(previous) * 0.20)):
        log(f"Universe rebuild rejected as unhealthy: new={len(filtered)} previous={len(previous)}")
        filtered = previous

    UNIVERSE = sorted(set(filtered))
    redis_set_json(KEY_UNIVERSE, UNIVERSE)
    runtime_stats["universe_size"] = len(UNIVERSE)
    runtime_stats["last_universe"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    log(f"News universe ready: {len(UNIVERSE)} symbols in ${PRICE_MIN:.2f}-${PRICE_MAX:.2f}")


def load_universe():
    global UNIVERSE
    UNIVERSE = redis_get_json(KEY_UNIVERSE, []) or []
    runtime_stats["universe_size"] = len(UNIVERSE)
    if not UNIVERSE:
        build_clean_universe()

# ==============================================================================
# News Classification
# ==============================================================================

SERIOUS_NEGATIVE_PHRASES = [
    "public offering", "registered direct offering", "private placement", "atm offering",
    "at-the-market offering", "shelf registration", "reverse stock split", "reverse split",
    "delisting", "nasdaq non-compliance", "bankruptcy", "chapter 11", "going concern",
    "warrant exercise", "prices offering", "securities purchase agreement",
]

MINOR_NEGATIVE_PHRASES = [
    "lawsuit", "class action", "investigation", "sec investigation", "downgrade",
    "resignation", "termination", "withdraws guidance", "misses estimates",
]

MAJOR_CATALYST_PHRASES = [
    "fda approval", "fda clearance", "regulatory approval", "breakthrough therapy",
    "phase 3", "phase iii", "positive topline", "positive top-line", "positive data",
    "meets primary endpoint", "government contract", "major contract", "purchase order",
    "exclusive agreement", "exclusive distribution", "strategic partnership",
    "license agreement", "licensing agreement", "commercial launch", "acquisition",
    "merger agreement", "buyout", "record revenue", "earnings beat", "patent granted",
]

POSITIVE_CATALYST_PHRASES = [
    "approval", "clearance", "authorized", "contract", "agreement", "deal", "supply",
    "supply agreement", "commercial supply", "distribution", "exclusive",
    "partnership", "strategic partnership", "collaboration", "award", "purchase order",
    "patent", "license", "licensing", "launch", "commercial launch", "expands",
    "expansion", "selected", "positive results", "positive data", "topline results",
    "primary endpoint", "phase 1", "phase 2", "phase 3", "fda", "ce mark",
    "record revenue", "revenue growth", "earnings beat", "raises guidance",
    "merger", "acquisition", "buyout", "breakthrough", "reimbursement",
]

WEAK_OR_ROUTINE_PHRASES = [
    "participate in", "conference", "fireside chat", "presentation", "webcast",
    "annual meeting", "investor day", "appoints", "announces date", "files form",
]


def classify_news_item(item):
    headline = str(item.get("headline", "") or "")
    summary = str(item.get("summary", "") or "")
    text = f"{headline} {summary}".lower()

    serious_negative = [p for p in SERIOUS_NEGATIVE_PHRASES if p in text]
    minor_negative = [p for p in MINOR_NEGATIVE_PHRASES if p in text]
    major_hits = [p for p in MAJOR_CATALYST_PHRASES if p in text]
    positive_hits = [p for p in POSITIVE_CATALYST_PHRASES if p in text]
    routine_hits = [p for p in WEAK_OR_ROUTINE_PHRASES if p in text]

    score = 0
    reasons = []

    if serious_negative:
        score -= 100
    if minor_negative:
        score -= 20

    score += min(len(set(positive_hits)) * 10, 40)
    score += min(len(set(major_hits)) * 18, 45)

    if headline and any(p in headline.lower() for p in MAJOR_CATALYST_PHRASES):
        score += 10
    if routine_hits and not major_hits:
        score -= 15

    # Prefer company-issued or recognized news sources when Finnhub provides source.
    source = str(item.get("source", "") or "")
    if source:
        score += 3

    news_dt = unix_to_dt(item.get("datetime"))
    age_minutes = 99999
    if news_dt:
        age_minutes = max(0, (datetime.now(timezone.utc) - news_dt).total_seconds() / 60)
        if age_minutes <= 30:
            score += 15
        elif age_minutes <= 120:
            score += 10
        elif age_minutes <= 360:
            score += 5

    if major_hits:
        reasons.append("محفز رئيسي: " + ", ".join(sorted(set(major_hits))[:3]))
    elif positive_hits:
        reasons.append("محفز إيجابي: " + ", ".join(sorted(set(positive_hits))[:3]))
    if age_minutes < 99999:
        reasons.append(f"عمر الخبر {age_minutes:.0f} دقيقة")

    category = "neutral"
    if serious_negative:
        category = "serious_negative"
    elif score >= 85:
        category = "major_catalyst"
    elif score >= MIN_CATALYST_SCORE:
        category = "positive_catalyst"
    elif minor_negative:
        category = "minor_negative"

    return {
        "headline": headline,
        "summary": summary,
        "source": source,
        "url": item.get("url", ""),
        "news_id": str(item.get("id") or f"{headline}:{item.get('datetime', '')}"),
        "datetime": safe_int(item.get("datetime")),
        "age_minutes": age_minutes,
        "score": max(-100, min(100, score)),
        "category": category,
        "positive": category in {"positive_catalyst", "major_catalyst"},
        "major": category == "major_catalyst",
        "serious_negative": bool(serious_negative),
        "minor_negative": bool(minor_negative),
        "matched_positive": sorted(set(positive_hits)),
        "matched_major": sorted(set(major_hits)),
        "reasons": reasons,
    }


def finnhub_wait_slot():
    global LAST_FINNHUB_REQUEST_TIME
    with FINNHUB_LOCK:
        elapsed = time.time() - LAST_FINNHUB_REQUEST_TIME
        if elapsed < FINNHUB_DELAY:
            time.sleep(FINNHUB_DELAY - elapsed)
        LAST_FINNHUB_REQUEST_TIME = time.time()


def fetch_company_news(symbol):
    if not FINNHUB_API_KEY:
        return []
    finnhub_wait_slot()
    now_utc = datetime.now(timezone.utc)
    from_date = (now_utc - timedelta(hours=NEWS_LOOKBACK_HOURS)).date()
    to_date = now_utc.date()
    try:
        response = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": from_date.strftime("%Y-%m-%d"),
                "to": to_date.strftime("%Y-%m-%d"),
                "token": FINNHUB_API_KEY,
            },
            timeout=15,
        )
        runtime_stats["news_requests"] += 1
        if response.status_code != 200:
            log(f"Finnhub {symbol} error {response.status_code}")
            return []
        data = response.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        log(f"Finnhub exception {symbol}: {exc}")
        return []


def analyze_symbol_news(symbol, force_refresh=False):
    now_ts = time.time()
    cached = NEWS_CACHE.get(symbol)
    if not force_refresh and cached and now_ts - safe_float(cached.get("checked_at")) < NEWS_RECHECK_TTL:
        return cached

    items = fetch_company_news(symbol)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    classified = []
    for item in items[:20]:
        dt = unix_to_dt(item.get("datetime"))
        if dt and dt < cutoff:
            continue
        classified.append(classify_news_item(item))

    classified.sort(key=lambda x: (safe_float(x.get("score")), safe_int(x.get("datetime"))), reverse=True)
    best = classified[0] if classified else {
        "headline": "", "score": 0, "category": "neutral", "positive": False,
        "major": False, "serious_negative": False, "minor_negative": False,
        "news_id": "", "datetime": 0, "reasons": [],
    }

    # Any recent serious dilution/compliance item blocks the symbol even if another headline is positive.
    serious_items = [x for x in classified if x.get("serious_negative")]
    if serious_items:
        newest_serious = max(serious_items, key=lambda x: safe_int(x.get("datetime")))
        best = dict(newest_serious)
        best["blocked_by_negative"] = True

    result = {
        "symbol": symbol,
        "checked_at": now_ts,
        "best": best,
        "items_count": len(classified),
    }
    NEWS_CACHE[symbol] = result
    redis_hset_json(KEY_NEWS_CACHE, symbol, result)
    return result

# ==============================================================================
# Serious Negative News Block
# ==============================================================================

def get_active_news_block(symbol):
    block = redis_hget_json(KEY_BLOCKED_NEWS, symbol, None)
    if not block:
        return None
    expires_at = safe_float(block.get("expires_at"))
    if expires_at <= time.time():
        redis_hdel(KEY_BLOCKED_NEWS, symbol)
        return None
    return block


def is_symbol_news_blocked(symbol):
    return get_active_news_block(symbol) is not None


# ==============================================================================
# NEWS_WATCHLIST
# ==============================================================================

def load_news_watchlist():
    global NEWS_WATCHLIST
    NEWS_WATCHLIST = redis_hgetall_json(KEY_NEWS_WATCHLIST)
    cleanup_news_watchlist()
    runtime_stats["news_watchlist_size"] = len(NEWS_WATCHLIST)


def add_to_news_watchlist(symbol, news_data):
    if is_symbol_news_blocked(symbol):
        log(f"News catalyst ignored for blocked symbol: {symbol}")
        return False
    best = news_data.get("best", {})
    news_id = str(best.get("news_id", ""))
    existing = NEWS_WATCHLIST.get(symbol)

    # Do not reset first-seen time for the same catalyst.
    if existing and existing.get("news_id") == news_id:
        existing["last_news_check"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
        existing["catalyst_score"] = best.get("score")
        existing["headline"] = best.get("headline")
        redis_hset_json(KEY_NEWS_WATCHLIST, symbol, existing)
        return False

    item = {
        "symbol": symbol,
        "news_id": news_id,
        "headline": best.get("headline", ""),
        "summary": best.get("summary", ""),
        "source": best.get("source", ""),
        "url": best.get("url", ""),
        "news_timestamp": best.get("datetime", 0),
        "news_age_minutes": best.get("age_minutes", 0),
        "catalyst_score": best.get("score", 0),
        "catalyst_category": best.get("category", "positive_catalyst"),
        "catalyst_reasons": best.get("reasons", []),
        "major_catalyst": bool(best.get("major")),
        "added_ts": time.time(),
        "added_at": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
        "last_checked_ts": 0,
        "last_market_status": "waiting",
        "best_entry_score": 0,
        "date": today_ksa(),
    }
    with WATCHLIST_LOCK:
        NEWS_WATCHLIST[symbol] = item
    redis_hset_json(KEY_NEWS_WATCHLIST, symbol, item)
    runtime_stats["positive_catalysts"] += 1
    runtime_stats["news_watchlist_size"] = len(NEWS_WATCHLIST)
    log(f"News catalyst added: {symbol} score={best.get('score')} | {best.get('headline', '')[:90]}")
    return True


def remove_from_news_watchlist(symbol, reason="expired"):
    with WATCHLIST_LOCK:
        NEWS_WATCHLIST.pop(symbol, None)
    redis_hdel(KEY_NEWS_WATCHLIST, symbol)
    runtime_stats["news_watchlist_size"] = len(NEWS_WATCHLIST)
    log(f"News watch removed {symbol}: {reason}")


def cleanup_news_watchlist():
    now_ts = time.time()
    for symbol, item in list(NEWS_WATCHLIST.items()):
        added_ts = safe_float(item.get("added_ts"))
        if added_ts <= 0 or now_ts - added_ts > NEWS_WATCH_TTL:
            remove_from_news_watchlist(symbol, "catalyst TTL expired")

# ==============================================================================
# Alpaca Data
# ==============================================================================

def bars_to_dataframe(bars):
    try:
        df = bars.df.copy()
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index()
            if "timestamp" in df.columns:
                df = df.set_index("timestamp")
        df.columns = [str(c).lower() for c in df.columns]
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["open", "high", "low", "close", "volume"])
    except Exception:
        return pd.DataFrame()


def get_bars(symbol, timeframe=TimeFrame.Minute, limit=160, cache_ttl=5):
    key = f"{symbol}:{timeframe}:{limit}"
    cached = BAR_CACHE.get(key)
    if cached and time.time() - cached[0] <= cache_ttl:
        return cached[1].copy()
    try:
        df = bars_to_dataframe(api.get_bars(symbol, timeframe, limit=limit))
        BAR_CACHE[key] = (time.time(), df.copy())
        return df
    except Exception as exc:
        log(f"Bars error {symbol}: {exc}")
        return pd.DataFrame()


def get_snapshots_batch(symbols):
    result = {}
    symbols = list(dict.fromkeys(symbols or []))
    if not symbols:
        return result
    for start in range(0, len(symbols), SNAPSHOT_BATCH_SIZE):
        chunk = symbols[start:start + SNAPSHOT_BATCH_SIZE]
        try:
            raw = api.get_snapshots(chunk)
            for symbol in chunk:
                snap = raw.get(symbol)
                if not snap:
                    continue
                trade = getattr(snap, "latest_trade", None)
                quote = getattr(snap, "latest_quote", None)
                daily = getattr(snap, "daily_bar", None)
                prev = getattr(snap, "prev_daily_bar", None)
                minute = getattr(snap, "minute_bar", None)
                price = safe_float(getattr(trade, "p", None)) or safe_float(getattr(daily, "c", None))
                bid = safe_float(getattr(quote, "bp", None))
                ask = safe_float(getattr(quote, "ap", None))
                spread = ((ask - bid) / price) * 100 if price > 0 and ask > 0 and bid > 0 else 999
                day_volume = safe_float(getattr(daily, "v", None))
                prev_close = safe_float(getattr(prev, "c", None))
                result[symbol] = {
                    "symbol": symbol,
                    "price": price,
                    "bid": bid,
                    "ask": ask,
                    "spread_pct": spread,
                    "day_volume": day_volume,
                    "minute_volume": safe_float(getattr(minute, "v", None)),
                    "dollar_volume": price * day_volume,
                    "day_high": safe_float(getattr(daily, "h", None)),
                    "day_low": safe_float(getattr(daily, "l", None)),
                    "prev_close": prev_close,
                    "gap_pct": ((price - prev_close) / prev_close) * 100 if price > 0 and prev_close > 0 else 0,
                }
        except Exception as exc:
            log(f"Snapshot batch error [{start}:{start + len(chunk)}]: {exc}")
    return result

# ==============================================================================
# Indicators
# ==============================================================================

def current_ny_session_df(df, completed_only=True):
    if df.empty:
        return df
    try:
        index = pd.to_datetime(df.index, utc=True, errors="coerce")
        valid = ~index.isna()
        session = df.loc[valid].copy()
        if session.empty:
            return df
        index = index[valid].tz_convert(ny_tz)
        session.index = index
        today = now_ny().date()
        session = session.loc[session.index.date == today]
        if completed_only and not session.empty:
            current_minute = pd.Timestamp.now(tz=ny_tz).floor("min")
            session = session.loc[session.index < current_minute]
        return session
    except Exception:
        return pd.DataFrame()


def calculate_vwap(df):
    df = current_ny_session_df(df)
    if df.empty:
        return 0
    volume = df["volume"].clip(lower=0)
    total = volume.sum()
    if total <= 0:
        return 0
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return safe_float((typical * volume).sum() / total)


def completed_indicator_df(df):
    """Return current NY-session bars with the still-forming minute removed."""
    return current_ny_session_df(df, completed_only=True)


def calculate_obv(df):
    df = completed_indicator_df(df)
    if df.empty or len(df) < 15:
        return {"obv_rising": False, "obv": 0, "obv_ema": 0}
    values = [0.0]
    closes = df["close"].astype(float).values
    volumes = df["volume"].astype(float).values
    for i in range(1, len(df)):
        if closes[i] > closes[i - 1]:
            values.append(values[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            values.append(values[-1] - volumes[i])
        else:
            values.append(values[-1])
    series = pd.Series(values)
    ema = series.ewm(span=10, adjust=False).mean()
    return {
        "obv": safe_float(series.iloc[-1]),
        "obv_ema": safe_float(ema.iloc[-1]),
        "obv_rising": bool(series.iloc[-1] > ema.iloc[-1] and series.iloc[-1] > series.iloc[-3]),
    }


def calculate_atr(df, period=14):
    df = completed_indicator_df(df)
    if df.empty or len(df) < period + 2:
        return 0
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return safe_float(tr.rolling(period).mean().iloc[-1])


def calculate_rvol(df):
    df = completed_indicator_df(df)
    if df.empty or len(df) < 30:
        return 0
    volumes = df["volume"].astype(float)
    recent = volumes.tail(3).mean()
    historical = volumes.iloc[:-3].tail(100)
    baseline = historical.median()
    if baseline <= 0:
        baseline = historical.mean()
    return safe_float(recent / baseline) if baseline > 0 else 0


def calculate_volume_acceleration(df):
    df = completed_indicator_df(df)
    if df.empty or len(df) < 12:
        return {"ratio": 0, "trend_up": False}
    volumes = df["volume"].astype(float)
    recent = volumes.tail(3).mean()
    previous = volumes.iloc[-10:-3].mean()
    ratio = recent / previous if previous > 0 else 0
    return {
        "ratio": safe_float(ratio),
        "trend_up": bool(volumes.iloc[-1] >= volumes.iloc[-2] or ratio >= 1.5),
    }


def calculate_resistance(df, lookback=100):
    """Detect nearby resistance from clustered confirmed pivot highs."""
    df = completed_indicator_df(df)
    if df.empty or len(df) < 35:
        return {"resistance": 0, "distance_pct": 999, "breakout": False, "touches": 0}

    window = df.tail(lookback).copy()
    current_close = safe_float(window["close"].iloc[-1])
    previous_close = safe_float(window["close"].iloc[-2])
    if current_close <= 0:
        return {"resistance": 0, "distance_pct": 999, "breakout": False, "touches": 0}

    highs = window["high"].astype(float)
    # Exclude the two newest completed candles from constructing historical resistance.
    historical = highs.iloc[:-2]
    pivot_levels = []
    for i in range(2, len(historical) - 2):
        value = safe_float(historical.iloc[i])
        neighborhood = historical.iloc[i - 2:i + 3]
        if value > 0 and value >= safe_float(neighborhood.max()):
            pivot_levels.append(value)

    if not pivot_levels:
        pivot_levels = historical.nlargest(min(12, len(historical))).tolist()
    if not pivot_levels:
        return {"resistance": 0, "distance_pct": 999, "breakout": False, "touches": 0}

    pivot_levels.sort()
    clusters = []
    for level in pivot_levels:
        tolerance = max(level * 0.004, 0.01 if level >= 1 else 0.003)
        match = None
        for cluster in clusters:
            if abs(level - cluster["center"]) <= max(tolerance, cluster["center"] * 0.004):
                match = cluster
                break
        if match is None:
            clusters.append({"center": level, "levels": [level]})
        else:
            match["levels"].append(level)
            match["center"] = sum(match["levels"]) / len(match["levels"])

    # Prefer repeated levels near/above price; never select a remote historical high first.
    qualified = [c for c in clusters if len(c["levels"]) >= 2 and c["center"] >= current_close * 0.97]
    if not qualified:
        qualified = [c for c in clusters if c["center"] >= current_close * 0.97]
    if not qualified:
        qualified = clusters

    def rank(cluster):
        center = safe_float(cluster["center"])
        distance = abs(center - current_close) / current_close
        repeated_bonus = min(len(cluster["levels"]), 5) * 0.015
        below_penalty = 0.05 if center < current_close * 0.97 else 0.0
        return distance + below_penalty - repeated_bonus

    best = min(qualified, key=rank)
    resistance = safe_float(best["center"])
    touches = len(best["levels"])
    distance_pct = ((resistance - current_close) / current_close) * 100 if resistance > 0 else 999
    breakout = bool(
        resistance > 0
        and current_close > resistance
        and previous_close <= resistance * 1.002
    )
    return {
        "resistance": resistance,
        "distance_pct": distance_pct,
        "breakout": breakout,
        "touches": touches,
    }


def get_15m_trend(symbol):
    df = get_bars(symbol, "15Min", limit=50, cache_ttl=60)
    df = current_ny_session_df(df, completed_only=False)
    if not df.empty:
        cutoff = pd.Timestamp.now(tz=ny_tz).floor("15min")
        df = df.loc[df.index < cutoff]
    if df.empty or len(df) < 25:
        return {"ok": False, "ema20": 0, "rising": False}
    ema = df["close"].ewm(span=20, adjust=False).mean()
    price = safe_float(df["close"].iloc[-1])
    ema_now = safe_float(ema.iloc[-1])
    rising = ema_now > safe_float(ema.iloc[-3])
    return {"ok": bool(price > ema_now or rising), "ema20": ema_now, "rising": rising}

# ==============================================================================
# Technical Confirmation for News Candidates
# ==============================================================================

def score_linear(value, minimum, maximum, points):
    value = safe_float(value)
    if value <= minimum:
        return 0
    if value >= maximum:
        return points
    return ((value - minimum) / (maximum - minimum)) * points


def pass_market_hard_rules(symbol, snapshot):
    price = safe_float(snapshot.get("price"))
    spread = safe_float(snapshot.get("spread_pct"), 999)
    dollar_volume = safe_float(snapshot.get("dollar_volume"))
    if price < PRICE_MIN or price > PRICE_MAX:
        return False, "السعر خارج النطاق"
    if spread > MAX_SPREAD:
        return False, "السبريد مرتفع"
    required = get_min_dollar_volume(get_float(symbol))
    if dollar_volume < required:
        return False, "السيولة الدولارية غير كافية"
    return True, "OK"


def evaluate_news_entry(symbol, watch_item, snapshot=None):
    if snapshot is None:
        snapshot = get_snapshots_batch([symbol]).get(symbol)
    if not snapshot:
        return None, "لا توجد Snapshot"

    hard_ok, hard_reason = pass_market_hard_rules(symbol, snapshot)
    if not hard_ok:
        return None, hard_reason

    price = safe_float(snapshot.get("price"))
    df = get_bars(symbol, TimeFrame.Minute, limit=390, cache_ttl=4)
    df = current_ny_session_df(df)
    if df.empty or len(df) < 40:
        return None, "بيانات الدقيقة للجلسة الحالية غير كافية"

    vwap = calculate_vwap(df)
    rvol = calculate_rvol(df)
    accel_data = calculate_volume_acceleration(df)
    accel = safe_float(accel_data.get("ratio"))
    obv = calculate_obv(df)
    atr = calculate_atr(df)
    atr_pct = (atr / price) * 100 if price > 0 else 0
    resistance_data = calculate_resistance(df)
    resistance = safe_float(resistance_data.get("resistance"))
    distance = safe_float(resistance_data.get("distance_pct"), 999)
    trend_15m = get_15m_trend(symbol)

    closes = df["close"].tail(3).astype(float).tolist()
    previous_close = closes[-2]
    last_close = closes[-1]
    breakout = bool(resistance > 0 and last_close > resistance and price >= resistance * 0.997)
    fresh_breakout = bool(resistance > 0 and previous_close <= resistance and last_close > resistance)
    near_breakout = bool(resistance > 0 and -0.5 <= ((price - resistance) / resistance) * 100 <= MAX_RESISTANCE_DISTANCE_PCT)

    catalyst_score = safe_float(watch_item.get("catalyst_score"))
    major_catalyst = bool(watch_item.get("major_catalyst"))

    score = 0
    reasons = []
    warnings = []

    score += score_linear(catalyst_score, MIN_CATALYST_SCORE, 100, 25)
    if major_catalyst:
        score += 5
        reasons.append("محفز إخباري رئيسي")
    else:
        reasons.append("محفز إخباري إيجابي")

    score += score_linear(rvol, 1.5, 6.0, 18)
    score += score_linear(accel, 1.0, 3.5, 15)

    if price >= vwap > 0:
        score += 10
        reasons.append("السعر فوق VWAP")
    else:
        warnings.append("السعر تحت VWAP")

    if obv.get("obv_rising"):
        score += 8
        reasons.append("OBV صاعد")

    if trend_15m.get("ok"):
        score += 8
        reasons.append("اتجاه 15 دقيقة داعم")

    score += score_linear(atr_pct, 0.5, 5.0, 6)
    score += get_float_score(symbol)

    if fresh_breakout:
        score += 8
        reasons.append("اختراق حديث للمقاومة")
    elif breakout:
        score += 6
        reasons.append("ثبات فوق المقاومة")
    elif near_breakout:
        score += 3
        reasons.append("قريب من المقاومة")

    spread = safe_float(snapshot.get("spread_pct"), 999)
    if spread <= 0.5:
        score += 4
    elif spread <= 1.0:
        score += 3
    elif spread <= 1.5:
        score += 2

    # Penalties prevent buying a news spike after it is already overextended.
    gap_pct = safe_float(snapshot.get("gap_pct"))
    day_high = safe_float(snapshot.get("day_high"))
    if day_high > 0 and price >= day_high * 0.997 and gap_pct >= 15:
        score -= 8
        warnings.append("قريب جدًا من قمة اليوم بعد اندفاع كبير")
    if atr_pct < MIN_ATR_PCT:
        score -= 5
        warnings.append("ATR محدود")
    if get_float(symbol) and get_float(symbol) > 100_000_000:
        score -= 5
        warnings.append("فلوت مرتفع")
    if is_last_market_hour():
        score -= 3
        warnings.append("آخر ساعة")

    final_score = max(0, min(100, score))

    # Catalyst path can arm before full breakout, but entry still needs live momentum.
    momentum_confirmed = (
        rvol >= MIN_RVOL
        and accel >= MIN_VOLUME_ACCEL
        and price >= vwap
        and atr_pct >= MIN_ATR_PCT
        and (breakout or near_breakout)
        and (obv.get("obv_rising") or trend_15m.get("ok"))
    )

    required_score = LAST_HOUR_ENTRY_SCORE if is_last_market_hour() else MIN_ENTRY_SCORE
    entry_ready = bool(final_score >= required_score and momentum_confirmed)

    metrics = {
        "symbol": symbol,
        "price": price,
        "bid": safe_float(snapshot.get("bid")),
        "ask": safe_float(snapshot.get("ask")),
        "spread_pct": spread,
        "day_volume": safe_float(snapshot.get("day_volume")),
        "dollar_volume": safe_float(snapshot.get("dollar_volume")),
        "gap_pct": gap_pct,
        "day_high": day_high,
        "vwap": vwap,
        "rvol": rvol,
        "volume_accel_ratio": accel,
        "obv_rising": bool(obv.get("obv_rising")),
        "atr": atr,
        "atr_pct": atr_pct,
        "trend_15m_ok": bool(trend_15m.get("ok")),
        "resistance": resistance,
        "resistance_distance_pct": distance,
        "breakout": breakout,
        "fresh_breakout": fresh_breakout,
        "near_breakout": near_breakout,
        "float_value": get_float(symbol),
        "float_label": get_float_label(symbol),
        "catalyst_score": catalyst_score,
        "catalyst_category": watch_item.get("catalyst_category"),
        "major_catalyst": major_catalyst,
        "news_headline": watch_item.get("headline", ""),
        "news_source": watch_item.get("source", ""),
        "news_url": watch_item.get("url", ""),
        "news_age_minutes": watch_item.get("news_age_minutes", 0),
        "reasons": reasons,
        "warnings": warnings,
        "final_score": final_score,
        "required_score": required_score,
        "momentum_confirmed": momentum_confirmed,
        "entry_ready": entry_ready,
        "evaluated_at": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return metrics, "ready" if entry_ready else "waiting technical confirmation"

# ==============================================================================
# Trade Plan / Alert
# ==============================================================================

def already_alerted_today(symbol):
    alerts = redis_get_json(KEY_ALERTS, {}) or {}
    item = alerts.get(symbol)
    return bool(item and item.get("date") == today_ksa())


def save_sent_alert(symbol, metrics):
    alerts = redis_get_json(KEY_ALERTS, {}) or {}
    alerts[symbol] = {
        "date": today_ksa(),
        "time": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
        "score": metrics.get("final_score"),
        "news_id": NEWS_WATCHLIST.get(symbol, {}).get("news_id"),
    }
    redis_set_json(KEY_ALERTS, alerts)


def build_trade_plan(metrics):
    price = safe_float(metrics.get("price"))
    atr = safe_float(metrics.get("atr"))
    vwap = safe_float(metrics.get("vwap"))
    resistance = safe_float(metrics.get("resistance"))
    if price <= 0 or atr <= 0:
        return None, "invalid price/ATR"

    swing_stop = price - max(atr * 1.1, price * 0.022)
    vwap_stop = vwap * 0.992 if vwap > 0 else swing_stop
    resistance_stop = resistance * 0.992 if resistance > 0 and metrics.get("breakout") else swing_stop
    max_loss_stop = price * (1 - MAX_STOP / 100)
    stop = max(min(swing_stop, vwap_stop, resistance_stop), max_loss_stop)
    risk = price - stop
    if risk <= 0:
        return None, "invalid stop"

    t1 = price + max(atr * 1.2, risk * 1.25)
    t2 = price + max(atr * 2.2, risk * 2.0)
    t3 = price + max(atr * 3.5, risk * 3.0)
    reward_risk = (t1 - price) / risk
    if reward_risk < 1.2:
        return None, "weak reward/risk"

    return {
        "symbol": metrics["symbol"],
        "entry": price,
        "initial_stop": stop,
        "stop": stop,
        "stop_distance_pct": (risk / price) * 100,
        "t1": t1,
        "t2": t2,
        "t3": t3,
        "reward_risk": reward_risk,
        "hit_t1": False,
        "hit_t2": False,
        "hit_t3": False,
        "status": "active",
        "created_at": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
        "date": today_ksa(),
    }, "OK"


def final_safety_check(metrics, watch_item):
    """Rebuild fresh metrics and trade plan, then run last-second safety checks."""
    symbol = metrics["symbol"]
    snapshot = get_snapshots_batch([symbol]).get(symbol)
    if not snapshot:
        return False, "تعذر قراءة السعر النهائي", None, None

    refreshed_metrics, status = evaluate_news_entry(symbol, watch_item, snapshot=snapshot)
    if not refreshed_metrics or not refreshed_metrics.get("entry_ready"):
        return False, f"لم يعد الدخول جاهزًا: {status}", None, None

    price = safe_float(refreshed_metrics.get("price"))
    spread = safe_float(refreshed_metrics.get("spread_pct"), 999)
    if spread > MAX_SPREAD:
        return False, "السبريد توسع", None, None
    if price < safe_float(refreshed_metrics.get("vwap")):
        return False, "كسر VWAP", None, None

    resistance = safe_float(refreshed_metrics.get("resistance"))
    if resistance > 0:
        above_pct = ((price - resistance) / resistance) * 100
        if above_pct > 3.0:
            return False, f"الدخول متأخر {above_pct:.2f}% فوق المقاومة", None, None

    df = get_bars(symbol, TimeFrame.Minute, limit=390, cache_ttl=0)
    df = completed_indicator_df(df)
    if df.empty or len(df) < 40:
        return False, "تعذر تحديث الشموع المكتملة", None, None

    last_close = safe_float(df["close"].iloc[-1])
    last_open = safe_float(df["open"].iloc[-1])
    last_low = safe_float(df["low"].iloc[-1])
    if last_close < last_open and last_close <= last_low * 1.01:
        return False, "آخر شمعة مكتملة ضعيفة جدًا", None, None
    if resistance > 0 and last_close < resistance * 0.997:
        return False, "فقد منطقة المقاومة قبل الإرسال", None, None

    refreshed_plan, reason = build_trade_plan(refreshed_metrics)
    if not refreshed_plan:
        return False, reason, None, None
    return True, "OK", refreshed_metrics, refreshed_plan


def build_alert_message(metrics, plan):
    reasons = "\n".join(f"• {html.escape(str(x))}" for x in metrics.get("reasons", [])[:7])
    warnings = ""
    if metrics.get("warnings"):
        warnings = "\n⚠️ <b>ملاحظات:</b>\n" + "\n".join(f"• {html.escape(str(x))}" for x in metrics.get("warnings", [])[:5])
    headline = html.escape(str(metrics.get("news_headline") or "خبر إيجابي حديث"))
    source = html.escape(str(metrics.get("news_source") or "Finnhub"))

    return f"""📰🚀 <b>{BOT_NAME_AR} - دخول الآن</b>

📈 <b>السهم:</b> {metrics['symbol']}
⭐ <b>درجة الدخول:</b> {safe_float(metrics.get('final_score')):.1f}/100
🧨 <b>قوة المحفز:</b> {safe_float(metrics.get('catalyst_score')):.0f}/100

━━━━━━━━━━━━━━

📰 <b>الخبر المحفز:</b>
{headline}

🏷 <b>المصدر:</b> {source}
🕒 <b>عمر الخبر عند اكتشافه:</b> {safe_float(metrics.get('news_age_minutes')):.0f} دقيقة

━━━━━━━━━━━━━━

✅ <b>التأكيد الفني:</b>
{reasons}
{warnings}

━━━━━━━━━━━━━━

💰 <b>الدخول الآن:</b> {fmt_price(plan.get('entry'))}
🛑 <b>وقف الخسارة:</b> {fmt_price(plan.get('stop'))} ({safe_float(plan.get('stop_distance_pct')):.2f}%)
🎯 <b>الهدف الأول:</b> {fmt_price(plan.get('t1'))}
🎯 <b>الهدف الثاني:</b> {fmt_price(plan.get('t2'))}
🎯 <b>الهدف الثالث:</b> {fmt_price(plan.get('t3'))}
📊 <b>العائد/المخاطرة:</b> {safe_float(plan.get('reward_risk')):.2f}

━━━━━━━━━━━━━━

📌 <b>بيانات الحركة:</b>
RVOL: {safe_float(metrics.get('rvol')):.2f}
تسارع الحجم: {safe_float(metrics.get('volume_accel_ratio')):.2f}x
ATR: {safe_float(metrics.get('atr_pct')):.2f}%
الفلوت: {metrics.get('float_label')}
السبريد: {safe_float(metrics.get('spread_pct')):.2f}%
"""


def activate_trade(metrics, plan):
    symbol = metrics["symbol"]
    item = {
        "symbol": symbol,
        "metrics": metrics,
        "trade_plan": plan,
        "highest_price": plan["entry"],
        "lowest_price": plan["entry"],
        "opened_at": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
        "date": today_ksa(),
    }
    with ACTIVE_TRADES_LOCK:
        ACTIVE_TRADES[symbol] = item
    redis_hset_json(KEY_ACTIVE, symbol, item)
    runtime_stats["active_trades"] = len(ACTIVE_TRADES)


def send_catalyst_alert(metrics):
    symbol = metrics["symbol"]
    if already_alerted_today(symbol):
        remove_from_news_watchlist(symbol, "already alerted today")
        return False

    watch_item = NEWS_WATCHLIST.get(symbol)
    if not watch_item:
        return False

    # Re-check Finnhub immediately before alerting. This bypasses the hourly cache.
    if NEWS_REVALIDATE_BEFORE_ALERT:
        refreshed_news = analyze_symbol_news(symbol, force_refresh=True)
        refreshed_best = refreshed_news.get("best", {})
        if refreshed_best.get("serious_negative") or refreshed_best.get("blocked_by_negative"):
            redis_hset_json(KEY_BLOCKED_NEWS, symbol, {
                "symbol": symbol,
                "headline": refreshed_best.get("headline", ""),
                "blocked_at": time.time(),
                "expires_at": time.time() + SERIOUS_NEGATIVE_TTL,
            })
            remove_from_news_watchlist(symbol, "negative news found in final recheck")
            log(f"Finalist rejected {symbol}: serious negative news found before alert")
            return False
        if not refreshed_best.get("positive") or safe_float(refreshed_best.get("score")) < MIN_CATALYST_SCORE:
            log(f"Finalist rejected {symbol}: catalyst no longer qualifies after news recheck")
            return False
        watch_item.update({
            "news_id": refreshed_best.get("news_id", watch_item.get("news_id")),
            "headline": refreshed_best.get("headline", watch_item.get("headline", "")),
            "summary": refreshed_best.get("summary", watch_item.get("summary", "")),
            "source": refreshed_best.get("source", watch_item.get("source", "")),
            "url": refreshed_best.get("url", watch_item.get("url", "")),
            "news_timestamp": refreshed_best.get("datetime", watch_item.get("news_timestamp", 0)),
            "news_age_minutes": refreshed_best.get("age_minutes", watch_item.get("news_age_minutes", 0)),
            "catalyst_score": refreshed_best.get("score", watch_item.get("catalyst_score", 0)),
            "catalyst_category": refreshed_best.get("category", watch_item.get("catalyst_category")),
            "catalyst_reasons": refreshed_best.get("reasons", watch_item.get("catalyst_reasons", [])),
            "major_catalyst": bool(refreshed_best.get("major")),
        })
        with WATCHLIST_LOCK:
            NEWS_WATCHLIST[symbol] = watch_item
        redis_hset_json(KEY_NEWS_WATCHLIST, symbol, watch_item)

    # Rebuild all execution metrics from fresh market data immediately before alerting.
    fresh_snapshot = get_snapshots_batch([symbol]).get(symbol)
    fresh_metrics, fresh_status = evaluate_news_entry(symbol, watch_item, snapshot=fresh_snapshot)
    if not fresh_metrics or not fresh_metrics.get("entry_ready"):
        log(f"Finalist no longer ready {symbol}: {fresh_status}")
        return False
    metrics = fresh_metrics

    ok, reason, refreshed_metrics, plan = final_safety_check(metrics, watch_item)
    if not ok:
        log(f"Final safety rejected {symbol}: {reason}")
        return False
    metrics = refreshed_metrics
    if not send_telegram(build_alert_message(metrics, plan)):
        return False
    save_sent_alert(symbol, metrics)
    redis_hset_json(KEY_HISTORY, f"{symbol}:open:{int(time.time())}", {
        "symbol": symbol, "metrics": metrics, "trade_plan": plan,
        "date": today_ksa(), "time": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
    })
    activate_trade(metrics, plan)
    remove_from_news_watchlist(symbol, "alert sent")
    runtime_stats["alerts_sent"] += 1
    log(f"Catalyst alert sent: {symbol} score={metrics.get('final_score'):.1f}")
    return True

# ==============================================================================
# News Discovery Loop
# ==============================================================================

def load_news_cache():
    global NEWS_CACHE
    now_ts = time.time()
    key_type = redis_type(KEY_NEWS_CACHE)
    raw = {}

    if key_type == "hash":
        raw = redis_hgetall_json(KEY_NEWS_CACHE)
    elif key_type == "string":
        # One-time migration from the old monolithic JSON cache to a Redis hash.
        old_cache = redis_get_json(KEY_NEWS_CACHE, {}) or {}
        redis_delete(KEY_NEWS_CACHE)
        for symbol, item in old_cache.items():
            if isinstance(item, dict):
                redis_hset_json(KEY_NEWS_CACHE, symbol, item)
        raw = old_cache
        log(f"Migrated news cache to Redis hash: {len(raw)} records")

    NEWS_CACHE = {}
    for symbol, item in raw.items():
        checked_at = safe_float(item.get("checked_at")) if isinstance(item, dict) else 0
        if isinstance(item, dict) and checked_at > 0 and now_ts - checked_at <= NEWS_RECHECK_TTL:
            NEWS_CACHE[symbol] = item
        else:
            redis_hdel(KEY_NEWS_CACHE, symbol)


def process_news_symbol(symbol):
    runtime_stats["news_symbols_checked"] += 1
    runtime_stats["last_news_scan"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    if is_symbol_news_blocked(symbol):
        return
    result = analyze_symbol_news(symbol)
    best = result.get("best", {})

    if best.get("serious_negative") or best.get("blocked_by_negative"):
        redis_hset_json(KEY_BLOCKED_NEWS, symbol, {
            "symbol": symbol,
            "headline": best.get("headline", ""),
            "blocked_at": time.time(),
            "expires_at": time.time() + SERIOUS_NEGATIVE_TTL,
        })
        if symbol in NEWS_WATCHLIST:
            remove_from_news_watchlist(symbol, "serious negative news")
        return

    if best.get("positive") and safe_float(best.get("score")) >= MIN_CATALYST_SCORE:
        add_to_news_watchlist(symbol, result)


def news_discovery_loop():
    global NEWS_CURSOR
    while True:
        try:
            if is_weekend() or not is_scan_window():
                time.sleep(30)
                continue
            if not UNIVERSE:
                time.sleep(10)
                continue
            if NEWS_CURSOR >= len(UNIVERSE):
                NEWS_CURSOR = 0
            symbol = UNIVERSE[NEWS_CURSOR]
            NEWS_CURSOR = (NEWS_CURSOR + 1) % len(UNIVERSE)
            process_news_symbol(symbol)
            if runtime_stats["news_symbols_checked"] % 40 == 0:
                log(
                    f"News rotation | checked={runtime_stats['news_symbols_checked']} | "
                    f"cursor={NEWS_CURSOR}/{len(UNIVERSE)} | watch={len(NEWS_WATCHLIST)}"
                )
            redis_set_json(KEY_RUNTIME, runtime_stats)
        except Exception as exc:
            log(f"News discovery loop error: {exc}")
            log(traceback.format_exc())
            time.sleep(5)

# ==============================================================================
# News Watch Monitor Loop
# ==============================================================================

def monitor_news_watchlist_once():
    cleanup_news_watchlist()
    with WATCHLIST_LOCK:
        symbols = list(NEWS_WATCHLIST.keys())
    if not symbols:
        return

    snapshots = get_snapshots_batch(symbols)
    for symbol in symbols:
        try:
            item = NEWS_WATCHLIST.get(symbol)
            if not item:
                continue
            metrics, status = evaluate_news_entry(symbol, item, snapshot=snapshots.get(symbol))
            runtime_stats["watchlist_checks"] += 1
            runtime_stats["last_watch_scan"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
            item["last_checked_ts"] = time.time()
            item["last_market_status"] = status
            if metrics:
                item["best_entry_score"] = max(
                    safe_float(item.get("best_entry_score")),
                    safe_float(metrics.get("final_score")),
                )
                item["last_metrics"] = {
                    "price": metrics.get("price"),
                    "rvol": metrics.get("rvol"),
                    "accel": metrics.get("volume_accel_ratio"),
                    "score": metrics.get("final_score"),
                    "entry_ready": metrics.get("entry_ready"),
                }
            redis_hset_json(KEY_NEWS_WATCHLIST, symbol, item)
            with WATCHLIST_LOCK:
                NEWS_WATCHLIST[symbol] = item

            if metrics and metrics.get("entry_ready"):
                send_catalyst_alert(metrics)
        except Exception as exc:
            log(f"News watch error {symbol}: {exc}")

    runtime_stats["news_watchlist_size"] = len(NEWS_WATCHLIST)
    redis_set_json(KEY_RUNTIME, runtime_stats)


def news_watch_loop():
    while True:
        cycle_started = time.monotonic()
        try:
            if not is_weekend() and is_scan_window():
                monitor_news_watchlist_once()
        except Exception as exc:
            log(f"News watch loop error: {exc}")
            log(traceback.format_exc())
        elapsed = time.monotonic() - cycle_started
        time.sleep(max(0.25, NEWS_WATCH_MONITOR_INTERVAL - elapsed))

# ==============================================================================
# Active Trade Monitor
# ==============================================================================

def load_active_trades():
    global ACTIVE_TRADES
    ACTIVE_TRADES = redis_hgetall_json(KEY_ACTIVE)
    runtime_stats["active_trades"] = len(ACTIVE_TRADES)


def close_active_trade(symbol, item, reason):
    item["close_reason"] = reason
    item["closed_at"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    item["trade_plan"]["status"] = "closed"
    redis_hset_json(KEY_HISTORY, f"{symbol}:closed:{int(time.time())}", item)
    redis_hdel(KEY_ACTIVE, symbol)
    with ACTIVE_TRADES_LOCK:
        ACTIVE_TRADES.pop(symbol, None)
    runtime_stats["active_trades"] = len(ACTIVE_TRADES)


def send_trade_update(symbol, text, exit_message=False):
    icon = "🔴" if exit_message else "🟢"
    title = "خروج / تحذير" if exit_message else "تحديث صفقة"
    send_telegram(f"{icon} <b>{BOT_NAME_AR} - {title}</b>\n\n📈 <b>السهم:</b> {symbol}\n\n{text}")


def monitor_single_trade(symbol, item):
    plan = item.get("trade_plan", {})
    metrics = item.get("metrics", {})
    snapshot = get_snapshots_batch([symbol]).get(symbol)
    if not snapshot:
        return
    price = safe_float(snapshot.get("price"))
    entry = safe_float(plan.get("entry"))
    stop = safe_float(plan.get("stop"))
    if price <= 0:
        return

    item["highest_price"] = max(safe_float(item.get("highest_price"), entry), price)
    item["lowest_price"] = min(safe_float(item.get("lowest_price"), entry), price)

    if stop > 0 and price <= stop:
        send_trade_update(symbol, f"💰 السعر: {fmt_price(price)}\n🛑 تم كسر الوقف: {fmt_price(stop)}", True)
        close_active_trade(symbol, item, "stop_hit")
        return

    messages = []
    changed = False
    for target_name, label, stop_builder in [
        ("t1", "الهدف الأول T1", lambda: max(safe_float(plan.get("stop")), entry * 1.002)),
        ("t2", "الهدف الثاني T2", lambda: max(safe_float(plan.get("stop")), price - max(safe_float(metrics.get("atr")), price * 0.02))),
        ("t3", "الهدف الثالث T3", lambda: max(safe_float(plan.get("stop")), price - max(safe_float(metrics.get("atr")) * 0.8, price * 0.018))),
    ]:
        hit_key = f"hit_{target_name}"
        target = safe_float(plan.get(target_name))
        if target > 0 and price >= target and not plan.get(hit_key):
            plan[hit_key] = True
            plan["stop"] = stop_builder()
            messages.append(f"🎯 وصل {label}\n💰 السعر: {fmt_price(price)}\n🛑 الوقف الجديد: {fmt_price(plan['stop'])}")
            changed = True

    # Keep trailing after T3 instead of stopping updates at the target.
    if plan.get("hit_t3"):
        atr_value = safe_float(metrics.get("atr"))
        highest = safe_float(item.get("highest_price"), price)
        trailing_candidate = highest - max(atr_value * 0.8, highest * 0.018)
        if trailing_candidate > safe_float(plan.get("stop")):
            plan["stop"] = trailing_candidate
            changed = True

    df = get_bars(symbol, TimeFrame.Minute, limit=390, cache_ttl=15)
    df = current_ny_session_df(df)
    if not df.empty and len(df) >= 30:
        vwap = calculate_vwap(df)
        rvol = calculate_rvol(df)
        accel = safe_float(calculate_volume_acceleration(df).get("ratio"))

        if not plan.get("hit_t1"):
            vwap_failed = bool(vwap > 0 and price < vwap)
            volume_dead = bool(rvol < 1.3 and accel < 0.9)

            item["vwap_failure_count"] = safe_int(item.get("vwap_failure_count")) + 1 if vwap_failed else 0
            item["volume_death_count"] = safe_int(item.get("volume_death_count")) + 1 if volume_dead else 0

            if item["vwap_failure_count"] >= VWAP_FAILURE_CONFIRMATIONS:
                send_trade_update(symbol, f"💰 السعر: {fmt_price(price)}\n📉 تأكد كسر VWAP قبل T1 عبر {item['vwap_failure_count']} فحصين متتاليين.", True)
                close_active_trade(symbol, item, "confirmed_vwap_failure_before_t1")
                return
            if item["volume_death_count"] >= VOLUME_DEATH_CONFIRMATIONS:
                send_trade_update(symbol, f"💰 السعر: {fmt_price(price)}\n📉 تأكد انهيار الزخم والحجم قبل T1 عبر {item['volume_death_count']} فحصين متتاليين.", True)
                close_active_trade(symbol, item, "confirmed_volume_death_before_t1")
                return
        else:
            item["vwap_failure_count"] = 0
            item["volume_death_count"] = 0

    if messages:
        send_trade_update(symbol, "\n\n".join(messages))
    item["trade_plan"] = plan
    redis_hset_json(KEY_ACTIVE, symbol, item)
    with ACTIVE_TRADES_LOCK:
        ACTIVE_TRADES[symbol] = item


def trade_monitor_loop():
    while True:
        try:
            with ACTIVE_TRADES_LOCK:
                trades = list(ACTIVE_TRADES.items())
            for symbol, item in trades:
                monitor_single_trade(symbol, item)
        except Exception as exc:
            log(f"Trade monitor error: {exc}")
        time.sleep(TRADE_MONITOR_INTERVAL)


# ==============================================================================
# Periodic Redis / State Cleanup
# ============================================================================== 

def cleanup_news_redis_and_blocks():
    now_ts = time.time()

    for symbol, item in list(NEWS_CACHE.items()):
        checked_at = safe_float(item.get("checked_at")) if isinstance(item, dict) else 0
        if checked_at <= 0 or now_ts - checked_at > 7 * 24 * 60 * 60:
            NEWS_CACHE.pop(symbol, None)
            redis_hdel(KEY_NEWS_CACHE, symbol)

    blocked = redis_hgetall_json(KEY_BLOCKED_NEWS)
    for symbol, item in blocked.items():
        if safe_float(item.get("expires_at")) <= now_ts:
            redis_hdel(KEY_BLOCKED_NEWS, symbol)


def should_close_end_of_day():
    ny = now_ny()
    if ny.weekday() >= 5:
        return False
    cutoff = ny.replace(hour=END_OF_DAY_CLOSE_HOUR_NY, minute=END_OF_DAY_CLOSE_MINUTE_NY, second=0, microsecond=0)
    return ny >= cutoff


def cleanup_stale_active_trades():
    now_ts = time.time()
    for symbol, item in list(ACTIVE_TRADES.items()):
        opened_at = item.get("opened_at")
        opened_ts = 0.0
        try:
            opened_ts = datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=saudi_tz).timestamp()
        except Exception:
            opened_ts = 0.0
        stale = opened_ts <= 0 or now_ts - opened_ts > STALE_ACTIVE_TRADE_SECONDS
        end_of_day = should_close_end_of_day()
        if end_of_day:
            close_active_trade(symbol, item, "end_of_day_cleanup")
            log(f"Closed active trade at end of day: {symbol}")
        elif stale:
            close_active_trade(symbol, item, "stale_trade_cleanup")
            log(f"Removed stale active trade: {symbol}")

# ==============================================================================
# Schedulers / Startup
# ==============================================================================

last_universe_refresh = 0.0
last_scheduled_rebuild_key = ""
last_float_reload_key = ""
last_redis_cleanup_ts = 0.0


def maybe_refresh_universe():
    global last_universe_refresh, last_scheduled_rebuild_key
    current = now_ksa()
    current_key = current.strftime("%Y-%m-%d %H:%M")
    current_hm = current.strftime("%H:%M")
    scheduled = current_hm in FULL_UNIVERSE_REFRESH_TIMES_KSA and current_key != last_scheduled_rebuild_key
    expired = time.time() - last_universe_refresh >= UNIVERSE_REFRESH_INTERVAL
    if not UNIVERSE or scheduled or expired:
        build_clean_universe()
        last_universe_refresh = time.time()
        if scheduled:
            last_scheduled_rebuild_key = current_key


def maybe_reload_float():
    global last_float_reload_key
    current = now_ksa()
    if current.hour == FLOAT_RELOAD_HOUR_KSA and current.minute == FLOAT_RELOAD_MINUTE_KSA:
        key = current.strftime("%Y-%m-%d %H:%M")
        if key != last_float_reload_key:
            load_float_cache()
            last_float_reload_key = key


def startup_message():
    send_telegram(f"""📰🚀 <b>{BOT_NAME_AR} بدأ التشغيل</b>

🧩 الوضع: News-Driven Catalyst Radar
📰 مصدر الاكتشاف: Finnhub News
⚡ حد الأخبار: {FINNHUB_MAX_REQUESTS_PER_MINUTE} طلب/دقيقة
👀 فحص قائمة الأخبار: كل {NEWS_WATCH_MONITOR_INTERVAL} ثوانٍ
📥 سجلات الفلوت: {len(FLOAT_CACHE)}
📦 Universe: {len(UNIVERSE)}
📰 NEWS_WATCHLIST: {len(NEWS_WATCHLIST)}

🕒 الوقت: {now_ksa().strftime('%Y-%m-%d %H:%M:%S')}
""")


def startup():
    log("======================================")
    log(f"{BOT_NAME} Starting...")
    log("Primary discovery: Finnhub positive catalysts")
    log("Technical confirmation: Alpaca market data")
    log("======================================")

    saved_runtime = redis_get_json(KEY_RUNTIME, {}) or {}
    for key, value in saved_runtime.items():
        if key in runtime_stats:
            runtime_stats[key] = value

    load_float_cache()
    load_news_cache()
    load_universe()
    load_news_watchlist()
    load_active_trades()
    cleanup_news_redis_and_blocks()
    cleanup_stale_active_trades()
    startup_message()
    log("Startup completed.")

# ==============================================================================
# Main
# ==============================================================================

def main_loop():
    global last_redis_cleanup_ts
    startup()

    threading.Thread(target=news_discovery_loop, daemon=True, name="news-discovery").start()
    threading.Thread(target=news_watch_loop, daemon=True, name="news-watch").start()
    threading.Thread(target=trade_monitor_loop, daemon=True, name="trade-monitor").start()

    while True:
        try:
            maybe_reload_float()
            if not is_weekend():
                maybe_refresh_universe()
            else:
                log("Weekend mode. News discovery and entry alerts paused.")
            if time.time() - last_redis_cleanup_ts >= REDIS_CLEANUP_INTERVAL:
                cleanup_news_redis_and_blocks()
                cleanup_news_watchlist()
                cleanup_stale_active_trades()
                last_redis_cleanup_ts = time.time()
            runtime_stats["news_watchlist_size"] = len(NEWS_WATCHLIST)
            runtime_stats["active_trades"] = len(ACTIVE_TRADES)
            redis_set_json(KEY_RUNTIME, runtime_stats)
            time.sleep(60)
        except KeyboardInterrupt:
            log(f"{BOT_NAME} stopped manually.")
            break
        except Exception as exc:
            log(f"Main loop error: {exc}")
            log(traceback.format_exc())
            time.sleep(30)


if __name__ == "__main__":
    main_loop()
