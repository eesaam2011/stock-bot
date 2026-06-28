# ==============================================================================
# Elite Radar
# Version : 1.0
# File    : elite_radar_bot.py
# Author  : OpenAI + Essam
#
# Deployment:
#   Render Background Worker
#
# Start Command:
#   python elite_radar_bot.py
# ==============================================================================

import os
import json
try:
    import orjson
except Exception:
    orjson = None
import math
import time
import pytz
import requests
import threading
import traceback
import statistics
import random
import zoneinfo
import numpy as np
import pandas as pd

from datetime import datetime, timedelta, timezone
from collections import deque
from typing import Dict, List, Optional

import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import TimeFrame

# ==============================================================================
# Timezones
# ==============================================================================

saudi_tz = zoneinfo.ZoneInfo("Asia/Riyadh")
ny_tz = zoneinfo.ZoneInfo("America/New_York")
utc = timezone.utc

# ==============================================================================
# Environment Variables
# ==============================================================================

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv(
    "ALPACA_BASE_URL",
    "https://paper-api.alpaca.markets"
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

# Float Sources

FLOAT_CACHE_URL = os.getenv("FLOAT_CACHE_URL", "")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")
FLOAT_CACHE_FILENAME = os.getenv(
    "FLOAT_CACHE_FILENAME",
    "float_cache.json"
)

# ==============================================================================
# Runtime
# ==============================================================================

SCAN_INTERVAL = 30

MONITOR_INTERVAL = 30

FULL_UNIVERSE_REFRESH = 60 * 60 * 4

LIGHT_UNIVERSE_REFRESH = None

FULL_SNAPSHOT_REBUILD_TIMES_KSA = [
    "10:45",
    "16:20"
]

NEWS_CACHE_TTL = 60 * 60

POSITIVE_NEWS_TTL = 2 * 60 * 60
SERIOUS_NEGATIVE_NEWS_TTL = 72 * 60 * 60
MAJOR_CATALYST_NEWS_TTL = 6 * 60 * 60

NEWS_LOOKBACK_HOURS = 12

FINNHUB_MAX_REQUESTS_PER_MINUTE = 40

FINNHUB_DELAY = 60 / FINNHUB_MAX_REQUESTS_PER_MINUTE

PRICE_MIN = 0.50
PRICE_MAX = 25.00

MIN_SCORE = 88
LAST_HOUR_SCORE = 93

MAX_SPREAD = 2.0

MIN_DOLLAR_VOLUME = 500000

MAX_STOP = 6

WATCHLIST_RECHECK = 30

# ==============================================================================
# Redis Keys
# ==============================================================================

REDIS_PREFIX = "elite"

KEY_STATE = f"{REDIS_PREFIX}:state"

KEY_UNIVERSE = f"{REDIS_PREFIX}:universe"

KEY_PRIORITY = f"{REDIS_PREFIX}:priority"

KEY_WATCHLIST = f"{REDIS_PREFIX}:watchlist"

KEY_COMPRESSION = f"{REDIS_PREFIX}:compression"

KEY_ACTIVE = f"{REDIS_PREFIX}:active"

KEY_HISTORY = f"{REDIS_PREFIX}:history"

KEY_REJECTED = f"{REDIS_PREFIX}:rejected"

KEY_FLOAT = f"{REDIS_PREFIX}:float"

KEY_NEWS = f"{REDIS_PREFIX}:news"

KEY_RUNTIME = f"{REDIS_PREFIX}:runtime"

KEY_ALERTS = f"{REDIS_PREFIX}:alerts"

KEY_PATTERNS = f"{REDIS_PREFIX}:patterns"

# ==============================================================================
# Runtime Stats
# ==============================================================================

runtime_stats = {

    "started": datetime.now(saudi_tz),

    "total_scans": 0,

    "symbols_checked": 0,

    "alerts_sent": 0,

    "active_trades": 0,

    "float_records": 0,

    "last_scan": "Never",

    "last_universe": "Never"

}

# ==============================================================================
# Alpaca
# ==============================================================================

api = tradeapi.REST(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    api_version="v2"
)

# ==============================================================================
# Global Memory
# ==============================================================================

FLOAT_CACHE = {}

NEWS_CACHE = {}

WATCHLIST = {}

ACTIVE_TRADES = {}

UNIVERSE = []

PRIORITY_UNIVERSE = []

NORMAL_UNIVERSE = []

# ==============================================================================
# Logging
# ==============================================================================

def log(message):

    now = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{now}] {message}", flush=True)

def json_dumps(data):
    if orjson:
        return orjson.dumps(data).decode("utf-8")

    return json.dumps(data, ensure_ascii=False, default=str)


def json_loads(data):
    if orjson:
        return orjson.loads(data)

    return json.loads(data)
    
# ==============================================================================
# Telegram
# ==============================================================================

def send_telegram(message):

    try:

        requests.post(

            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",

            json={

                "chat_id": TELEGRAM_CHAT_ID,

                "text": message,

                "parse_mode": "HTML",

                "disable_web_page_preview": True

            },

            timeout=15

        )

    except Exception as e:

        log(e)

# ==============================================================================
# Startup
# ==============================================================================

log("======================================")

log("Elite Radar Starting...")

log("Background Worker Mode")

log("Loading Environment Variables...")

log("Preparing Redis...")

log("Preparing Float Manager...")

log("Preparing Universe Engine...")

log("Preparing Scan Engine...")

log("Preparing Trade Manager...")

log("======================================")
# ==============================================================================
# Redis Manager
# ==============================================================================

def redis_available():
    return bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)


def redis_command(command):
    if not redis_available():
        return None

    try:
        response = requests.post(
            UPSTASH_REDIS_REST_URL,
            headers={
                "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"
            },
            json=command,
            timeout=20
        )

        if response.status_code != 200:
            log(f"Redis Error {response.status_code}: {response.text[:200]}")
            return None

        return response.json().get("result")

    except Exception as e:
        log(f"Redis Exception: {e}")
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
    try:
        payload = json_dumps(value)
        
        if expire_seconds:
            return redis_command(["SET", key, payload, "EX", expire_seconds])

        return redis_command(["SET", key, payload])

    except Exception as e:
        log(f"Redis Set JSON Error: {e}")
        return None


def redis_hset_json(key, field, value):
    try:
        payload = json_dumps(value)
        return redis_command(["HSET", key, field, payload])
    except Exception as e:
        log(f"Redis HSET Error: {e}")
        return None


def redis_hgetall_json(key):
    result = redis_command(["HGETALL", key])

    output = {}

    if not result:
        return output

    try:
        for i in range(0, len(result), 2):
            field = result[i]
            value = result[i + 1]

            try:
                output[field] = json_loads(value)
            except Exception:
                output[field] = value

    except Exception as e:
        log(f"Redis HGETALL Parse Error: {e}")

    return output


def redis_hdel(key, field):
    return redis_command(["HDEL", key, field])


# ==============================================================================
# Utility Helpers
# ==============================================================================

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
        if value is None:
            return default

        return int(float(value))

    except Exception:
        return default


def fmt_price(value):
    value = safe_float(value)

    if value >= 1:
        return f"${value:.2f}"

    return f"${value:.4f}"


def fmt_pct(value):
    return f"{safe_float(value):.2f}%"


def fmt_big_number(value):
    value = safe_float(value)

    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"

    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"

    if value >= 1_000:
        return f"{value / 1_000:.1f}K"

    return f"{value:.0f}"


def now_ksa():
    return datetime.now(saudi_tz)


def now_ny():
    return datetime.now(ny_tz)


def today_ksa():
    return now_ksa().strftime("%Y-%m-%d")


def today_ny():
    return now_ny().strftime("%Y-%m-%d")


def is_weekend():
    return now_ny().weekday() >= 5

def is_market_weekday():
    return now_ny().weekday() < 5


def is_scan_window():
    if not is_market_weekday():
        return False

    current = now_ksa().time()

    start = datetime.strptime("11:00", "%H:%M").time()

    # من 11:00 صباحًا حتى 01:00 بعد منتصف الليل
    return (
        current >= start
        or current <= datetime.strptime("01:00", "%H:%M").time()
    )

def is_last_market_hour():
    ny = now_ny()

    if ny.weekday() >= 5:
        return False

    start = ny.replace(hour=15, minute=0, second=0, microsecond=0)
    end = ny.replace(hour=16, minute=0, second=0, microsecond=0)

    return start <= ny <= end


# ==============================================================================
# Symbol Filters
# ==============================================================================

SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS",
    "AXP", "USB", "TFC", "PNC", "COF", "DFS",
    "MET", "PRU", "ALL", "AIG", "AFL", "TRV", "HIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR",
    "BUD", "TAP", "STZ", "DEO", "PM", "MO", "BTI",
    "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL", "AMC", "CNK", "IMAX", "HITI"
}


BAD_NAME_KEYWORDS = [
    "ETF",
    "ETN",
    "FUND",
    "TRUST",
    "INDEX",
    "WARRANT",
    "UNIT",
    "RIGHT",
    "SPAC",
    "ACQUISITION",
    "BLANK CHECK",
    "PREFERRED",
    "NOTE",
    "BOND",
    "DEBT",
    "2X",
    "3X",
    "ULTRA",
    "INVERSE",
    "BEAR",
    "BULL"
]


def is_clean_symbol(symbol):
    if not symbol:
        return False

    symbol = symbol.upper().strip()

    if len(symbol) > 5:
        return False

    if not symbol.isalpha():
        return False

    if "." in symbol or "-" in symbol or "/" in symbol or "^" in symbol:
        return False

    if symbol in SYMBOL_BLACKLIST:
        return False

    if len(symbol) >= 5 and symbol[-1] in ["W", "U", "R", "P", "Q", "Z"]:
        return False

    return True


def is_bad_asset_name(name):
    if not name:
        return False

    name = name.upper()

    for keyword in BAD_NAME_KEYWORDS:
        if keyword in name:
            return True

    return False


# ==============================================================================
# Float Manager
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
            float_value = (
                value.get("float")
                or value.get("share_float")
                or value.get("floatShares")
                or value.get("value")
            )
        else:
            float_value = value

        float_value = safe_float(float_value)

        if float_value > 0:
            normalized[symbol] = float_value

    return normalized


def load_float_from_url():
    if not FLOAT_CACHE_URL:
        return {}

    try:
        log("Loading float cache from FLOAT_CACHE_URL...")

        response = requests.get(FLOAT_CACHE_URL, timeout=30)

        if response.status_code != 200:
            log(f"FLOAT_CACHE_URL failed: {response.status_code}")
            return {}

        data = response.json()

        return normalize_float_cache(data)

    except Exception as e:
        log(f"FLOAT_CACHE_URL Error: {e}")
        return {}


def load_float_from_gist():
    if not GITHUB_TOKEN or not GIST_ID:
        return {}

    try:
        log("Loading float cache from GitHub Gist...")

        response = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"
            },
            timeout=30
        )

        if response.status_code != 200:
            log(f"Gist Float Load Failed: {response.status_code}")
            return {}

        gist_data = response.json()

        files = gist_data.get("files", {})

        file_data = files.get(FLOAT_CACHE_FILENAME)

        if not file_data:
            log(f"{FLOAT_CACHE_FILENAME} not found in Gist.")
            return {}

        content = file_data.get("content", "{}")

        raw_data = json.loads(content)

        return normalize_float_cache(raw_data)

    except Exception as e:
        log(f"Gist Float Error: {e}")
        return {}


def load_float_from_redis():
    data = redis_get_json(KEY_FLOAT, {})

    return normalize_float_cache(data)


def save_float_to_redis():
    redis_set_json(KEY_FLOAT, FLOAT_CACHE)


def load_float_cache():
    global FLOAT_CACHE

    log("Loading float cache...")

    data = {}

    data = load_float_from_url()

    if not data:
        data = load_float_from_gist()

    if not data:
        data = load_float_from_redis()

    FLOAT_CACHE = data or {}

    runtime_stats["float_records"] = len(FLOAT_CACHE)

    if FLOAT_CACHE:
        save_float_to_redis()

    log(f"Float records loaded: {len(FLOAT_CACHE)}")

    return FLOAT_CACHE


def get_float(symbol):
    return FLOAT_CACHE.get(symbol.upper())


def get_float_score(symbol):
    float_value = get_float(symbol)

    if not float_value:
        return 0

    if float_value <= 5_000_000:
        return 10

    if float_value <= 15_000_000:
        return 8

    if float_value <= 30_000_000:
        return 6

    if float_value <= 50_000_000:
        return 4

    if float_value <= 100_000_000:
        return 2

    return 0


def get_float_label(symbol):
    float_value = get_float(symbol)

    if not float_value:
        return "غير متوفر"

    if float_value <= 5_000_000:
        return f"منخفض جدًا ({fmt_big_number(float_value)})"

    if float_value <= 15_000_000:
        return f"منخفض ({fmt_big_number(float_value)})"

    if float_value <= 30_000_000:
        return f"جيد ({fmt_big_number(float_value)})"

    if float_value <= 50_000_000:
        return f"مقبول ({fmt_big_number(float_value)})"

    if float_value <= 100_000_000:
        return f"مرتفع نسبيًا ({fmt_big_number(float_value)})"

    return f"مرتفع ({fmt_big_number(float_value)})"


# ==============================================================================
# Finnhub News Manager - Max 40 Requests / Minute
# ==============================================================================

SERIOUS_NEGATIVE_NEWS = [
    "offering",
    "registered direct",
    "private placement",
    "atm offering",
    "reverse split",
    "delisting",
    "nasdaq non-compliance",
    "bankruptcy",
    "chapter 11",
    "going concern",
    "shelf registration",
    "public offering"
]


MINOR_NEGATIVE_NEWS = [
    "lawsuit",
    "investigation",
    "sec investigation",
    "class action",
    "downgrade",
    "resignation",
    "termination",
    "withdraws guidance"
]


POSITIVE_NEWS = [
    "approval",
    "fda",
    "contract",
    "partnership",
    "award",
    "patent",
    "merger",
    "acquisition",
    "strategic",
    "collaboration",
    "purchase order",
    "record revenue",
    "positive data",
    "phase",
    "launch",
    "breakthrough"
]


LAST_FINNHUB_REQUEST_TIME = 0


def finnhub_wait_slot():
    global LAST_FINNHUB_REQUEST_TIME

    elapsed = time.time() - LAST_FINNHUB_REQUEST_TIME

    if elapsed < FINNHUB_DELAY:
        time.sleep(FINNHUB_DELAY - elapsed)

    LAST_FINNHUB_REQUEST_TIME = time.time()


def load_news_cache():
    global NEWS_CACHE

    NEWS_CACHE = redis_get_json(KEY_NEWS, {}) or {}

    return NEWS_CACHE


def save_news_cache():
    redis_set_json(KEY_NEWS, NEWS_CACHE)


def classify_news_text(text):
    text = (text or "").lower()

    serious_negative = any(word in text for word in SERIOUS_NEGATIVE_NEWS)
    minor_negative = any(word in text for word in MINOR_NEGATIVE_NEWS)
    positive = any(word in text for word in POSITIVE_NEWS)

    bonus = 0

    if positive and not serious_negative:
        bonus += 5

    if minor_negative:
        bonus -= 10

    return {
        "positive": positive,
        "minor_negative": minor_negative,
        "serious_negative": serious_negative,
        "bonus": bonus
    }

def get_symbol_news(symbol):
    if not FINNHUB_API_KEY:
        return {
            "status": "no_key",
            "positive": False,
            "minor_negative": False,
            "serious_negative": False,
            "bonus": 0,
            "headline": "",
            "category": "none",
            "cached": False
        }

    symbol = symbol.upper()

    cached = NEWS_CACHE.get(symbol)

    if cached:
        cached_at = cached.get("cached_at", 0)
        ttl = cached.get("ttl", NEWS_CACHE_TTL)

        if time.time() - cached_at <= ttl:
            data = cached.get("data", {})
            data["cached"] = True
            return data

    try:
        finnhub_wait_slot()

        to_date = datetime.utcnow().date()
        from_date = to_date - timedelta(hours=NEWS_LOOKBACK_HOURS)

        response = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": from_date.strftime("%Y-%m-%d"),
                "to": to_date.strftime("%Y-%m-%d"),
                "token": FINNHUB_API_KEY
            },
            timeout=15
        )

        if response.status_code != 200:
            data = {
                "status": f"error_{response.status_code}",
                "positive": False,
                "minor_negative": False,
                "serious_negative": False,
                "bonus": 0,
                "headline": "",
                "category": "error",
                "cached": False
            }

            ttl = NEWS_CACHE_TTL

        else:
            items = response.json()

            if not isinstance(items, list):
                items = []

            items = items[:10]

            combined_text = " ".join(
                [
                    f"{item.get('headline', '')} {item.get('summary', '')}"
                    for item in items
                ]
            )

            result = classify_news_text(combined_text)

            headline = items[0].get("headline", "") if items else ""

            text_lower = combined_text.lower()

            category = "neutral"
            ttl = NEWS_CACHE_TTL

            if result["serious_negative"]:
                category = "serious_negative"
                ttl = SERIOUS_NEGATIVE_NEWS_TTL

            elif result["positive"]:
                category = "positive"
                ttl = POSITIVE_NEWS_TTL

                major_words = [
                    "fda",
                    "approval",
                    "contract",
                    "purchase order",
                    "merger",
                    "acquisition",
                    "breakthrough",
                    "positive data"
                ]

                if any(word in text_lower for word in major_words):
                    category = "major_catalyst"
                    ttl = MAJOR_CATALYST_NEWS_TTL

            elif result["minor_negative"]:
                category = "minor_negative"
                ttl = 24 * 60 * 60

            data = {
                "status": "ok",
                "positive": result["positive"],
                "minor_negative": result["minor_negative"],
                "serious_negative": result["serious_negative"],
                "bonus": result["bonus"],
                "headline": headline,
                "category": category,
                "cached": False
            }

        NEWS_CACHE[symbol] = {
            "cached_at": time.time(),
            "ttl": ttl,
            "data": data
        }

        save_news_cache()

        return data

    except Exception as e:
        log(f"Finnhub error for {symbol}: {e}")

        return {
            "status": "exception",
            "positive": False,
            "minor_negative": False,
            "serious_negative": False,
            "bonus": 0,
            "headline": "",
            "category": "exception",
            "cached": False
        }
        
# ==============================================================================
# Alpaca Data Helpers
# ==============================================================================

BAR_CACHE = {}

TREND_15M_CACHE = {}


def bars_to_dataframe(bars):
    try:
        df = bars.df

        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index()

            if "timestamp" in df.columns:
                df = df.set_index("timestamp")

        df = df.copy()

        df.columns = [str(col).lower() for col in df.columns]

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["open", "high", "low", "close", "volume"])

        return df

    except Exception:
        rows = []

        try:
            for bar in bars:
                rows.append({
                    "timestamp": getattr(bar, "t", None),
                    "open": getattr(bar, "o", None),
                    "high": getattr(bar, "h", None),
                    "low": getattr(bar, "l", None),
                    "close": getattr(bar, "c", None),
                    "volume": getattr(bar, "v", None)
                })

            df = pd.DataFrame(rows)

            if "timestamp" in df.columns:
                df = df.set_index("timestamp")

            return df

        except Exception:
            return pd.DataFrame()


def get_bars(symbol, timeframe, limit=120, cache_ttl=60):
    key = f"{symbol}:{timeframe}:{limit}"

    cached = BAR_CACHE.get(key)

    if cached:
        cached_at, df = cached

        if time.time() - cached_at <= cache_ttl:
            return df.copy()

    try:
        bars = api.get_bars(
            symbol,
            timeframe,
            limit=limit
        )

        df = bars_to_dataframe(bars)

        BAR_CACHE[key] = (time.time(), df.copy())

        return df

    except Exception as e:
        log(f"get_bars error {symbol}: {e}")
        return pd.DataFrame()


def get_snapshot(symbol):
    try:
        snapshot = api.get_snapshot(symbol)

        latest_trade = getattr(snapshot, "latest_trade", None)
        latest_quote = getattr(snapshot, "latest_quote", None)
        daily_bar = getattr(snapshot, "daily_bar", None)
        prev_daily_bar = getattr(snapshot, "prev_daily_bar", None)
        minute_bar = getattr(snapshot, "minute_bar", None)

        price = safe_float(getattr(latest_trade, "p", None))

        bid = safe_float(getattr(latest_quote, "bp", None))
        ask = safe_float(getattr(latest_quote, "ap", None))

        day_volume = safe_float(getattr(daily_bar, "v", None))
        day_high = safe_float(getattr(daily_bar, "h", None))
        day_low = safe_float(getattr(daily_bar, "l", None))
        day_close = safe_float(getattr(daily_bar, "c", None))

        prev_close = safe_float(getattr(prev_daily_bar, "c", None))

        minute_volume = safe_float(getattr(minute_bar, "v", None))

        if price <= 0:
            price = day_close

        spread_pct = 999

        if price > 0 and ask > 0 and bid > 0:
            spread_pct = ((ask - bid) / price) * 100

        gap_pct = 0

        if price > 0 and prev_close > 0:
            gap_pct = ((price - prev_close) / prev_close) * 100

        dollar_volume = price * day_volume

        return {
            "symbol": symbol,
            "price": price,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "day_volume": day_volume,
            "minute_volume": minute_volume,
            "dollar_volume": dollar_volume,
            "prev_close": prev_close,
            "gap_pct": gap_pct,
            "day_high": day_high,
            "day_low": day_low
        }

    except Exception as e:
        log(f"Snapshot error {symbol}: {e}")

        return {
            "symbol": symbol,
            "price": 0,
            "bid": 0,
            "ask": 0,
            "spread_pct": 999,
            "day_volume": 0,
            "minute_volume": 0,
            "dollar_volume": 0,
            "prev_close": 0,
            "gap_pct": 0,
            "day_high": 0,
            "day_low": 0
        }


# ==============================================================================
# Indicator Engine
# ==============================================================================

def calculate_vwap(df):
    if df.empty:
        return 0

    required = ["high", "low", "close", "volume"]

    for col in required:
        if col not in df.columns:
            return 0

    typical_price = (df["high"] + df["low"] + df["close"]) / 3

    volume = df["volume"].clip(lower=0)

    total_volume = volume.sum()

    if total_volume <= 0:
        return 0

    return float((typical_price * volume).sum() / total_volume)


def calculate_obv(df):
    if df.empty or len(df) < 15:
        return {
            "obv": 0,
            "obv_ema": 0,
            "obv_rising": False
        }

    closes = df["close"].values
    volumes = df["volume"].values

    obv_values = [0]

    for i in range(1, len(df)):
        if closes[i] > closes[i - 1]:
            obv_values.append(obv_values[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv_values.append(obv_values[-1] - volumes[i])
        else:
            obv_values.append(obv_values[-1])

    obv_series = pd.Series(obv_values)

    obv_ema = obv_series.ewm(span=10, adjust=False).mean()

    obv = float(obv_series.iloc[-1])
    ema = float(obv_ema.iloc[-1])

    rising = (
        obv > ema
        and obv_series.iloc[-1] > obv_series.iloc[-3]
    )

    return {
        "obv": obv,
        "obv_ema": ema,
        "obv_rising": bool(rising)
    }


def calculate_atr(df, period=14):
    if df.empty or len(df) < period + 2:
        return 0

    high = df["high"]
    low = df["low"]
    close = df["close"]

    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            (high - low).abs(),
            (high - previous_close).abs(),
            (low - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    atr = true_range.rolling(period).mean().iloc[-1]

    return safe_float(atr)

def calculate_rvol(df):
    if df.empty or len(df) < 30:
        return 0

    volumes = df["volume"].astype(float)

    recent_volume = volumes.tail(5).mean()

    # نستبعد آخر 5 دقائق من المقارنة لأنها هي حركة الزخم الحالية
    historical_volume = volumes.iloc[:-5]

    if historical_volume.empty:
        return 0

    # نستخدم Median بدل Mean حتى لا تبالغ شمعة حجم شاذة في الحساب
    base_volume = historical_volume.tail(120).median()

    if not base_volume or base_volume <= 0:
        base_volume = historical_volume.mean()

    if not base_volume or base_volume <= 0:
        return 0

    instant_rvol = recent_volume / base_volume

    return float(instant_rvol)
    
def calculate_volume_acceleration(df):
    if df.empty or len(df) < 12:
        return {
            "ratio": 0,
            "trend_up": False,
            "peak_recent": False
        }

    volumes = df["volume"]

    last_3 = volumes.tail(3).mean()

    previous_7 = volumes.iloc[-10:-3].mean()

    if previous_7 <= 0:
        ratio = 0
    else:
        ratio = last_3 / previous_7

    trend_up = (
        volumes.iloc[-1] >= volumes.iloc[-2]
        or last_3 > previous_7 * 1.5
    )

    peak_recent = (
        volumes.tail(3).max()
        >= volumes.tail(20).max() * 0.80
    )

    return {
        "ratio": safe_float(ratio),
        "trend_up": bool(trend_up),
        "peak_recent": bool(peak_recent)
    }

def calculate_resistance(df, lookback=80):
    if df.empty or len(df) < 30:
        return {
            "resistance": 0,
            "distance_pct": 999,
            "breakout": False,
            "touches": 0
        }

    current_price = safe_float(df["close"].iloc[-1])

    recent = df.tail(lookback).copy()

    recent["body_high"] = recent[["open", "close"]].max(axis=1)

    body_levels = sorted(
        recent["body_high"].iloc[:-1].tolist(),
        reverse=True
    )

    if not body_levels:
        return {
            "resistance": 0,
            "distance_pct": 999,
            "breakout": False,
            "touches": 0
        }

    best_level = body_levels[0]
    best_touches = 0

    for i, level in enumerate(body_levels):
        tolerance = level * 0.003

        touches = sum(
            1
            for j, x in enumerate(body_levels)
            if j != i and abs(x - level) <= tolerance
        )

        if touches > best_touches:
            best_touches = touches
            best_level = level

    resistance = safe_float(best_level)

    if current_price > 0 and resistance > 0:
        distance_pct = (
            (resistance - current_price)
            / current_price
        ) * 100
    else:
        distance_pct = 999

    breakout = (
        current_price > resistance
        and df["close"].iloc[-1] > df["open"].iloc[-1]
        and best_touches >= 1
    )

    return {
        "resistance": resistance,
        "distance_pct": distance_pct,
        "breakout": bool(breakout),
        "touches": best_touches
    }
    
def get_15m_trend(symbol):
    cached = TREND_15M_CACHE.get(symbol)

    if cached:
        cached_at, data = cached

        if time.time() - cached_at <= 300:
            return data

    try:
        df = get_bars(symbol, TimeFrame(15, TimeFrame.Unit.Minute), limit=50, cache_ttl=300)

    except Exception:
        df = get_bars(symbol, TimeFrame.Minute, limit=200, cache_ttl=300)

    if df.empty or len(df) < 25:
        data = {
            "ok": False,
            "ema20": 0,
            "rising": False
        }

        TREND_15M_CACHE[symbol] = (time.time(), data)

        return data

    close = df["close"]

    ema20 = close.ewm(span=20, adjust=False).mean()

    price = safe_float(close.iloc[-1])

    ema_now = safe_float(ema20.iloc[-1])
    ema_prev = safe_float(ema20.iloc[-3])

    rising = ema_now > ema_prev

    ok = price > ema_now or rising

    data = {
        "ok": bool(ok),
        "ema20": ema_now,
        "rising": bool(rising)
    }

    TREND_15M_CACHE[symbol] = (time.time(), data)

    return data


# ==============================================================================
# Universe Builder
# ==============================================================================

def build_clean_universe():
    global UNIVERSE

    log("Building clean universe from Alpaca assets...")

    symbols = []

    try:
        assets = api.list_assets(status="active")

        for asset in assets:
            symbol = getattr(asset, "symbol", "").upper()
            name = getattr(asset, "name", "")
            tradable = bool(getattr(asset, "tradable", False))
            exchange = str(getattr(asset, "exchange", "")).upper()

            if not tradable:
                continue

            if exchange not in ["NASDAQ", "NYSE", "AMEX", "ARCA"]:
                continue

            if not is_clean_symbol(symbol):
                continue

            if is_bad_asset_name(name):
                continue

            symbols.append(symbol)

    except Exception as e:
        log(f"Build clean universe error: {e}")

    symbols = sorted(list(set(symbols)))

    UNIVERSE = symbols

    redis_set_json(KEY_UNIVERSE, UNIVERSE)

    runtime_stats["last_universe"] = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")

    log(f"Clean universe ready: {len(UNIVERSE)} symbols")

    return UNIVERSE


def fast_priority_check(symbol):
    snapshot = get_snapshot(symbol)

    price = safe_float(snapshot.get("price"))
    spread = safe_float(snapshot.get("spread_pct"), 999)
    dollar_volume = safe_float(snapshot.get("dollar_volume"))
    day_volume = safe_float(snapshot.get("day_volume"))
    minute_volume = safe_float(snapshot.get("minute_volume"))
    gap_pct = safe_float(snapshot.get("gap_pct"))
    day_high = safe_float(snapshot.get("day_high"))

    if price < PRICE_MIN or price > PRICE_MAX:
        return False, snapshot

    if spread > MAX_SPREAD:
        return False, snapshot

    if dollar_volume < MIN_DOLLAR_VOLUME:
        return False, snapshot

    near_high = False

    if day_high > 0 and price >= day_high * 0.96:
        near_high = True

    low_float = False

    float_value = get_float(symbol)

    if float_value and float_value <= 50_000_000:
        low_float = True

    hot = (
        gap_pct >= 4
        or day_volume >= 500_000
        or minute_volume >= 20_000
        or near_high
        or low_float
    )

    return hot, snapshot


def build_priority_universe():
    global PRIORITY_UNIVERSE
    global NORMAL_UNIVERSE

    log("Building priority universe...")

    priority_scored = []
    normal = []

    checked = 0

    for symbol in UNIVERSE:
        checked += 1

        try:
            hot, snapshot = fast_priority_check(symbol)

            price = safe_float(snapshot.get("price"))

            if price < PRICE_MIN or price > PRICE_MAX:
                continue

            score = 0

            score += min(max(safe_float(snapshot.get("gap_pct")), 0), 30)

            score += min(safe_float(snapshot.get("day_volume")) / 100_000, 25)

            score += min(safe_float(snapshot.get("dollar_volume")) / 250_000, 25)

            day_high = safe_float(snapshot.get("day_high"))

            if day_high > 0 and price >= day_high * 0.96:
                score += 15

            score += get_float_score(symbol) * 2

            if hot:
                priority_scored.append((score, symbol))
            else:
                normal.append(symbol)

        except Exception:
            continue

        if checked % 200 == 0:
            log(f"Priority universe checked {checked}/{len(UNIVERSE)}")
            time.sleep(1)

    priority_symbols = [
        symbol
        for score, symbol in priority_scored
    ]

    priority_set = set(priority_symbols)

    PRIORITY_UNIVERSE = sorted(priority_set)

    NORMAL_UNIVERSE = sorted([
        symbol
        for symbol in UNIVERSE
        if symbol not in priority_set
    ])

    redis_set_json(KEY_PRIORITY, PRIORITY_UNIVERSE)

    redis_set_json(
        f"{REDIS_PREFIX}:normal",
        NORMAL_UNIVERSE
    )

    log(
        f"Priority universe: {len(PRIORITY_UNIVERSE)} | "
        f"Normal universe: {len(NORMAL_UNIVERSE)} | "
        f"Total covered: {len(PRIORITY_UNIVERSE) + len(NORMAL_UNIVERSE)}"
    )

    return PRIORITY_UNIVERSE, NORMAL_UNIVERSE

def load_universe_from_redis():
    global UNIVERSE
    global PRIORITY_UNIVERSE
    global NORMAL_UNIVERSE

    UNIVERSE = redis_get_json(KEY_UNIVERSE, []) or []

    PRIORITY_UNIVERSE = redis_get_json(KEY_PRIORITY, []) or []

    NORMAL_UNIVERSE = redis_get_json(f"{REDIS_PREFIX}:normal", []) or []

    log(
        f"Loaded universe from Redis: "
        f"all={len(UNIVERSE)} priority={len(PRIORITY_UNIVERSE)} normal={len(NORMAL_UNIVERSE)}"
    )


def rebuild_universe(full=True):
    if full or not UNIVERSE:
        build_clean_universe()

    build_priority_universe()


# ==============================================================================
# Batch Engine
# ==============================================================================

priority_cursor = 0
normal_cursor = 0


def get_next_batch():
    global priority_cursor
    global normal_cursor

    if not PRIORITY_UNIVERSE and not NORMAL_UNIVERSE:
        return []

    use_priority = (
        bool(PRIORITY_UNIVERSE)
        and (
            random.random() < 0.70
            or not NORMAL_UNIVERSE
        )
    )

    source = PRIORITY_UNIVERSE if use_priority else NORMAL_UNIVERSE

    if not source:
        source = PRIORITY_UNIVERSE or NORMAL_UNIVERSE

    if source is PRIORITY_UNIVERSE:
        cursor = priority_cursor
    else:
        cursor = normal_cursor

    if cursor >= len(source):
        cursor = 0

    batch = source[cursor:cursor + 180]

    if len(batch) < 180:
        batch += source[:180 - len(batch)]

    cursor = (cursor + 180) % max(len(source), 1)

    if source is PRIORITY_UNIVERSE:
        priority_cursor = cursor
    else:
        normal_cursor = cursor

    return list(dict.fromkeys(batch))


# ==============================================================================
# Watchlist / Memory
# ==============================================================================

def get_sent_alerts():
    return redis_get_json(KEY_ALERTS, {}) or {}


def save_sent_alert(symbol, data):
    alerts = get_sent_alerts()

    alerts[symbol] = data

    redis_set_json(KEY_ALERTS, alerts)


def already_alerted_today(symbol):
    alerts = get_sent_alerts()

    item = alerts.get(symbol)

    if not item:
        return False

    return item.get("date") == today_ksa()


def add_to_watchlist(symbol, reason, data=None):
    if data is None:
        data = {}

    item = {
        "symbol": symbol,
        "reason": reason,
        "data": data,
        "added_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "last_checked": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "date": today_ksa()
    }

    WATCHLIST[symbol] = item

    redis_hset_json(KEY_WATCHLIST, symbol, item)


def remove_from_watchlist(symbol):
    if symbol in WATCHLIST:
        del WATCHLIST[symbol]

    redis_hdel(KEY_WATCHLIST, symbol)

def get_compression_candidates():
    return redis_hgetall_json(KEY_COMPRESSION)


def save_compression_candidate(symbol, data):
    redis_hset_json(KEY_COMPRESSION, symbol, data)


def remove_compression_candidate(symbol):
    redis_hdel(KEY_COMPRESSION, symbol)


def detect_compression_pattern(symbol):
    df = get_bars(symbol, TimeFrame.Minute, limit=80, cache_ttl=60)

    if df.empty or len(df) < 35:
        return None

    price = safe_float(df["close"].iloc[-1])

    if price <= 0:
        return None

    recent = df.tail(25)

    high = safe_float(recent["high"].max())
    low = safe_float(recent["low"].min())

    range_pct = ((high - low) / price) * 100 if price > 0 else 999

    vwap = calculate_vwap(df)

    obv_data = calculate_obv(df)

    atr = calculate_atr(df)

    atr_pct = (atr / price) * 100 if price > 0 else 0

    resistance_data = calculate_resistance(df)

    rvol = calculate_rvol(df)

    volume_accel = calculate_volume_acceleration(df)

    if range_pct > 6:
        return None

    if atr_pct > 5:
        return None

    if vwap > 0 and price < vwap * 0.985:
        return None

    if not obv_data.get("obv_rising") and rvol < 1.2:
        return None

    if resistance_data.get("distance_pct", 999) > 3:
        return None

    data = {
        "symbol": symbol,
        "price": price,
        "range_pct": range_pct,
        "atr_pct": atr_pct,
        "rvol": rvol,
        "volume_accel_ratio": safe_float(volume_accel.get("ratio")),
        "resistance": resistance_data.get("resistance"),
        "resistance_distance_pct": resistance_data.get("distance_pct"),
        "vwap": vwap,
        "obv_rising": obv_data.get("obv_rising"),
        "detected_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "date": today_ksa()
    }

    return data


def scan_compression_candidates(batch):
    for symbol in batch:
        try:
            data = detect_compression_pattern(symbol)

            if data:
                save_compression_candidate(symbol, data)

        except Exception as e:
            log(f"Compression scan error {symbol}: {e}")

def load_watchlist():
    global WATCHLIST

    WATCHLIST = redis_hgetall_json(KEY_WATCHLIST)

    return WATCHLIST


def save_rejection(symbol, reason, details=None):
    if details is None:
        details = {}

    item = {
        "symbol": symbol,
        "reason": reason,
        "details": details,
        "time": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "date": today_ksa()
    }

    key = f"{symbol}:{int(time.time())}"

    redis_hset_json(KEY_REJECTED, key, item)


def save_alert_history(symbol, metrics, trade_plan):
    item = {
        "symbol": symbol,
        "metrics": metrics,
        "trade_plan": trade_plan,
        "time": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "date": today_ksa()
    }

    key = f"{symbol}:{int(time.time())}"

    redis_hset_json(KEY_HISTORY, key, item)

# ==============================================================================
# Pattern Engine
# ==============================================================================

PATTERN_TTL_SECONDS = 60 * 60 * 3


def load_pattern_cache():
    return redis_hgetall_json(KEY_PATTERNS)


def save_pattern(symbol, pattern_data):
    redis_hset_json(KEY_PATTERNS, symbol, pattern_data)


def remove_pattern(symbol):
    redis_hdel(KEY_PATTERNS, symbol)


def cleanup_expired_patterns():
    patterns = load_pattern_cache()

    if not patterns:
        return

    now_ts = time.time()

    for symbol, pattern_data in list(patterns.items()):
        try:
            detected_ts = safe_float(pattern_data.get("detected_ts"))

            if detected_ts <= 0:
                remove_pattern(symbol)
                continue

            if now_ts - detected_ts > PATTERN_TTL_SECONDS:
                remove_pattern(symbol)

        except Exception:
            remove_pattern(symbol)


def detect_compression_setup(symbol):
    df = get_bars(symbol, TimeFrame.Minute, limit=100, cache_ttl=60)

    if df.empty or len(df) < 45:
        return None

    price = safe_float(df["close"].iloc[-1])

    if price <= 0:
        return None

    recent = df.tail(30)

    recent_high = safe_float(recent["high"].max())
    recent_low = safe_float(recent["low"].min())

    if recent_high <= 0 or recent_low <= 0:
        return None

    compression_range_pct = ((recent_high - recent_low) / price) * 100

    if compression_range_pct > 6:
        return None

    atr = calculate_atr(df)

    atr_pct = (atr / price) * 100 if price > 0 else 0

    if atr_pct > 5:
        return None

    vwap = calculate_vwap(df)

    if vwap > 0 and price < vwap * 0.985:
        return None

    obv_data = calculate_obv(df)

    rvol = calculate_rvol(df)

    volume_accel = calculate_volume_acceleration(df)

    resistance_data = calculate_resistance(df)

    resistance_distance_pct = safe_float(
        resistance_data.get("distance_pct"),
        999
    )

    if resistance_distance_pct > 3:
        return None

    if not obv_data.get("obv_rising") and rvol < 1.2:
        return None

    higher_lows = False

    try:
        lows = recent["low"].tail(10).values

        if len(lows) >= 6:
            higher_lows = lows[-1] >= lows[0] * 0.985
    except Exception:
        higher_lows = False

    compression_score = 0

    reasons = []

    if compression_range_pct <= 3:
        compression_score += 30
        reasons.append("نطاق ضيق جدًا")

    elif compression_range_pct <= 5:
        compression_score += 20
        reasons.append("نطاق ضغط مقبول")

    if atr_pct <= 3:
        compression_score += 20
        reasons.append("ATR منخفض مناسب للضغط")

    if vwap > 0 and price >= vwap:
        compression_score += 15
        reasons.append("السعر فوق VWAP أثناء الضغط")

    if obv_data.get("obv_rising"):
        compression_score += 15
        reasons.append("OBV لا ينهار أثناء الضغط")

    if resistance_distance_pct <= 2:
        compression_score += 10
        reasons.append("قريب من مقاومة قابلة للاختراق")

    if higher_lows:
        compression_score += 10
        reasons.append("قيعان متماسكة/مرتفعة")

    if compression_score < 55:
        return None

    pattern_data = {
        "symbol": symbol,
        "pattern": "compression",
        "score": compression_score,
        "price": price,
        "range_pct": compression_range_pct,
        "atr_pct": atr_pct,
        "vwap": vwap,
        "rvol": rvol,
        "volume_accel_ratio": safe_float(volume_accel.get("ratio")),
        "obv_rising": obv_data.get("obv_rising"),
        "resistance": resistance_data.get("resistance"),
        "resistance_distance_pct": resistance_distance_pct,
        "higher_lows": higher_lows,
        "reasons": reasons,
        "detected_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "detected_ts": time.time(),
        "date": today_ksa()
    }

    return pattern_data


def scan_pattern_engine(batch):
    if not batch:
        return

    cleanup_expired_patterns()

    for symbol in batch:
        try:
            pattern_data = detect_compression_setup(symbol)

            if pattern_data:
                save_pattern(symbol, pattern_data)

        except Exception as e:
            log(f"Pattern engine error {symbol}: {e}")


def get_pattern_data(symbol):
    patterns = load_pattern_cache()

    return patterns.get(symbol)


def is_compression_breakout(symbol, metrics):
    pattern_data = get_pattern_data(symbol)

    if not pattern_data:
        return False, None

    if pattern_data.get("pattern") != "compression":
        return False, None

    if pattern_data.get("date") != today_ksa():
        remove_pattern(symbol)
        return False, None

    breakout = bool(metrics.get("breakout"))
    rvol = safe_float(metrics.get("rvol"))
    volume_accel_ratio = safe_float(metrics.get("volume_accel_ratio"))
    price = safe_float(metrics.get("price"))
    resistance = safe_float(pattern_data.get("resistance"))

    if not breakout:
        return False, pattern_data

    if rvol < 3:
        return False, pattern_data

    if volume_accel_ratio < 1.5:
        return False, pattern_data

    if resistance > 0 and price < resistance:
        return False, pattern_data

    return True, pattern_data


def apply_pattern_boost(metrics):
    symbol = metrics.get("symbol")

    if not symbol:
        return metrics

    compression_breakout, pattern_data = is_compression_breakout(
        symbol,
        metrics
    )

    metrics["pattern_data"] = pattern_data
    metrics["pattern_breakout"] = compression_breakout
    metrics["pattern_name"] = pattern_data.get("pattern") if pattern_data else None

    if not compression_breakout:
        return metrics

    current_multiplier = safe_float(metrics.get("synergy_multiplier"), 1.0)

    new_multiplier = min(
        1.35,
        current_multiplier + 0.10
    )

    metrics["synergy_multiplier"] = new_multiplier

    core_score = safe_float(metrics.get("core_score"))
    penalty_points = safe_float(metrics.get("penalty_points"))

    metrics["final_score"] = min(
        100,
        max(
            0,
            (core_score * new_multiplier) - penalty_points
        )
    )

    metrics["confidence"] = min(
        99,
        safe_float(metrics.get("confidence")) + 5
    )

    synergy_reasons = metrics.get("synergy_reasons", [])

    if "اختراق بعد مرحلة ضغط مراقبة مسبقًا" not in synergy_reasons:
        synergy_reasons.append("اختراق بعد مرحلة ضغط مراقبة مسبقًا")

    metrics["synergy_reasons"] = synergy_reasons

    return metrics
    
# ==============================================================================
# High Target Analysis
# ==============================================================================

def analyze_high_target(symbol, price, atr, resistance, rvol, trend_15m_ok):
    float_value = get_float(symbol)

    room_to_resistance_pct = 999

    if resistance and resistance > price:
        room_to_resistance_pct = ((resistance - price) / price) * 100

    atr_pct = 0

    if price > 0:
        atr_pct = (atr / price) * 100

    score = 0

    reasons = []

    if room_to_resistance_pct >= 8 or room_to_resistance_pct == 999:
        score += 30
        reasons.append("مقاومات بعيدة نسبيًا")

    if atr_pct >= 2:
        score += 25
        reasons.append("ATR يسمح بحركة قوية")

    if rvol >= 4:
        score += 20
        reasons.append("RVOL يدعم استمرار الزخم")

    if float_value and float_value <= 50_000_000:
        score += 15
        reasons.append("الفلوت مناسب للحركة السريعة")

    if trend_15m_ok:
        score += 10
        reasons.append("الاتجاه على 15 دقيقة داعم")

    has_high_target = score >= 65

    if has_high_target:
        extended_target = price + (atr * 4)
    else:
        extended_target = price + (atr * 2)

    return {
        "has_high_target": has_high_target,
        "score": score,
        "reasons": reasons,
        "room_to_resistance_pct": room_to_resistance_pct,
        "extended_target": extended_target
    }


# ==============================================================================
# Phase Detection
# ==============================================================================

def detect_stock_phase(breakout, rvol, volume_accel_ratio, trend_15m_ok):
    if is_last_market_hour():
        return "⚠️ مرحلة متأخرة بشروط مشددة"

    if breakout and rvol >= 5 and volume_accel_ratio >= 2:
        return "🚀 مرحلة الاختراق والتسارع"

    if rvol >= 5 and volume_accel_ratio >= 2:
        return "🔥 مرحلة التسارع"

    if trend_15m_ok and rvol >= 3:
        return "🟢 مرحلة الانطلاق المبكرة"

    return "📈 مرحلة مراقبة قوية"


# ==============================================================================
# Scoring Helpers
# ==============================================================================

def score_linear(value, min_value, max_value, max_points):
    value = safe_float(value)

    if value <= min_value:
        return 0

    if value >= max_value:
        return max_points

    return ((value - min_value) / (max_value - min_value)) * max_points


def calculate_core_score(
    symbol,
    price,
    vwap,
    rvol,
    volume_accel_ratio,
    obv_rising,
    atr_pct,
    trend_15m_ok,
    float_score,
    breakout,
    resistance_distance_pct,
    spread_pct,
    news_bonus
):
    score = 0

    # RVOL = 20
    score += score_linear(rvol, 2.5, 8.0, 20)

    # Volume Acceleration = 18
    score += score_linear(volume_accel_ratio, 1.2, 4.0, 18)

    # VWAP / Price Position = 12
    if price > vwap:
        score += 12

    # OBV = 12
    if obv_rising:
        score += 12

    # ATR Quality = 10
    score += score_linear(atr_pct, 1.0, 8.0, 10)

    # 15m Trend = 10
    if trend_15m_ok:
        score += 10

    # Float = 10
    score += float_score

    # Breakout Quality = 6
    if breakout:
        score += 6
    else:
        if resistance_distance_pct <= 2:
            score += score_linear(2 - resistance_distance_pct, 0, 2, 4)

    # Liquidity / Spread = 4
    if spread_pct <= 0.5:
        score += 4
    elif spread_pct <= 1.0:
        score += 3
    elif spread_pct <= 1.5:
        score += 2
    elif spread_pct <= 2.0:
        score += 1

    # Positive news bonus only
    score += news_bonus

    return min(score, 100)


def calculate_synergy_multiplier(
    symbol,
    rvol,
    volume_accel_ratio,
    price,
    vwap,
    obv_rising,
    trend_15m_ok,
    breakout,
    atr_pct,
    resistance_distance_pct,
    positive_news
):
    multiplier = 1.00

    reasons = []

    float_value = get_float(symbol)

    low_float = float_value and float_value <= 50_000_000

    strong_rvol = rvol >= 4

    strong_accel = volume_accel_ratio >= 2

    if low_float and strong_rvol:
        multiplier += 0.10
        reasons.append("فلوت منخفض مع RVOL قوي")

    if strong_rvol and strong_accel:
        multiplier += 0.10
        reasons.append("RVOL قوي مع تسارع واضح في الحجم")

    if price > vwap and obv_rising and trend_15m_ok:
        multiplier += 0.10
        reasons.append("السعر فوق VWAP مع OBV صاعد واتجاه 15 دقيقة داعم")

    if breakout and atr_pct >= 1.5 and resistance_distance_pct <= 1.5:
        multiplier += 0.10
        reasons.append("اختراق مع ATR مناسب وقرب ممتاز من منطقة الدخول")

    if positive_news and strong_rvol:
        multiplier += 0.05
        reasons.append("خبر إيجابي داعم مع زخم قوي")

    multiplier = min(multiplier, 1.35)

    return multiplier, reasons


def calculate_penalties(
    symbol,
    atr_pct,
    spread_pct,
    resistance_distance_pct,
    breakout,
    minor_negative_news
):
    penalties = 0

    warnings = []

    float_value = get_float(symbol)

    if float_value and float_value > 100_000_000:
        penalties += 5
        warnings.append("الفلوت مرتفع نسبيًا")

    if float_value and float_value > 500_000_000:
        penalties += 25
        warnings.append("فلوت عالي جدًا")

    if resistance_distance_pct <= 0.30 and not breakout:
        penalties += 8
        warnings.append("مقاومة قريبة جدًا")

    if atr_pct < 1.5:
        penalties += 5
        warnings.append("ATR محدود")

    if spread_pct > 1.5:
        penalties += 5
        warnings.append("السبريد متوسط/مرتفع")

    if minor_negative_news:
        penalties += 12
        warnings.append("خبر سلبي خفيف")

    if is_last_market_hour():
        penalties += 5
        warnings.append("وضع آخر ساعة: شروط أكثر صرامة")

    return penalties, warnings


# ==============================================================================
# Hard Rules
# ==============================================================================

def pass_hard_rules(symbol, snapshot):
    price = safe_float(snapshot.get("price"))

    spread_pct = safe_float(snapshot.get("spread_pct"), 999)

    dollar_volume = safe_float(snapshot.get("dollar_volume"))

    if price < PRICE_MIN or price > PRICE_MAX:
        save_rejection(symbol, "السعر خارج النطاق", snapshot)
        return False

    if spread_pct > MAX_SPREAD:
        save_rejection(symbol, "السبريد مرتفع", snapshot)
        return False

    if dollar_volume < MIN_DOLLAR_VOLUME:
        save_rejection(symbol, "السيولة الدولارية ضعيفة", snapshot)
        return False

    return True


# ==============================================================================
# Candidate Evaluation
# ==============================================================================

def evaluate_candidate(symbol, deep_news=False):
    symbol = symbol.upper()

    snapshot = get_snapshot(symbol)

    if not pass_hard_rules(symbol, snapshot):
        return None

    price = safe_float(snapshot.get("price"))

    df = get_bars(symbol, TimeFrame.Minute, limit=160, cache_ttl=60)

    if df.empty or len(df) < 40:
        save_rejection(symbol, "بيانات الشموع غير كافية", snapshot)
        return None

    vwap = calculate_vwap(df)

    if vwap <= 0 or price < vwap:
        save_rejection(
            symbol,
            "السعر تحت VWAP",
            {
                "price": price,
                "vwap": vwap
            }
        )
        return None

    rvol = calculate_rvol(df)

    if rvol < 2.5:
        add_to_watchlist(
            symbol,
            "RVOL لم يكتمل بعد",
            {
                "rvol": rvol,
                "price": price
            }
        )

        save_rejection(
            symbol,
            "RVOL ضعيف",
            {
                "rvol": rvol
            }
        )

        return None

    volume_acceleration = calculate_volume_acceleration(df)

    volume_accel_ratio = safe_float(volume_acceleration.get("ratio"))

    obv_data = calculate_obv(df)

    atr = calculate_atr(df)

    atr_pct = 0

    if price > 0:
        atr_pct = (atr / price) * 100

    if atr_pct < 1.0:
        add_to_watchlist(
            symbol,
            "ATR ضعيف وقد يتحسن لاحقًا",
            {
                "atr_pct": atr_pct
            }
        )

        save_rejection(
            symbol,
            "ATR منخفض",
            {
                "atr_pct": atr_pct
            }
        )

        return None

    resistance_data = calculate_resistance(df)

    trend_15m = get_15m_trend(symbol)

    news_data = {
        "positive": False,
        "minor_negative": False,
        "serious_negative": False,
        "bonus": 0,
        "headline": ""
    }

    if deep_news:
        news_data = get_symbol_news(symbol)

    if news_data.get("serious_negative"):
        save_rejection(
            symbol,
            "خبر سلبي خطير",
            news_data
        )
        return None

    float_points = get_float_score(symbol)

    core_score = calculate_core_score(
        symbol=symbol,
        price=price,
        vwap=vwap,
        rvol=rvol,
        volume_accel_ratio=volume_accel_ratio,
        obv_rising=obv_data.get("obv_rising"),
        atr_pct=atr_pct,
        trend_15m_ok=trend_15m.get("ok"),
        float_score=float_points,
        breakout=resistance_data.get("breakout"),
        resistance_distance_pct=safe_float(resistance_data.get("distance_pct"), 999),
        spread_pct=safe_float(snapshot.get("spread_pct"), 999),
        news_bonus=safe_int(news_data.get("bonus"))
    )

    synergy_multiplier, synergy_reasons = calculate_synergy_multiplier(
        symbol=symbol,
        rvol=rvol,
        volume_accel_ratio=volume_accel_ratio,
        price=price,
        vwap=vwap,
        obv_rising=obv_data.get("obv_rising"),
        trend_15m_ok=trend_15m.get("ok"),
        breakout=resistance_data.get("breakout"),
        atr_pct=atr_pct,
        resistance_distance_pct=safe_float(resistance_data.get("distance_pct"), 999),
        positive_news=news_data.get("positive")
    )

    penalty_points, warnings = calculate_penalties(
        symbol=symbol,
        atr_pct=atr_pct,
        spread_pct=safe_float(snapshot.get("spread_pct"), 999),
        resistance_distance_pct=safe_float(resistance_data.get("distance_pct"), 999),
        breakout=resistance_data.get("breakout"),
        minor_negative_news=news_data.get("minor_negative")
    )

    final_score = min(
        100,
        max(
            0,
            (core_score * synergy_multiplier) - penalty_points
        )
    )

    confidence = min(
        99,
        max(
            50,
            final_score - (len(warnings) * 2) + (5 if len(synergy_reasons) >= 3 else 0)
        )
    )

    high_target = analyze_high_target(
        symbol=symbol,
        price=price,
        atr=atr,
        resistance=safe_float(resistance_data.get("resistance")),
        rvol=rvol,
        trend_15m_ok=trend_15m.get("ok")
    )

    phase = detect_stock_phase(
        breakout=resistance_data.get("breakout"),
        rvol=rvol,
        volume_accel_ratio=volume_accel_ratio,
        trend_15m_ok=trend_15m.get("ok")
    )
    
    metrics = {
        "symbol": symbol,
        "price": price,
        "bid": safe_float(snapshot.get("bid")),
        "ask": safe_float(snapshot.get("ask")),
        "spread_pct": safe_float(snapshot.get("spread_pct"), 999),
        "day_volume": safe_float(snapshot.get("day_volume")),
        "dollar_volume": safe_float(snapshot.get("dollar_volume")),
        "gap_pct": safe_float(snapshot.get("gap_pct")),
        "vwap": vwap,
        "rvol": rvol,
        "volume_accel_ratio": volume_accel_ratio,
        "volume_trend_up": volume_acceleration.get("trend_up"),
        "volume_peak_recent": volume_acceleration.get("peak_recent"),
        "obv": obv_data.get("obv"),
        "obv_ema": obv_data.get("obv_ema"),
        "obv_rising": obv_data.get("obv_rising"),
        "atr": atr,
        "atr_pct": atr_pct,
        "trend_15m_ok": trend_15m.get("ok"),
        "trend_15m_rising": trend_15m.get("rising"),
        "resistance": safe_float(resistance_data.get("resistance")),
        "resistance_distance_pct": safe_float(resistance_data.get("distance_pct"), 999),
        "breakout": resistance_data.get("breakout"),
        "float_value": get_float(symbol),
        "float_score": float_points,
        "float_label": get_float_label(symbol),
        "news_bonus": safe_int(news_data.get("bonus")),
        "positive_news": news_data.get("positive"),
        "minor_negative_news": news_data.get("minor_negative"),
        "serious_negative_news": news_data.get("serious_negative"),
        "news_headline": news_data.get("headline"),
        "core_score": core_score,
        "synergy_multiplier": synergy_multiplier,
        "synergy_reasons": synergy_reasons,
        "penalty_points": penalty_points,
        "warnings": warnings,
        "final_score": final_score,
        "confidence": confidence,
        "phase": phase,
        "high_target": high_target,
        "evaluated_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
    }

    return metrics
    
# ==============================================================================
# Trade Plan Engine
# ==============================================================================

def build_trade_plan(metrics):
    symbol = metrics["symbol"]

    price = safe_float(metrics.get("price"))

    atr = safe_float(metrics.get("atr"))

    vwap = safe_float(metrics.get("vwap"))

    resistance = safe_float(metrics.get("resistance"))

    if price <= 0 or atr <= 0:
        save_rejection(
            symbol,
            "تعذر بناء خطة الصفقة بسبب سعر أو ATR غير صالح",
            metrics
        )
        return None

    swing_stop = price - max(atr * 1.2, price * 0.025)

    vwap_stop = vwap * 0.992 if vwap > 0 else swing_stop

    breakout_stop = resistance * 0.992 if metrics.get("breakout") and resistance > 0 else swing_stop

    max_loss_stop = price * (1 - (MAX_STOP / 100))

    stop = max(
        min(swing_stop, vwap_stop, breakout_stop),
        max_loss_stop
    )

    stop_distance_pct = ((price - stop) / price) * 100 if price > 0 else 999

    if stop_distance_pct <= 0:
        save_rejection(
            symbol,
            "وقف الخسارة غير صالح",
            {
                "price": price,
                "stop": stop
            }
        )
        return None

    if stop_distance_pct > MAX_STOP:
        save_rejection(
            symbol,
            "وقف الخسارة بعيد جدًا",
            {
                "price": price,
                "stop": stop,
                "stop_distance_pct": stop_distance_pct
            }
        )
        return None

    t1 = price + (atr * 1.2)
    t2 = price + (atr * 2.2)

    t3_atr = price + (atr * 3.5)

    if resistance > price:
        t3 = max(t3_atr, resistance)
    else:
        t3 = t3_atr

    high_target = None

    high_target_data = metrics.get("high_target", {})

    if high_target_data.get("has_high_target"):
        high_target = safe_float(high_target_data.get("extended_target"))

    reward = t1 - price

    risk = price - stop

    reward_risk = reward / risk if risk > 0 else 0

    if reward_risk < 1.2:
        save_rejection(
            symbol,
            "نسبة العائد للمخاطرة ضعيفة",
            {
                "reward_risk": reward_risk,
                "price": price,
                "stop": stop,
                "t1": t1
            }
        )
        return None

    plan = {
        "symbol": symbol,
        "entry": price,
        "initial_stop": stop,
        "stop": stop,
        "stop_distance_pct": stop_distance_pct,
        "t1": t1,
        "t2": t2,
        "t3": t3,
        "high_target": high_target,
        "reward_risk": reward_risk,
        "hit_t1": False,
        "hit_t2": False,
        "hit_t3": False,
        "hit_high_target": False,
        "status": "active",
        "created_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "date": today_ksa()
    }

    return plan


# ==============================================================================
# Final Safety Check
# ==============================================================================

def final_safety_check(metrics, trade_plan):
    symbol = metrics["symbol"]

    snapshot = get_snapshot(symbol)

    price = safe_float(snapshot.get("price"))

    spread_pct = safe_float(snapshot.get("spread_pct"), 999)

    if price <= 0:
        return False, "تعذر قراءة السعر النهائي"

    if spread_pct > MAX_SPREAD:
        return False, "السبريد توسع قبل الإرسال"

    vwap = safe_float(metrics.get("vwap"))

    if vwap > 0 and price < vwap:
        return False, "كسر VWAP قبل الإرسال"

    entry = safe_float(trade_plan.get("entry"))

    if entry > 0:
        extension_pct = ((price - entry) / entry) * 100

        if extension_pct > 7:
            return False, "السعر ابتعد عن منطقة الدخول"

    final_score = safe_float(metrics.get("final_score"))

    required_score = LAST_HOUR_SCORE if is_last_market_hour() else MIN_SCORE

    if final_score < required_score:
        return False, "الدرجة النهائية أقل من المطلوب"

    if is_last_market_hour():
        rvol = safe_float(metrics.get("rvol"))
        accel = safe_float(metrics.get("volume_accel_ratio"))
        trend_ok = bool(metrics.get("trend_15m_ok"))

        if not (rvol >= 5 and accel >= 2 and trend_ok):
            return False, "آخر ساعة والسهم لا يملك زخمًا استثنائيًا"

    df = get_bars(symbol, TimeFrame.Minute, limit=10, cache_ttl=5)

    if not df.empty and len(df) >= 3:
        last_close = safe_float(df["close"].iloc[-1])
        last_open = safe_float(df["open"].iloc[-1])
        last_low = safe_float(df["low"].iloc[-1])

        if last_close < last_open and last_close <= last_low * 1.01:
            return False, "آخر شمعة دقيقة ضعيفة جدًا"

    return True, "OK"


# ==============================================================================
# Alert Message Builder - Arabic
# ==============================================================================

def build_alert_message(metrics, trade_plan):
    symbol = metrics["symbol"]

    high_target_data = metrics.get("high_target", {})

    reasons = []

    reasons.append(f"✅ فلوت: {metrics.get('float_label')}")

    reasons.append(f"✅ RVOL مرتفع: {safe_float(metrics.get('rvol')):.2f}")

    reasons.append(f"✅ تسارع الحجم: {safe_float(metrics.get('volume_accel_ratio')):.2f}x")

    reasons.append("✅ السعر فوق VWAP")

    if metrics.get("obv_rising"):
        reasons.append("✅ OBV صاعد")
    else:
        reasons.append("⚠️ OBV ليس مثاليًا")

    if metrics.get("trend_15m_ok"):
        reasons.append("✅ اتجاه 15 دقيقة داعم")
    else:
        reasons.append("⚠️ اتجاه 15 دقيقة غير مؤكد")

    if metrics.get("breakout"):
        reasons.append("✅ اختراق مقاومة مع ثبات")
    else:
        reasons.append("✅ قريب من منطقة اختراق")

    reasons.append(f"✅ ATR مناسب: {safe_float(metrics.get('atr_pct')):.2f}%")

    reasons.append(f"✅ السبريد: {safe_float(metrics.get('spread_pct')):.2f}%")

    reasons.append("✅ لا توجد أخبار سلبية خطيرة")

    if metrics.get("positive_news"):
        reasons.append("✅ يوجد خبر إيجابي داعم")

    if metrics.get("synergy_reasons"):
        reasons.append(
            "✅ توافق قوي: "
            + " | ".join(metrics.get("synergy_reasons")[:3])
        )

    if metrics.get("pattern_breakout"):
        reasons.append("🔥 اختراق بعد مرحلة ضغط كان البوت يراقبها مسبقًا")

    warnings_text = ""

    if metrics.get("warnings"):
        warnings_text = "\n⚠️ <b>ملاحظات:</b>\n"

        for warning in metrics.get("warnings")[:5]:
            warnings_text += f"• {warning}\n"

    high_target_text = ""

    if high_target_data.get("has_high_target"):
        high_target_text += "\n━━━━━━━━━━━━━━\n"
        high_target_text += "🚀 <b>احتمالية الأهداف العالية:</b>\n\n"
        high_target_text += "✅ نعم، السهم لديه مساحة صعود عالية\n"
        high_target_text += "📌 السبب:\n"

        for reason in high_target_data.get("reasons", [])[:4]:
            high_target_text += f"• {reason}\n"

        if trade_plan.get("high_target"):
            high_target_text += f"\n🎯 الهدف الممتد: <b>{fmt_price(trade_plan.get('high_target'))}</b>\n"

    else:
        high_target_text += "\n━━━━━━━━━━━━━━\n"
        high_target_text += "🚀 <b>احتمالية الأهداف العالية:</b>\n\n"
        high_target_text += "⚠️ محدودة حاليًا\n"
        high_target_text += "📌 يحتاج استمرار الزخم أو مساحة أفضل فوق المقاومات.\n"

    message = f"""🚀 <b>رادار النخبة - Elite Radar</b>

📈 <b>السهم:</b> {symbol}

⭐ <b>درجة الجودة:</b> {safe_float(metrics.get('final_score')):.1f} / 100
🎯 <b>مستوى الثقة:</b> {safe_float(metrics.get('confidence')):.0f}%
📌 <b>مرحلة السهم:</b> {metrics.get('phase')}

━━━━━━━━━━━━━━

📌 <b>سبب اختيار السهم:</b>

{chr(10).join(reasons)}
{warnings_text}
━━━━━━━━━━━━━━

💰 <b>سعر الدخول:</b> {fmt_price(trade_plan.get('entry'))}
🛑 <b>وقف الخسارة:</b> {fmt_price(trade_plan.get('stop'))} ({safe_float(trade_plan.get('stop_distance_pct')):.2f}%)
🎯 <b>الهدف الأول:</b> {fmt_price(trade_plan.get('t1'))}
🎯 <b>الهدف الثاني:</b> {fmt_price(trade_plan.get('t2'))}
🎯 <b>الهدف الثالث:</b> {fmt_price(trade_plan.get('t3'))}

📊 <b>العائد / المخاطرة:</b> {safe_float(trade_plan.get('reward_risk')):.2f}

━━━━━━━━━━━━━━

🧠 <b>تفاصيل القرار:</b>
CoreScore: {safe_float(metrics.get('core_score')):.1f}
Synergy: x{safe_float(metrics.get('synergy_multiplier')):.2f}
Penalty: -{safe_float(metrics.get('penalty_points')):.1f}

{high_target_text}
━━━━━━━━━━━━━━

👀 سيستمر البوت في مراقبة الصفقة كل 30 ثانية، ويرسل تحديثات رفع الوقف أو الخروج عند الحاجة.
"""

    return message


# ==============================================================================
# Alert Sender
# ==============================================================================

def activate_trade(symbol, metrics, trade_plan):
    
    entry_price = safe_float(trade_plan.get("entry"))

    item = {
        "symbol": symbol,
        "metrics": metrics,
        "trade_plan": trade_plan,
        "highest_price": entry_price,
        "lowest_price": entry_price,
        "max_profit_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "opened_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "last_update": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "date": today_ksa()
    }

    ACTIVE_TRADES[symbol] = item

    redis_hset_json(KEY_ACTIVE, symbol, item)


def send_elite_alert(metrics):
    symbol = metrics["symbol"]

    if already_alerted_today(symbol):
        return False

    trade_plan = build_trade_plan(metrics)

    if not trade_plan:
        return False

    ok, reason = final_safety_check(metrics, trade_plan)

    if not ok:
        save_rejection(
            symbol,
            f"فشل الفحص النهائي: {reason}",
            {
                "metrics": metrics,
                "trade_plan": trade_plan
            }
        )
        return False

    message = build_alert_message(metrics, trade_plan)

    send_telegram(message)

    save_sent_alert(
        symbol,
        {
            "date": today_ksa(),
            "time": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
            "score": metrics.get("final_score")
        }
    )

    save_alert_history(symbol, metrics, trade_plan)

    activate_trade(symbol, metrics, trade_plan)

    remove_from_watchlist(symbol)

    runtime_stats["alerts_sent"] += 1

    log(f"Elite alert sent: {symbol} score={safe_float(metrics.get('final_score')):.1f}")

    return True

# ==============================================================================
# Active Trade Manager
# ==============================================================================

def load_active_trades():
    global ACTIVE_TRADES

    ACTIVE_TRADES = redis_hgetall_json(KEY_ACTIVE)

    runtime_stats["active_trades"] = len(ACTIVE_TRADES)

    return ACTIVE_TRADES


def update_active_trade(symbol, item):
    ACTIVE_TRADES[symbol] = item

    redis_hset_json(KEY_ACTIVE, symbol, item)


def close_active_trade(symbol, item, reason):
    trade_plan = item.get("trade_plan", {})

    trade_plan["status"] = "closed"

    item["trade_plan"] = trade_plan

    item["closed_at"] = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")

    item["close_reason"] = reason

    opened_at = item.get("opened_at")

    if opened_at:
        try:
            opened_dt = datetime.strptime(
                opened_at,
                "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=saudi_tz)

            item["holding_minutes"] = int(
                (datetime.now(saudi_tz) - opened_dt).total_seconds() / 60
            )

        except Exception:
            item["holding_minutes"] = None
    else:
        item["holding_minutes"] = None

    trade_plan = item.get("trade_plan", {})

    item["t1_hit"] = bool(trade_plan.get("hit_t1"))
    item["t2_hit"] = bool(trade_plan.get("hit_t2"))
    item["t3_hit"] = bool(trade_plan.get("hit_t3"))

    redis_hset_json(
        KEY_HISTORY,
        f"{symbol}:closed:{int(time.time())}",
        item
    )

    redis_hdel(KEY_ACTIVE, symbol)

    if symbol in ACTIVE_TRADES:
        del ACTIVE_TRADES[symbol]

    runtime_stats["active_trades"] = len(ACTIVE_TRADES)


def send_trade_update(symbol, text):
    message = f"""🟢 <b>Elite Radar - تحديث صفقة</b>

📈 <b>السهم:</b> {symbol}

{text}
"""

    send_telegram(message)


def send_trade_exit(symbol, text):
    message = f"""🔴 <b>Elite Radar - خروج / تحذير</b>

📈 <b>السهم:</b> {symbol}

{text}
"""

    send_telegram(message)


def monitor_single_trade(symbol, item):
    trade_plan = item.get("trade_plan", {})

    metrics = item.get("metrics", {})

    snapshot = get_snapshot(symbol)

    price = safe_float(snapshot.get("price"))

    if price <= 0:
        return

    entry = safe_float(trade_plan.get("entry"))
    highest_price = max(
        safe_float(item.get("highest_price"), entry),
        price
    )

    lowest_price = min(
        safe_float(item.get("lowest_price"), entry),
        price
    )

    item["highest_price"] = highest_price
    item["lowest_price"] = lowest_price

    if entry > 0:
        item["max_profit_pct"] = ((highest_price - entry) / entry) * 100
        item["max_drawdown_pct"] = ((lowest_price - entry) / entry) * 100
        
    stop = safe_float(trade_plan.get("stop"))
    t1 = safe_float(trade_plan.get("t1"))
    t2 = safe_float(trade_plan.get("t2"))
    t3 = safe_float(trade_plan.get("t3"))
    high_target = safe_float(trade_plan.get("high_target"))

    # --------------------------------------------------------------------------
    # Stop hit
    # --------------------------------------------------------------------------

    if stop > 0 and price <= stop:
        send_trade_exit(
            symbol,
            f"""💰 السعر الحالي: {fmt_price(price)}
🛑 تم كسر الوقف: {fmt_price(stop)}

📌 سبب الخروج:
ضرب وقف الخسارة."""
        )

        close_active_trade(symbol, item, "stop_hit")

        return

    messages = []

    updated = False

    # --------------------------------------------------------------------------
    # T1 hit
    # --------------------------------------------------------------------------

    if not trade_plan.get("hit_t1") and t1 > 0 and price >= t1:
        trade_plan["hit_t1"] = True

        new_stop = max(stop, entry * 1.002)

        if new_stop > stop:
            trade_plan["stop"] = new_stop

        messages.append(
            f"""🎯 وصل الهدف الأول T1
💰 السعر الحالي: {fmt_price(price)}
🛑 الوقف الجديد: {fmt_price(trade_plan.get("stop"))}"""
        )

        updated = True

    # --------------------------------------------------------------------------
    # T2 hit
    # --------------------------------------------------------------------------

    if not trade_plan.get("hit_t2") and t2 > 0 and price >= t2:
        trade_plan["hit_t2"] = True

        atr = safe_float(metrics.get("atr"))

        if atr <= 0:
            atr = price * 0.025

        new_stop = max(
            safe_float(trade_plan.get("stop")),
            price - max(atr, price * 0.02)
        )

        trade_plan["stop"] = new_stop

        messages.append(
            f"""🎯 وصل الهدف الثاني T2
💰 السعر الحالي: {fmt_price(price)}
🛑 الوقف الجديد: {fmt_price(trade_plan.get("stop"))}"""
        )

        updated = True

    # --------------------------------------------------------------------------
    # T3 hit
    # --------------------------------------------------------------------------

    if not trade_plan.get("hit_t3") and t3 > 0 and price >= t3:
        trade_plan["hit_t3"] = True

        atr = safe_float(metrics.get("atr"))

        if atr <= 0:
            atr = price * 0.02

        new_stop = max(
            safe_float(trade_plan.get("stop")),
            price - max(atr * 0.8, price * 0.018)
        )

        trade_plan["stop"] = new_stop

        messages.append(
            f"""🎯 وصل الهدف الثالث T3
💰 السعر الحالي: {fmt_price(price)}
🛑 الوقف الجديد: {fmt_price(trade_plan.get("stop"))}

📌 الصفقة وصلت لهدف قوي، يتم تشديد الوقف لحماية الربح."""
        )

        updated = True

    # --------------------------------------------------------------------------
    # High target hit
    # --------------------------------------------------------------------------

    if (
        high_target > 0
        and not trade_plan.get("hit_high_target")
        and price >= high_target
    ):
        trade_plan["hit_high_target"] = True

        messages.append(
            f"""🚀 وصل الهدف الممتد
💰 السعر الحالي: {fmt_price(price)}
🎯 الهدف الممتد: {fmt_price(high_target)}

📌 هذه حركة قوية جدًا، يُفضل مراقبة الوقف المتحرك."""
        )

        updated = True

    # --------------------------------------------------------------------------
    # Weakness / emergency checks
    # --------------------------------------------------------------------------

    df = get_bars(symbol, TimeFrame.Minute, limit=80, cache_ttl=20)

    if not df.empty and len(df) >= 30:
        vwap = calculate_vwap(df)

        obv_data = calculate_obv(df)

        rvol = calculate_rvol(df)

        volume_accel = calculate_volume_acceleration(df)

        volume_accel_ratio = safe_float(volume_accel.get("ratio"))

        close = safe_float(df["close"].iloc[-1])

        open_ = safe_float(df["open"].iloc[-1])

        # VWAP failure before T1
        if (
            vwap > 0
            and price < vwap
            and not trade_plan.get("hit_t1")
        ):
            send_trade_exit(
                symbol,
                f"""💰 السعر الحالي: {fmt_price(price)}
📉 كسر VWAP قبل تحقيق الهدف الأول.

📌 سبب الخروج:
فشل مبكر في الزخم."""
            )

            close_active_trade(symbol, item, "vwap_failure_before_t1")

            return

        # VWAP weakness after T1
        if (
            vwap > 0
            and price < vwap
            and trade_plan.get("hit_t1")
        ):
            new_stop = max(
                safe_float(trade_plan.get("stop")),
                entry
            )

            if new_stop > safe_float(trade_plan.get("stop")):
                trade_plan["stop"] = new_stop

                messages.append(
                    f"""⚠️ السعر كسر VWAP بعد تحقيق هدف
🛑 تم تشديد الوقف إلى: {fmt_price(new_stop)}"""
                )

                updated = True

        # OBV deterioration after profit
        if (
            not obv_data.get("obv_rising")
            and trade_plan.get("hit_t1")
        ):
            new_stop = max(
                safe_float(trade_plan.get("stop")),
                entry
            )

            if new_stop > safe_float(trade_plan.get("stop")):
                trade_plan["stop"] = new_stop

                messages.append(
                    f"""⚠️ OBV بدأ يضعف بعد تحقيق ربح
🛑 تم رفع الوقف إلى: {fmt_price(new_stop)}"""
                )

                updated = True

        # Volume collapse
        if (
            rvol < 1.5
            and volume_accel_ratio < 1.0
            and not trade_plan.get("hit_t1")
        ):
            send_trade_exit(
                symbol,
                f"""💰 السعر الحالي: {fmt_price(price)}
📉 انهيار واضح في RVOL وتسارع الحجم قبل تحقيق الهدف.

📌 سبب الخروج:
Volume Death."""
            )

            close_active_trade(symbol, item, "volume_death_before_t1")

            return

        # Weak red candle after extended move
        if (
            close < open_
            and trade_plan.get("hit_t2")
            and close <= safe_float(df["low"].iloc[-1]) * 1.01
        ):
            new_stop = max(
                safe_float(trade_plan.get("stop")),
                price - max(safe_float(metrics.get("atr")), price * 0.015)
            )

            if new_stop > safe_float(trade_plan.get("stop")):
                trade_plan["stop"] = new_stop

                messages.append(
                    f"""⚠️ شمعة دقيقة ضعيفة بعد امتداد قوي
🛑 تم تشديد الوقف إلى: {fmt_price(new_stop)}"""
                )

                updated = True

    # --------------------------------------------------------------------------
    # Send updates
    # --------------------------------------------------------------------------

    if messages:
        send_trade_update(
            symbol,
            "\n\n".join(messages)
        )

    if updated:
        item["trade_plan"] = trade_plan
        item["last_update"] = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")

        update_active_trade(symbol, item)


def monitor_active_trades():
    trades = load_active_trades()

    if not trades:
        return

    for symbol, item in list(trades.items()):
        try:
            monitor_single_trade(symbol, item)
        except Exception as e:
            log(f"Monitor error {symbol}: {e}")


# ==============================================================================
# Watchlist Scanner
# ==============================================================================

def scan_watchlist():
    load_watchlist()

    if not WATCHLIST:
        return

    checked = 0

    for symbol in list(WATCHLIST.keys()):
        checked += 1

        try:
            metrics = evaluate_candidate(symbol, deep_news=True)

            if not metrics:
                continue

            required_score = LAST_HOUR_SCORE if is_last_market_hour() else MIN_SCORE

            if safe_float(metrics.get("final_score")) >= required_score:
                send_elite_alert(metrics)

        except Exception as e:
            log(f"Watchlist scan error {symbol}: {e}")

        if checked >= 80:
            break


# ==============================================================================
# Main Scan Engine
# ==============================================================================

def scan_once():
    batch = get_next_batch()

    if not batch:
        log("Empty batch. Rebuilding universe...")

        rebuild_universe(full=True)

        return

    scan_pattern_engine(batch)

    runtime_stats["total_scans"] += 1

    runtime_stats["last_scan"] = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")

    finalists = []

    evaluated = 0

    required_score = LAST_HOUR_SCORE if is_last_market_hour() else MIN_SCORE

    for symbol in batch:
        try:
            if already_alerted_today(symbol):
                continue

            hot, snapshot = fast_priority_check(symbol)

            if not hot:
                continue

            metrics = evaluate_candidate(symbol, deep_news=False)

            evaluated += 1

            if not metrics:
                continue

            # If near-final, do deep news check
            if safe_float(metrics.get("final_score")) >= required_score - 8:
                deep_metrics = evaluate_candidate(symbol, deep_news=True)

                if deep_metrics:
                    metrics = deep_metrics

            metrics = apply_pattern_boost(metrics)

            if safe_float(metrics.get("final_score")) >= required_score:
                finalists.append(metrics)

        except Exception as e:
            log(f"Scan error {symbol}: {e}")

    finalists = sorted(
        finalists,
        key=lambda item: safe_float(item.get("final_score")),
        reverse=True
    )

    sent = 0

    # No fixed max: send only truly qualified candidates.
    for metrics in finalists:
        if send_elite_alert(metrics):
            sent += 1
            time.sleep(1)

    runtime_stats["symbols_checked"] += evaluated

    redis_set_json(KEY_RUNTIME, runtime_stats)

    log(
        f"Scan #{runtime_stats['total_scans']} | "
        f"Batch={len(batch)} | "
        f"Evaluated={evaluated} | "
        f"Finalists={len(finalists)} | "
        f"Sent={sent}"
    )


# ==============================================================================
# Universe Refresh Logic
# ==============================================================================

last_full_universe_refresh = 0

last_light_universe_refresh = 0

def maybe_refresh_universe():
    global last_full_universe_refresh
    global last_light_universe_refresh

    now_time = time.time()

    if not UNIVERSE:
        rebuild_universe(full=True)

        last_full_universe_refresh = now_time

        last_light_universe_refresh = now_time

        return

    if should_run_full_snapshot_rebuild():
        log("Scheduled full snapshot rebuild...")

        rebuild_universe(full=True)

        last_full_universe_refresh = now_time

        last_light_universe_refresh = now_time

        return

# ==============================================================================
# Daily Summary
# ==============================================================================

def send_daily_summary():
    active = load_active_trades()

    history = redis_hgetall_json(KEY_HISTORY)

    today = today_ksa()

    today_alerts = 0

    for key, item in history.items():
        if isinstance(item, dict) and item.get("date") == today:
            today_alerts += 1

    message = f"""📊 <b>Elite Radar - ملخص اليوم</b>

📅 التاريخ: {today}

🔍 عدد جولات الفحص: {runtime_stats.get('total_scans', 0)}
📈 عدد الأسهم التي تم تقييمها: {runtime_stats.get('symbols_checked', 0)}
🚀 عدد التنبيهات المرسلة: {runtime_stats.get('alerts_sent', 0)}
👀 الصفقات النشطة: {len(active)}
📥 سجلات الفلوت: {runtime_stats.get('float_records', 0)}

🕒 آخر فحص: {runtime_stats.get('last_scan')}
📦 آخر تحديث للـ Universe: {runtime_stats.get('last_universe')}

📌 ملاحظة:
عدم إرسال تنبيه في يوم ضعيف يعتبر قرارًا صحيحًا وليس فشلًا.
"""

    send_telegram(message)


# ==============================================================================
# Weekend Analysis
# ==============================================================================

def analyze_trade_result(item):
    trade_plan = item.get("trade_plan", {})

    metrics = item.get("metrics", {})

    close_reason = item.get("close_reason", "")

    if trade_plan.get("hit_t3") or trade_plan.get("hit_high_target"):
        return "strong_win"

    if trade_plan.get("hit_t2"):
        return "win"

    if trade_plan.get("hit_t1"):
        return "small_win"

    if close_reason in [
        "stop_hit",
        "vwap_failure_before_t1",
        "volume_death_before_t1"
    ]:
        return "loss"

    return "unknown"


def weekend_analysis():
    if now_ksa().weekday() not in [4, 5]:
        return

    history = redis_hgetall_json(KEY_HISTORY)

    if not history:
        return

    wins = 0
    losses = 0
    unknown = 0

    success_reasons = {}
    failure_warnings = {}
    failure_close_reasons = {}

    for key, item in history.items():
        if not isinstance(item, dict):
            continue

        result = analyze_trade_result(item)

        metrics = item.get("metrics", {})

        if result in ["strong_win", "win", "small_win"]:
            wins += 1

            for reason in metrics.get("synergy_reasons", []):
                success_reasons[reason] = success_reasons.get(reason, 0) + 1

            if metrics.get("rvol", 0) >= 4:
                success_reasons["RVOL قوي"] = success_reasons.get("RVOL قوي", 0) + 1

            if metrics.get("volume_accel_ratio", 0) >= 2:
                success_reasons["تسارع حجم قوي"] = success_reasons.get("تسارع حجم قوي", 0) + 1

            if metrics.get("float_value") and metrics.get("float_value") <= 50_000_000:
                success_reasons["فلوت مناسب"] = success_reasons.get("فلوت مناسب", 0) + 1

        elif result == "loss":
            losses += 1

            for warning in metrics.get("warnings", []):
                failure_warnings[warning] = failure_warnings.get(warning, 0) + 1

            close_reason = item.get("close_reason", "unknown")

            failure_close_reasons[close_reason] = failure_close_reasons.get(close_reason, 0) + 1

        else:
            unknown += 1

    success_sorted = sorted(
        success_reasons.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]

    failure_sorted = sorted(
        failure_warnings.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]

    close_sorted = sorted(
        failure_close_reasons.items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]

    success_text = ""

    if success_sorted:
        for reason, count in success_sorted:
            success_text += f"• {reason}: {count}\n"
    else:
        success_text = "لا يوجد نمط نجاح واضح بعد.\n"

    failure_text = ""

    if failure_sorted:
        for reason, count in failure_sorted:
            failure_text += f"• {reason}: {count}\n"
    else:
        failure_text = "لا يوجد نمط فشل واضح بعد.\n"

    close_text = ""

    if close_sorted:
        for reason, count in close_sorted:
            close_text += f"• {reason}: {count}\n"
    else:
        close_text = "لا يوجد سبب خروج متكرر واضح.\n"

    message = f"""🧠 <b>Elite Radar - تحليل الويكند</b>

📊 <b>نتائج التنبيهات:</b>
✅ صفقات ناجحة: {wins}
❌ صفقات فاشلة: {losses}
⚪ غير محسومة: {unknown}

━━━━━━━━━━━━━━

✅ <b>أكثر عوامل النجاح تكرارًا:</b>
{success_text}

━━━━━━━━━━━━━━

⚠️ <b>أكثر عوامل الفشل تكرارًا:</b>
{failure_text}

━━━━━━━━━━━━━━

🔴 <b>أسباب الخروج المتكررة:</b>
{close_text}

━━━━━━━━━━━━━━

📌 <b>مهم:</b>
هذا التقرير لا يغير القواعد تلقائيًا.
أي تعديل على الأوزان أو الفلاتر يحتاج اعتمادك أولًا.
"""

    send_telegram(message)


# ==============================================================================
# Startup Loader
# ==============================================================================

def load_runtime_state():
    global runtime_stats

    saved = redis_get_json(KEY_RUNTIME, None)

    if isinstance(saved, dict):
        for key, value in saved.items():
            runtime_stats[key] = value


def startup_message():
    message = f"""🚀 <b>Elite Radar بدأ التشغيل</b>

🧩 الوضع: Background Worker
📥 سجلات الفلوت: {len(FLOAT_CACHE)}
📦 Universe: {len(UNIVERSE)}
⚡ Priority Universe: {len(PRIORITY_UNIVERSE)}
📋 Normal Universe: {len(NORMAL_UNIVERSE)}

🕒 الوقت: {datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")}
"""

    send_telegram(message)


def startup():
    log("Elite Radar startup sequence...")

    load_runtime_state()

    load_news_cache()

    load_float_cache()

    load_universe_from_redis()

    load_watchlist()

    load_active_trades()

    if not UNIVERSE or not PRIORITY_UNIVERSE:
        rebuild_universe(full=True)

    startup_message()

    log("Startup completed.")


# ==============================================================================
# Float Reload Scheduler
# ==============================================================================

last_float_reload_key = ""


def maybe_reload_float_cache():
    global last_float_reload_key

    current = now_ksa()

    # Temporary mode:
    # Early Explosion is still responsible for updating float_cache.json.
    # Elite Radar reloads the Gist copy at 11:00 KSA.
    if current.hour == 10 and current.minute == 45:
        key = current.strftime("%Y-%m-%d %H:%M")

        if key != last_float_reload_key:
            log("Scheduled float cache reload at 11:00 KSA...")

            load_float_cache()

            last_float_reload_key = key

last_full_snapshot_rebuild_key = ""


def should_run_full_snapshot_rebuild():
    global last_full_snapshot_rebuild_key

    current = now_ksa()

    current_time = current.strftime("%H:%M")

    if current_time not in FULL_SNAPSHOT_REBUILD_TIMES_KSA:
        return False

    key = current.strftime("%Y-%m-%d %H:%M")

    if key == last_full_snapshot_rebuild_key:
        return False

    last_full_snapshot_rebuild_key = key

    return True
    
# ==============================================================================
# Main Loop
# ==============================================================================

last_monitor_time = 0

last_daily_summary_date = ""

last_weekend_analysis_date = ""

def trade_monitor_loop():
    while True:
        try:
            monitor_active_trades()
        except Exception as e:
            log(f"Trade monitor loop error: {e}")

        time.sleep(MONITOR_INTERVAL)

def main_loop():
    global last_monitor_time
    global last_daily_summary_date
    global last_weekend_analysis_date

    startup()

    threading.Thread(
        target=trade_monitor_loop,
        daemon=True
    ).start()
    
    while True:
        try:
            current = now_ksa()

            maybe_reload_float_cache()

            # ------------------------------------------------------------------
            # Weekend mode
            # ------------------------------------------------------------------

            if is_weekend():
                if (
                    current.hour == 12
                    and today_ksa() != last_weekend_analysis_date
                ):
                    weekend_analysis()

                    last_weekend_analysis_date = today_ksa()

                log("Weekend mode. No trading alerts. Waiting...")

                time.sleep(60)

                continue

            # ------------------------------------------------------------------
            # Daily Summary
            # ------------------------------------------------------------------

            if (
                current.hour == 23
                and current.minute >= 30
                and today_ksa() != last_daily_summary_date
            ):
                send_daily_summary()

                last_daily_summary_date = today_ksa()

            # ------------------------------------------------------------------
            # Scan Window
            # ------------------------------------------------------------------

            if is_scan_window():
                maybe_refresh_universe()

                scan_watchlist()

                scan_once()

                time.sleep(SCAN_INTERVAL)

            else:
                log("Outside scan window. Monitoring only.")

                time.sleep(60)

        except KeyboardInterrupt:
            log("Elite Radar stopped manually.")
            break

        except Exception as e:
            log(f"Main loop error: {e}")
            log(traceback.format_exc())

            time.sleep(30)


# ==============================================================================
# Run
# ==============================================================================

if __name__ == "__main__":
    main_loop()
  
