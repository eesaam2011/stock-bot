import os
import json
import time
import math
import traceback
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from flask import Flask

try:
    import alpaca_trade_api as tradeapi
except Exception:
    tradeapi = None


# ============================================================
# 🚀 بوت الأهداف العالية - High Targets Bot
# Single File Version
# ============================================================

BOT_NAME = "🚀 بوت الأهداف العالية"

SAUDI_TZ = ZoneInfo("Asia/Riyadh")
NY_TZ = ZoneInfo("America/New_York")

# -----------------------------
# ENV
# -----------------------------
# يعتمد على مفاتيح Alpaca الموجودة عندك
ALPACA_API_KEY = os.getenv("APCA_API_KEY_ID", "")
ALPACA_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

FLOAT_CACHE_URL = os.getenv("FLOAT_CACHE_URL", "")

PORT = int(os.getenv("PORT", "10000"))

# -----------------------------
# SETTINGS
# -----------------------------
PRICE_MIN = 0.30
PRICE_MAX = 25.00

MIN_AVG_VOLUME = 50_000
MIN_DOLLAR_VOLUME = 300_000

BATCH_SIZE = 200
BATCH_SLEEP_SEC = 0.8

CANDIDATE_REFRESH_SEC = 10 * 60
ACTIVE_MONITOR_INTERVAL_SEC = 60

FLOAT_REFRESH_HOUR_KSA = 11
UNIVERSE_BUILD_HOUR_KSA = 12

STRONG_SCORE = 88
WATCHLIST_SCORE = 70
MAX_DAILY_PICKS = 3

# Finnhub protection
NEWS_LOOKBACK_HOURS = 24
NEWS_CACHE_TTL_SEC = 60 * 60
NEWS_SCAN_LIMIT = int(os.getenv("NEWS_SCAN_LIMIT", "250"))
FINNHUB_DELAY_SEC = float(os.getenv("FINNHUB_DELAY_SEC", "1.05"))
FINNHUB_BACKOFF_SEC = int(os.getenv("FINNHUB_BACKOFF_SEC", "900"))

# -----------------------------
# REDIS KEYS
# -----------------------------
KEY_STATE = "high_targets:state"
KEY_FLOAT_CACHE = "high_targets:float_cache"
KEY_UNIVERSE = "high_targets:universe"
KEY_WATCHLIST = "high_targets:watchlist"
KEY_STRONG = "high_targets:strong_candidates"
KEY_ACTIVE = "high_targets:active_monitoring"
KEY_SCORE_HISTORY = "high_targets:score_history"
KEY_ALERTS_SENT = "high_targets:alerts_sent"
KEY_DAILY_RANKINGS = "high_targets:daily_rankings"
KEY_NEWS_CACHE = "high_targets:news_cache"


# ============================================================
# Flask
# ============================================================

app = Flask(__name__)

runtime_stats = {
    "started_at": None,
    "last_float_refresh": None,
    "last_universe_build": None,
    "last_scan": None,
    "last_candidate_alert": None,
    "last_premarket_confirmation": None,
    "total_scans": 0,
    "float_count": 0,
    "universe_count": 0,
    "quick_stage_count": 0,
    "news_stage_count": 0,
    "watchlist_count": 0,
    "strong_count": 0,
    "active_count": 0,
    "finnhub_backoff_until": None,
}


@app.route("/")
def home():
    now_sa = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""
    <html>
    <head><title>High Targets Bot</title></head>
    <body style="font-family: Arial; line-height: 1.7;">
        <h2>{BOT_NAME}</h2>
        <p>✅ Status: Running</p>
        <p>🕒 Saudi Time: {now_sa}</p>
        <hr>
        <p>🚀 Started At: {runtime_stats.get("started_at")}</p>
        <p>📥 Last Float Refresh: {runtime_stats.get("last_float_refresh")}</p>
        <p>📊 Last Universe Build: {runtime_stats.get("last_universe_build")}</p>
        <p>🔍 Last Scan: {runtime_stats.get("last_scan")}</p>
        <p>📨 Last Candidate Alert: {runtime_stats.get("last_candidate_alert")}</p>
        <p>🌅 Last Premarket Confirmation: {runtime_stats.get("last_premarket_confirmation")}</p>
        <hr>
        <p>📊 Float Count: {runtime_stats.get("float_count")}</p>
        <p>🌎 Universe Count: {runtime_stats.get("universe_count")}</p>
        <p>⚡ Quick Stage Count: {runtime_stats.get("quick_stage_count")}</p>
        <p>📰 News Stage Count: {runtime_stats.get("news_stage_count")}</p>
        <p>👀 Watchlist Count: {runtime_stats.get("watchlist_count")}</p>
        <p>🥇 Strong Count: {runtime_stats.get("strong_count")}</p>
        <p>📈 Active Monitoring: {runtime_stats.get("active_count")}</p>
        <p>🔁 Total Scans: {runtime_stats.get("total_scans")}</p>
        <p>🛑 Finnhub Backoff Until: {runtime_stats.get("finnhub_backoff_until")}</p>
    </body>
    </html>
    """
    return html, 200


def run_flask():
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


# ============================================================
# Time / Logging
# ============================================================

def now_ksa():
    return datetime.now(SAUDI_TZ)


def now_ny():
    return datetime.now(NY_TZ)


def today_ksa_str():
    return now_ksa().strftime("%Y-%m-%d")


def log_info(msg):
    print(f"[INFO] {now_ksa().strftime('%Y-%m-%d %H:%M:%S')} | {msg}", flush=True)


def log_warn(msg):
    print(f"[WARN] {now_ksa().strftime('%Y-%m-%d %H:%M:%S')} | {msg}", flush=True)


def log_error(msg):
    print(f"[ERROR] {now_ksa().strftime('%Y-%m-%d %H:%M:%S')} | {msg}", flush=True)


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.strip() == "":
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def pct_change(old, new):
    old = safe_float(old)
    new = safe_float(new)
    if old <= 0:
        return 0.0
    return ((new - old) / old) * 100.0


def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ============================================================
# Upstash Redis
# ============================================================

def redis_enabled():
    return bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)


def redis_request(command):
    if not redis_enabled():
        log_warn("Upstash Redis not configured.")
        return None

    try:
        url = UPSTASH_REDIS_REST_URL.rstrip("/")
        headers = {
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=command, timeout=20)
        if response.status_code >= 400:
            log_error(f"Redis error {response.status_code}: {response.text[:300]}")
            return None
        data = response.json()
        return data.get("result")
    except Exception as e:
        log_error(f"Redis request failed: {e}")
        return None


def redis_set_json(key, value):
    try:
        payload = json.dumps(value, ensure_ascii=False)
        return redis_request(["SET", key, payload])
    except Exception as e:
        log_error(f"redis_set_json failed for {key}: {e}")
        return None


def redis_get_json(key, default=None):
    try:
        result = redis_request(["GET", key])
        if not result:
            return default
        return json.loads(result)
    except Exception as e:
        log_error(f"redis_get_json failed for {key}: {e}")
        return default


# ============================================================
# Telegram
# ============================================================

def telegram_enabled():
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def send_telegram(message):
    if not telegram_enabled():
        log_warn("Telegram not configured. Message skipped.")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        response = requests.post(url, data=payload, timeout=20)
        if response.status_code != 200:
            log_error(f"Telegram error {response.status_code}: {response.text[:300]}")
            return False
        return True
    except Exception as e:
        log_error(f"Telegram send failed: {e}")
        return False


def bot_header():
    return "═══════════════════════\n🚀 بوت الأهداف العالية\n═══════════════════════"


def send_startup_alert(state_summary):
    message = f"""{bot_header()}

✅ تم تشغيل البوت بنجاح

📥 سجلات الفلوت: {state_summary.get("float_count", 0)}
🌎 أسهم الكون المحفوظ: {state_summary.get("universe_count", 0)}
👀 أسهم المراقبة: {state_summary.get("watchlist_count", 0)}
🥇 المرشحين الأقوياء: {state_summary.get("strong_count", 0)}
📈 المتابعة النشطة: {state_summary.get("active_count", 0)}

💾 تم استرجاع الحالة من Upstash
🕒 الوقت: {now_ksa().strftime("%Y-%m-%d %H:%M:%S")}
"""
    send_telegram(message)


# ============================================================
# Alpaca
# ============================================================

api = None

def init_alpaca():
    global api

    if tradeapi is None:
        log_error("alpaca_trade_api package not installed.")
        return None

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log_error("Alpaca keys are missing.")
        return None

    try:
        api = tradeapi.REST(
            ALPACA_API_KEY,
            ALPACA_SECRET_KEY,
            ALPACA_BASE_URL,
            api_version="v2"
        )
        log_info("Alpaca initialized.")
        return api
    except Exception as e:
        log_error(f"Alpaca init failed: {e}")
        return None


def get_alpaca_api():
    global api
    if api is None:
        return init_alpaca()
    return api


# ============================================================
# Filters
# ============================================================

SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC", "PNC", "COF", "DFS",
    "MET", "PRU", "ALL", "AIG", "AFL", "TRV", "HIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR",
    "BUD", "TAP", "STZ", "DEO", "PM", "MO", "BTI",
    "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL", "AMC", "CNK", "IMAX", "HITI",
}

BAD_NAME_KEYWORDS = [
    "ETF", "ETN", "FUND", "TRUST", "INDEX", "WARRANT", "UNIT", "RIGHT",
    "PREFERRED", "PREF", "NOTE", "BOND", "SPAC", "BLANK CHECK",
    "ACQUISITION", "ACQUISITION CORP",
]

BAD_SUFFIXES = ("W", "U", "R", "P", "Q", "Z")


def is_clean_symbol(symbol):
    if not symbol:
        return False
    symbol = symbol.strip().upper()
    if len(symbol) > 5:
        return False
    if not symbol.isalpha():
        return False
    for bad in [".", "-", "/", "^"]:
        if bad in symbol:
            return False
    if len(symbol) >= 2 and symbol.endswith(BAD_SUFFIXES):
        return False
    return True


def is_blacklisted(symbol, name=""):
    symbol = (symbol or "").upper().strip()
    name = (name or "").upper().strip()

    if symbol in SYMBOL_BLACKLIST:
        return True

    for kw in BAD_NAME_KEYWORDS:
        if kw in name:
            return True

    return False


# ============================================================
# Float Cache
# ============================================================

float_cache = {}


def normalize_float_cache(raw):
    cleaned = {}

    if not isinstance(raw, dict):
        return cleaned

    for symbol, value in raw.items():
        sym = str(symbol).upper().strip()
        if not sym:
            continue

        if isinstance(value, dict):
            float_value = (
                value.get("float")
                or value.get("shareFloat")
                or value.get("freeFloat")
                or value.get("floatShares")
            )
        else:
            float_value = value

        fv = safe_float(float_value, 0)
        if fv > 0:
            cleaned[sym] = fv

    return cleaned


def load_float_cache_from_source():
    global float_cache

    log_info("📥 Loading float cache...")

    raw = None

    if FLOAT_CACHE_URL:
        try:
            response = requests.get(FLOAT_CACHE_URL, timeout=30)
            if response.status_code == 200:
                raw = response.json()
            else:
                log_warn(f"Float cache URL error {response.status_code}")
        except Exception as e:
            log_warn(f"Float cache URL load failed: {e}")

    if raw is None:
        stored = redis_get_json(KEY_FLOAT_CACHE, default={})
        if stored:
            raw = stored

    if raw is None:
        raw = {}

    cleaned = normalize_float_cache(raw)
    float_cache = cleaned

    redis_set_json(KEY_FLOAT_CACHE, cleaned)

    runtime_stats["last_float_refresh"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    runtime_stats["float_count"] = len(cleaned)

    state = load_state()
    state["last_float_refresh_date"] = today_ksa_str()
    state["last_float_refresh_at"] = runtime_stats["last_float_refresh"]
    save_state(state)

    log_info(f"✅ Float records loaded: {len(cleaned)}")
    return cleaned


def get_float(symbol):
    symbol = (symbol or "").upper().strip()
    return safe_float(float_cache.get(symbol, 0), 0)


# ============================================================
# State
# ============================================================

def default_state():
    return {
        "bot_name": BOT_NAME,
        "version": "1.1-rate-limited-news",
        "created_at": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
        "last_float_refresh_date": None,
        "last_universe_build_date": None,
        "last_candidate_alert_date": None,
        "last_premarket_confirmation_date": None,
        "last_saved_at": None,
    }


def load_state():
    state = redis_get_json(KEY_STATE, default=None)
    if not isinstance(state, dict):
        state = default_state()
    return state


def save_state(state):
    state["last_saved_at"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    redis_set_json(KEY_STATE, state)


def load_runtime_collections():
    universe = redis_get_json(KEY_UNIVERSE, default=[])
    watchlist = redis_get_json(KEY_WATCHLIST, default={})
    strong = redis_get_json(KEY_STRONG, default={})
    active = redis_get_json(KEY_ACTIVE, default={})
    rankings = redis_get_json(KEY_DAILY_RANKINGS, default=[])

    if not isinstance(universe, list):
        universe = []
    if not isinstance(watchlist, dict):
        watchlist = {}
    if not isinstance(strong, dict):
        strong = {}
    if not isinstance(active, dict):
        active = {}
    if not isinstance(rankings, list):
        rankings = []

    runtime_stats["universe_count"] = len(universe)
    runtime_stats["watchlist_count"] = len(watchlist)
    runtime_stats["strong_count"] = len(strong)
    runtime_stats["active_count"] = len(active)

    return {
        "universe": universe,
        "watchlist": watchlist,
        "strong": strong,
        "active": active,
        "rankings": rankings,
    }


# ============================================================
# Universe Builder
# ============================================================

def get_latest_trade_price(symbol):
    client = get_alpaca_api()
    if client is None:
        return 0.0

    try:
        snapshot = client.get_snapshot(symbol)
        if snapshot is None:
            return 0.0

        price = 0.0

        if getattr(snapshot, "latest_trade", None):
            price = safe_float(snapshot.latest_trade.p, 0)

        if price <= 0 and getattr(snapshot, "daily_bar", None):
            price = safe_float(snapshot.daily_bar.c, 0)

        return price
    except Exception:
        return 0.0


def build_daily_universe(force=False):
    state = load_state()
    today = today_ksa_str()

    if not force and state.get("last_universe_build_date") == today:
        saved = redis_get_json(KEY_UNIVERSE, default=[])
        if saved:
            log_info(f"Universe already built today. Count: {len(saved)}")
            runtime_stats["universe_count"] = len(saved)
            return saved

    client = get_alpaca_api()
    if client is None:
        log_error("Cannot build universe: Alpaca not ready.")
        return []

    log_info("🌎 Building daily universe...")

    try:
        assets = client.list_assets(status="active")
    except Exception as e:
        log_error(f"Failed to list Alpaca assets: {e}")
        return []

    total_assets = len(assets)
    clean_count = 0
    blacklist_removed = 0
    tradable_removed = 0

    universe = []

    for asset in assets:
        try:
            symbol = str(asset.symbol).upper().strip()
            name = str(getattr(asset, "name", "") or "")

            if not getattr(asset, "tradable", False):
                tradable_removed += 1
                continue

            if not is_clean_symbol(symbol):
                continue

            clean_count += 1

            if is_blacklisted(symbol, name):
                blacklist_removed += 1
                continue

            universe.append({
                "symbol": symbol,
                "name": name,
                "exchange": str(getattr(asset, "exchange", "") or ""),
            })

        except Exception as e:
            log_warn(f"Asset filter error: {e}")

    log_info("==================================================")
    log_info("🌎 Universe Build Results")
    log_info(f"📊 Total Assets: {total_assets}")
    log_info(f"✅ Clean Symbols: {clean_count}")
    log_info(f"🚫 Not Tradable Removed: {tradable_removed}")
    log_info(f"🚫 Blacklist/Bad Name Removed: {blacklist_removed}")
    log_info(f"✅ Final Universe Before Price Filter: {len(universe)}")
    log_info("==================================================")

    filtered = []

    for batch_index, batch in enumerate(chunk_list(universe, BATCH_SIZE), start=1):
        log_info(f"📦 Universe Price Batch {batch_index} | Symbols: {len(batch)}")

        for item in batch:
            symbol = item["symbol"]
            try:
                price = get_latest_trade_price(symbol)
                if price <= 0:
                    continue

                if price < PRICE_MIN or price > PRICE_MAX:
                    continue

                item["last_price"] = round(price, 4)
                filtered.append(item)

            except Exception as e:
                log_warn(f"Price filter failed for {symbol}: {e}")

        time.sleep(BATCH_SLEEP_SEC)

    redis_set_json(KEY_UNIVERSE, filtered)

    state["last_universe_build_date"] = today
    state["last_universe_build_at"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)

    runtime_stats["last_universe_build"] = state["last_universe_build_at"]
    runtime_stats["universe_count"] = len(filtered)

    log_info(f"✅ Final Universe Saved: {len(filtered)}")
    return filtered


def load_universe():
    universe = redis_get_json(KEY_UNIVERSE, default=[])
    if not isinstance(universe, list):
        universe = []
    return universe


# ============================================================
# Market Data Helpers
# ============================================================

def get_daily_bars(symbol, limit=60):
    client = get_alpaca_api()
    if client is None:
        return pd.DataFrame()

    try:
        bars = client.get_bars(symbol, "1Day", limit=limit).df
        if bars is None or bars.empty:
            return pd.DataFrame()
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol)
        return bars.copy()
    except Exception:
        return pd.DataFrame()


def get_minute_bars(symbol, limit=120):
    client = get_alpaca_api()
    if client is None:
        return pd.DataFrame()

    try:
        bars = client.get_bars(symbol, "1Min", limit=limit).df
        if bars is None or bars.empty:
            return pd.DataFrame()
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol)
        return bars.copy()
    except Exception:
        return pd.DataFrame()


def get_snapshot_data(symbol):
    client = get_alpaca_api()
    if client is None:
        return {}

    try:
        snapshot = client.get_snapshot(symbol)

        data = {
            "price": 0.0,
            "prev_close": 0.0,
            "daily_volume": 0,
        }

        if snapshot is None:
            return data

        if getattr(snapshot, "latest_trade", None):
            data["price"] = safe_float(snapshot.latest_trade.p, 0)

        if getattr(snapshot, "daily_bar", None):
            data["daily_volume"] = safe_int(snapshot.daily_bar.v, 0)
            if data["price"] <= 0:
                data["price"] = safe_float(snapshot.daily_bar.c, 0)

        if getattr(snapshot, "prev_daily_bar", None):
            data["prev_close"] = safe_float(snapshot.prev_daily_bar.c, 0)

        return data
    except Exception:
        return {}


# ============================================================
# News - Finnhub with Rate Limit
# ============================================================

POSITIVE_NEWS_KEYWORDS = [
    "fda", "approval", "approved", "clearance", "contract", "agreement",
    "partnership", "collaboration", "merger", "acquisition", "buyout",
    "patent", "clinical", "phase", "trial", "results", "earnings",
    "revenue", "guidance", "order", "government", "award", "license",
]

NEGATIVE_NEWS_KEYWORDS = [
    "offering", "dilution", "bankruptcy", "delisting", "reverse split",
    "investigation", "lawsuit", "sec", "nasdaq notice", "withdrawal",
]

finnhub_last_request_ts = 0.0
finnhub_backoff_until_ts = 0.0


def get_news_cache():
    cache = redis_get_json(KEY_NEWS_CACHE, default={})
    if not isinstance(cache, dict):
        cache = {}
    return cache


def save_news_cache(cache):
    redis_set_json(KEY_NEWS_CACHE, cache)


def get_company_news(symbol, allow_request=True):
    global finnhub_last_request_ts, finnhub_backoff_until_ts

    symbol = symbol.upper().strip()
    cache = get_news_cache()
    now_ts = time.time()

    cached = cache.get(symbol)
    if cached and now_ts - cached.get("ts", 0) < NEWS_CACHE_TTL_SEC:
        return cached.get("news", [])

    if not allow_request:
        return []

    if not FINNHUB_API_KEY:
        return []

    if now_ts < finnhub_backoff_until_ts:
        return []

    elapsed = now_ts - finnhub_last_request_ts
    if elapsed < FINNHUB_DELAY_SEC:
        time.sleep(FINNHUB_DELAY_SEC - elapsed)

    try:
        end_date = datetime.utcnow().date()
        start_date = end_date - timedelta(hours=NEWS_LOOKBACK_HOURS)

        url = "https://finnhub.io/api/v1/company-news"
        params = {
            "symbol": symbol,
            "from": start_date.strftime("%Y-%m-%d"),
            "to": end_date.strftime("%Y-%m-%d"),
            "token": FINNHUB_API_KEY,
        }

        finnhub_last_request_ts = time.time()
        response = requests.get(url, params=params, timeout=20)

        if response.status_code == 429:
            finnhub_backoff_until_ts = time.time() + FINNHUB_BACKOFF_SEC
            backoff_until = datetime.fromtimestamp(finnhub_backoff_until_ts, SAUDI_TZ).strftime("%Y-%m-%d %H:%M:%S")
            runtime_stats["finnhub_backoff_until"] = backoff_until
            log_warn(f"Finnhub 429 rate limit. News paused until {backoff_until}")
            return []

        if response.status_code != 200:
            log_warn(f"Finnhub news error for {symbol}: {response.status_code}")
            return []

        news = response.json()
        if not isinstance(news, list):
            news = []

        cache[symbol] = {"ts": time.time(), "news": news[:10]}
        save_news_cache(cache)

        return news[:10]

    except Exception as e:
        log_warn(f"News fetch failed for {symbol}: {e}")
        return []


def score_news_catalyst(symbol, allow_request=True):
    news = get_company_news(symbol, allow_request=allow_request)
    if not news:
        return 0, "لا يوجد خبر حديث واضح", None

    best_score = 0
    best_title = None

    for item in news:
        title = str(item.get("headline", "") or "")
        summary = str(item.get("summary", "") or "")
        combined = f"{title} {summary}".lower()

        score = 0

        if any(kw in combined for kw in NEGATIVE_NEWS_KEYWORDS):
            score -= 10

        positive_hits = sum(1 for kw in POSITIVE_NEWS_KEYWORDS if kw in combined)

        if positive_hits >= 4:
            score += 25
        elif positive_hits >= 3:
            score += 22
        elif positive_hits >= 2:
            score += 17
        elif positive_hits >= 1:
            score += 10

        if score > best_score:
            best_score = score
            best_title = title

    best_score = max(0, min(25, best_score))

    if best_score >= 22:
        reason = "خبر قوي جداً"
    elif best_score >= 15:
        reason = "خبر إيجابي جيد"
    elif best_score >= 10:
        reason = "محفز خبري متوسط"
    else:
        reason = "لا يوجد محفز قوي"

    return best_score, reason, best_title


# ============================================================
# Scoring Engine
# ============================================================

def score_float(symbol):
    fv = get_float(symbol)

    if fv <= 0:
        return 0, "Float غير متوفر"

    if fv <= 5_000_000:
        return 20, f"Float منخفض جداً: {fv/1_000_000:.1f}M"
    if fv <= 10_000_000:
        return 18, f"Float منخفض: {fv/1_000_000:.1f}M"
    if fv <= 20_000_000:
        return 15, f"Float جيد: {fv/1_000_000:.1f}M"
    if fv <= 50_000_000:
        return 10, f"Float متوسط: {fv/1_000_000:.1f}M"
    if fv <= 100_000_000:
        return 5, f"Float مرتفع نسبياً: {fv/1_000_000:.1f}M"

    return 0, f"Float عالي: {fv/1_000_000:.1f}M"


def score_volume_creep(symbol):
    bars = get_daily_bars(symbol, limit=40)
    if bars.empty or len(bars) < 15:
        return 0, "بيانات الحجم غير كافية"

    try:
        vols = bars["volume"].astype(float)
        avg_30 = vols.tail(30).mean()
        recent_7 = vols.tail(7).mean()
        recent_3 = vols.tail(3).mean()

        if avg_30 <= 0:
            return 0, "متوسط الحجم غير صالح"

        ratio_7 = recent_7 / avg_30
        ratio_3 = recent_3 / avg_30
        trend_up = recent_3 >= recent_7

        score = 0

        if ratio_7 >= 2.5 and ratio_3 >= 3.0 and trend_up:
            score = 15
        elif ratio_7 >= 2.0 and ratio_3 >= 2.5:
            score = 13
        elif ratio_7 >= 1.5 and ratio_3 >= 2.0:
            score = 10
        elif ratio_7 >= 1.3:
            score = 7
        elif ratio_7 >= 1.1:
            score = 4

        reason = f"Volume Creep: 7D={ratio_7:.2f}x / 3D={ratio_3:.2f}x"
        return score, reason
    except Exception:
        return 0, "خطأ في حساب Volume Creep"


def score_compression(symbol):
    bars = get_daily_bars(symbol, limit=30)
    if bars.empty or len(bars) < 12:
        return 0, "بيانات الضغط غير كافية"

    try:
        recent = bars.tail(10).copy()

        high = recent["high"].astype(float)
        low = recent["low"].astype(float)
        close = recent["close"].astype(float)

        ranges = (high - low) / close.replace(0, pd.NA)
        avg_range_recent = ranges.tail(5).mean()
        avg_range_prev = ranges.head(5).mean()

        higher_lows = low.tail(5).iloc[-1] >= low.tail(5).iloc[0]
        near_high = close.iloc[-1] >= high.max() * 0.92

        score = 0

        if avg_range_recent < avg_range_prev * 0.75:
            score += 4
        elif avg_range_recent < avg_range_prev * 0.90:
            score += 2

        if higher_lows:
            score += 3

        if near_high:
            score += 3

        score = min(10, score)

        reason = "ضغط سعري جيد" if score >= 7 else "ضغط سعري متوسط" if score >= 4 else "ضغط ضعيف"
        return score, reason
    except Exception:
        return 0, "خطأ في حساب الضغط السعري"


def score_after_hours(symbol):
    snap = get_snapshot_data(symbol)
    price = safe_float(snap.get("price", 0), 0)
    prev_close = safe_float(snap.get("prev_close", 0), 0)
    volume = safe_int(snap.get("daily_volume", 0), 0)

    if price <= 0 or prev_close <= 0:
        return 0, "بيانات After Hours غير كافية"

    change_pct = pct_change(prev_close, price)

    score = 0

    if change_pct >= 30 and volume >= 2_000_000:
        score = 15
    elif change_pct >= 20 and volume >= 1_000_000:
        score = 13
    elif change_pct >= 15 and volume >= 500_000:
        score = 10
    elif change_pct >= 10 and volume >= 250_000:
        score = 7
    elif change_pct >= 5 and volume >= 100_000:
        score = 4

    reason = f"Extended: {change_pct:.1f}% | Vol={volume:,}"
    return score, reason


def score_premarket_validation(symbol):
    ny = now_ny()
    if not (ny.hour >= 4 and (ny.hour < 9 or (ny.hour == 9 and ny.minute < 30))):
        return 0, "ليست فترة Premarket"

    bars = get_minute_bars(symbol, limit=30)
    if bars.empty or len(bars) < 10:
        return 0, "بيانات Premarket غير كافية"

    try:
        vols = bars["volume"].astype(float)
        closes = bars["close"].astype(float)

        recent_vol = vols.tail(5).mean()
        prev_vol = vols.head(max(1, len(vols) - 5)).mean()

        price_change = pct_change(closes.iloc[0], closes.iloc[-1])
        vol_ratio = recent_vol / prev_vol if prev_vol > 0 else 0

        score = 0

        if price_change >= 10 and vol_ratio >= 3:
            score = 10
        elif price_change >= 7 and vol_ratio >= 2:
            score = 8
        elif price_change >= 4 and vol_ratio >= 1.5:
            score = 6
        elif price_change >= 2:
            score = 3

        reason = f"Premarket: {price_change:.1f}% | VolRatio={vol_ratio:.2f}x"
        return score, reason
    except Exception:
        return 0, "خطأ في حساب Premarket"


def score_historical_similarity(symbol, factor_snapshot):
    history = redis_get_json(KEY_SCORE_HISTORY, default={})
    if not isinstance(history, dict) or not history:
        return 0, "لا توجد قاعدة تشابه كافية"

    try:
        best_similarity = 0

        current = {
            "float_score": factor_snapshot.get("float_score", 0),
            "news_score": factor_snapshot.get("news_score", 0),
            "volume_score": factor_snapshot.get("volume_score", 0),
            "compression_score": factor_snapshot.get("compression_score", 0),
            "after_hours_score": factor_snapshot.get("after_hours_score", 0),
        }

        for _, record in list(history.items())[-200:]:
            if not isinstance(record, dict):
                continue

            past = record.get("factor_snapshot", {})
            if not past:
                continue

            diff = 0
            diff += abs(current["float_score"] - past.get("float_score", 0)) / 20
            diff += abs(current["news_score"] - past.get("news_score", 0)) / 25
            diff += abs(current["volume_score"] - past.get("volume_score", 0)) / 15
            diff += abs(current["compression_score"] - past.get("compression_score", 0)) / 10
            diff += abs(current["after_hours_score"] - past.get("after_hours_score", 0)) / 15

            similarity = max(0, 1 - (diff / 5))
            best_similarity = max(best_similarity, similarity)

        if best_similarity >= 0.80:
            return 5, f"تشابه تاريخي قوي: {best_similarity:.0%}"
        if best_similarity >= 0.65:
            return 3, f"تشابه تاريخي متوسط: {best_similarity:.0%}"

        return 0, "تشابه تاريخي ضعيف"
    except Exception:
        return 0, "خطأ في حساب التشابه التاريخي"


def classify_score(score):
    if score >= STRONG_SCORE:
        return "strong"
    if score >= WATCHLIST_SCORE:
        return "watchlist"
    return "ignored"


def classify_arabic(status):
    if status == "strong":
        return "🥇 مرشح قوي"
    if status == "watchlist":
        return "👀 مرشح مراقبة"
    return "❌ تجاهل"


def evaluate_symbol(symbol, include_news=False):
    symbol = symbol.upper().strip()

    float_score, float_reason = score_float(symbol)
    volume_score, volume_reason = score_volume_creep(symbol)
    compression_score, compression_reason = score_compression(symbol)
    after_hours_score, after_hours_reason = score_after_hours(symbol)
    premarket_score, premarket_reason = score_premarket_validation(symbol)

    if include_news:
        news_score, news_reason, news_title = score_news_catalyst(symbol, allow_request=True)
    else:
        news_score, news_reason, news_title = 0, "لم يتم فحص الأخبار في المرحلة السريعة", None

    partial_snapshot = {
        "float_score": float_score,
        "news_score": news_score,
        "volume_score": volume_score,
        "compression_score": compression_score,
        "after_hours_score": after_hours_score,
        "premarket_score": premarket_score,
    }

    similarity_score, similarity_reason = score_historical_similarity(symbol, partial_snapshot)

    total = (
        float_score
        + news_score
        + volume_score
        + compression_score
        + after_hours_score
        + premarket_score
        + similarity_score
    )

    total = int(max(0, min(100, total)))
    status = classify_score(total)

    snap = get_snapshot_data(symbol)
    price = safe_float(snap.get("price", 0), 0)
    prev_close = safe_float(snap.get("prev_close", 0), 0)

    result = {
        "symbol": symbol,
        "score": total,
        "status": status,
        "status_ar": classify_arabic(status),
        "price": price,
        "prev_close": prev_close,
        "change_pct": round(pct_change(prev_close, price), 2) if prev_close > 0 else 0,
        "evaluated_at": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
        "include_news": include_news,
        "factor_scores": {
            "float": float_score,
            "news": news_score,
            "volume_creep": volume_score,
            "compression": compression_score,
            "after_hours": after_hours_score,
            "premarket": premarket_score,
            "similarity": similarity_score,
        },
        "factor_reasons": {
            "float": float_reason,
            "news": news_reason,
            "volume_creep": volume_reason,
            "compression": compression_reason,
            "after_hours": after_hours_reason,
            "premarket": premarket_reason,
            "similarity": similarity_reason,
        },
        "news_title": news_title,
    }

    mode = "FINAL" if include_news else "QUICK"
    log_info(
        f"🔍 {mode} {symbol} | Score={total}/100 | {result['status_ar']} | "
        f"F={float_score} N={news_score} V={volume_score} C={compression_score} "
        f"AH={after_hours_score} PM={premarket_score} S={similarity_score}"
    )

    return result


# ============================================================
# Candidate Engine - Two Stage Evaluation
# ============================================================

def save_candidates(watchlist, strong, rankings):
    redis_set_json(KEY_WATCHLIST, watchlist)
    redis_set_json(KEY_STRONG, strong)
    redis_set_json(KEY_DAILY_RANKINGS, rankings)

    runtime_stats["watchlist_count"] = len(watchlist)
    runtime_stats["strong_count"] = len(strong)


def save_score_history_record(symbol, evaluation):
    history = redis_get_json(KEY_SCORE_HISTORY, default={})
    if not isinstance(history, dict):
        history = {}

    key = f"{today_ksa_str()}:{symbol}:{int(time.time())}"

    history[key] = {
        "symbol": symbol,
        "score": evaluation.get("score", 0),
        "status": evaluation.get("status", ""),
        "price": evaluation.get("price", 0),
        "evaluated_at": evaluation.get("evaluated_at"),
        "factor_snapshot": {
            "float_score": evaluation.get("factor_scores", {}).get("float", 0),
            "news_score": evaluation.get("factor_scores", {}).get("news", 0),
            "volume_score": evaluation.get("factor_scores", {}).get("volume_creep", 0),
            "compression_score": evaluation.get("factor_scores", {}).get("compression", 0),
            "after_hours_score": evaluation.get("factor_scores", {}).get("after_hours", 0),
            "premarket_score": evaluation.get("factor_scores", {}).get("premarket", 0),
            "similarity_score": evaluation.get("factor_scores", {}).get("similarity", 0),
        },
    }

    if len(history) > 3000:
        keys = list(history.keys())[-2500:]
        history = {k: history[k] for k in keys}

    redis_set_json(KEY_SCORE_HISTORY, history)


def evaluate_universe_cycle():
    universe = load_universe()

    if not universe:
        log_warn("Universe is empty. Building now...")
        universe = build_daily_universe(force=True)

    if not universe:
        log_warn("No universe available for evaluation.")
        return

    log_info("==================================================")
    log_info("🔍 Starting Two-Stage Universe Evaluation")
    log_info(f"🌎 Total Universe: {len(universe)}")
    log_info("==================================================")

    quick_rankings = []
    total_batches = math.ceil(len(universe) / BATCH_SIZE)

    # Stage 1: quick score without Finnhub
    for batch_num, batch in enumerate(chunk_list(universe, BATCH_SIZE), start=1):
        log_info(f"📦 Quick Score Batch {batch_num}/{total_batches} | Symbols: {len(batch)}")

        for item in batch:
            symbol = item.get("symbol", "").upper().strip()
            if not symbol:
                continue

            try:
                evaluation = evaluate_symbol(symbol, include_news=False)

                # المرحلة السريعة لا تعتمد على خبر، لذلك نحفظها للترتيب فقط
                quick_rankings.append(evaluation)

            except Exception as e:
                log_error(f"Quick evaluation failed for {symbol}: {e}")
                log_error(traceback.format_exc())

        time.sleep(BATCH_SLEEP_SEC)

    quick_rankings = sorted(quick_rankings, key=lambda x: x.get("score", 0), reverse=True)

    # لا نرسل Finnhub لكل الأسهم. فقط أفضل المرشحين سريعاً.
    news_stage = quick_rankings[:NEWS_SCAN_LIMIT]
    runtime_stats["quick_stage_count"] = len(quick_rankings)
    runtime_stats["news_stage_count"] = len(news_stage)

    log_info("==================================================")
    log_info(f"🌎 Total Universe: {len(universe)}")
    log_info(f"⚡ Quick Score Completed: {len(quick_rankings)}")
    log_info(f"📰 News Stage Limit: {len(news_stage)}")
    log_info("==================================================")

    watchlist = redis_get_json(KEY_WATCHLIST, default={})
    strong = redis_get_json(KEY_STRONG, default={})

    if not isinstance(watchlist, dict):
        watchlist = {}
    if not isinstance(strong, dict):
        strong = {}

    final_rankings = []

    # Stage 2: final score with Finnhub only for limited top candidates
    for idx, row in enumerate(news_stage, start=1):
        symbol = row.get("symbol", "")
        try:
            log_info(f"📰 Processing News Stage {idx}/{len(news_stage)} | {symbol}")
            evaluation = evaluate_symbol(symbol, include_news=True)
            status = evaluation.get("status")
            score = evaluation.get("score", 0)

            save_score_history_record(symbol, evaluation)

            final_row = {
                "symbol": symbol,
                "score": score,
                "status": status,
                "price": evaluation.get("price", 0),
                "change_pct": evaluation.get("change_pct", 0),
                "evaluated_at": evaluation.get("evaluated_at"),
                "news_title": evaluation.get("news_title"),
                "factor_scores": evaluation.get("factor_scores"),
                "factor_reasons": evaluation.get("factor_reasons"),
            }

            final_rankings.append(final_row)

            if status == "strong":
                strong[symbol] = evaluation
                watchlist.pop(symbol, None)
            elif status == "watchlist":
                if symbol not in strong:
                    watchlist[symbol] = evaluation
            else:
                watchlist.pop(symbol, None)
                strong.pop(symbol, None)

        except Exception as e:
            log_error(f"Final evaluation failed for {symbol}: {e}")
            log_error(traceback.format_exc())

    final_rankings = sorted(final_rankings, key=lambda x: x.get("score", 0), reverse=True)
    save_candidates(watchlist, strong, final_rankings)

    runtime_stats["last_scan"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    runtime_stats["total_scans"] += 1

    log_info("==================================================")
    log_info("🏆 Final Top Candidates After News Stage")
    for i, row in enumerate(final_rankings[:10], start=1):
        log_info(f"{i}. {row.get('symbol')} = {row.get('score')} | {classify_arabic(row.get('status'))}")
    log_info(f"👀 Watchlist: {len(watchlist)}")
    log_info(f"🥇 Strong Candidates: {len(strong)}")
    log_info("==================================================")


def already_alerted(alert_key):
    alerts = redis_get_json(KEY_ALERTS_SENT, default={})
    if not isinstance(alerts, dict):
        alerts = {}
    return bool(alerts.get(alert_key))


def mark_alert_sent(alert_key):
    alerts = redis_get_json(KEY_ALERTS_SENT, default={})
    if not isinstance(alerts, dict):
        alerts = {}
    alerts[alert_key] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    redis_set_json(KEY_ALERTS_SENT, alerts)


# ============================================================
# Targets / Risk
# ============================================================

def calculate_targets(symbol, entry_price):
    bars = get_daily_bars(symbol, limit=20)

    atr = 0.0

    try:
        if not bars.empty and len(bars) >= 10:
            high = bars["high"].astype(float)
            low = bars["low"].astype(float)
            close = bars["close"].astype(float)
            prev_close = close.shift(1)

            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = safe_float(tr.tail(14).mean(), 0)
    except Exception:
        atr = 0.0

    if atr <= 0:
        atr = entry_price * 0.08

    stop = max(0.01, entry_price - (atr * 0.70))
    t1 = entry_price + (atr * 0.80)
    t2 = entry_price + (atr * 1.50)
    t3 = entry_price + (atr * 2.50)
    extended = entry_price + (atr * 4.00)

    return {
        "entry": round(entry_price, 4),
        "stop": round(stop, 4),
        "t1": round(t1, 4),
        "t2": round(t2, 4),
        "t3": round(t3, 4),
        "extended": round(extended, 4),
        "atr": round(atr, 4),
    }


def format_price(value):
    value = safe_float(value, 0)
    if value < 1:
        return f"{value:.4f}"
    if value < 10:
        return f"{value:.3f}"
    return f"{value:.2f}"


# ============================================================
# Alerts
# ============================================================

def make_candidate_alert(row, targets):
    reasons = row.get("factor_reasons", {}) or {}
    scores = row.get("factor_scores", {}) or {}

    news_title = row.get("news_title") or "لا يوجد عنوان خبر واضح"

    message = f"""{bot_header()}

🔥 أفضل مرشح للأهداف العالية

السهم: {row.get("symbol")}
الدرجة: {row.get("score")}/100
التصنيف: {classify_arabic(row.get("status"))}

📍 منطقة الدخول: {format_price(targets.get("entry"))}
🛑 وقف الخسارة: {format_price(targets.get("stop"))}

🎯 الهدف الأول: {format_price(targets.get("t1"))}
🎯 الهدف الثاني: {format_price(targets.get("t2"))}
🎯 الهدف الثالث: {format_price(targets.get("t3"))}

🚀 الهدف الممتد المتوقع: {format_price(targets.get("extended"))}

📊 تفاصيل الدرجة:
• Float: {scores.get("float", 0)}/20 — {reasons.get("float", "")}
• News: {scores.get("news", 0)}/25 — {reasons.get("news", "")}
• Volume Creep: {scores.get("volume_creep", 0)}/15 — {reasons.get("volume_creep", "")}
• Compression: {scores.get("compression", 0)}/10 — {reasons.get("compression", "")}
• After Hours: {scores.get("after_hours", 0)}/15 — {reasons.get("after_hours", "")}
• Premarket: {scores.get("premarket", 0)}/10 — {reasons.get("premarket", "")}
• Similarity: {scores.get("similarity", 0)}/5 — {reasons.get("similarity", "")}

📰 الخبر:
{news_title}

📌 ملاحظة:
هذا مرشح عالي الحركة، وسيتم مراقبته ورفع الوقف أو الخروج عند ضعف الزخم.
"""
    return message


def send_daily_candidates_alert():
    state = load_state()
    today = today_ksa_str()

    if state.get("last_candidate_alert_date") == today:
        log_info("Daily candidate alert already sent today.")
        return

    rankings = redis_get_json(KEY_DAILY_RANKINGS, default=[])
    if not isinstance(rankings, list) or not rankings:
        log_warn("No rankings available for daily alert.")
        return

    top = [r for r in rankings if r.get("score", 0) >= STRONG_SCORE][:MAX_DAILY_PICKS]

    if not top:
        log_warn("No strong candidates >= 88 for daily alert.")
        return

    active = redis_get_json(KEY_ACTIVE, default={})
    if not isinstance(active, dict):
        active = {}

    sent_count = 0

    for row in top:
        symbol = row.get("symbol")
        alert_key = f"daily_candidate:{today}:{symbol}"

        if already_alerted(alert_key):
            continue

        entry = safe_float(row.get("price", 0), 0)
        if entry <= 0:
            continue

        targets = calculate_targets(symbol, entry)
        msg = make_candidate_alert(row, targets)

        if send_telegram(msg):
            mark_alert_sent(alert_key)
            sent_count += 1

            active[symbol] = {
                "symbol": symbol,
                "opened_from": "daily_candidate",
                "score": row.get("score"),
                "status": "active",
                "entry": targets.get("entry"),
                "stop": targets.get("stop"),
                "current_stop": targets.get("stop"),
                "t1": targets.get("t1"),
                "t2": targets.get("t2"),
                "t3": targets.get("t3"),
                "extended": targets.get("extended"),
                "t1_hit": False,
                "t2_hit": False,
                "t3_hit": False,
                "extended_active": True,
                "created_at": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
                "last_update": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
            }

    redis_set_json(KEY_ACTIVE, active)
    runtime_stats["active_count"] = len(active)

    if sent_count > 0:
        state["last_candidate_alert_date"] = today
        state["last_candidate_alert_at"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        runtime_stats["last_candidate_alert"] = state["last_candidate_alert_at"]

    log_info(f"Daily candidate alerts sent: {sent_count}")


def make_premarket_confirmation_alert(row):
    reasons = row.get("factor_reasons", {}) or {}
    scores = row.get("factor_scores", {}) or {}

    message = f"""{bot_header()}

🌅 تأكيد البري ماركت

السهم: {row.get("symbol")}
الدرجة الحالية: {row.get("score")}/100
التصنيف: {classify_arabic(row.get("status"))}

📌 تقييم البري ماركت:
• Premarket: {scores.get("premarket", 0)}/10 — {reasons.get("premarket", "")}
• After Hours: {scores.get("after_hours", 0)}/15 — {reasons.get("after_hours", "")}
• Volume Creep: {scores.get("volume_creep", 0)}/15 — {reasons.get("volume_creep", "")}

✅ الخلاصة:
المرشح ما زال صالحاً للمراقبة طالما لم يكسر الوقف والزخم مستمر.
"""
    return message


def send_premarket_confirmation_if_due():
    ny = now_ny()
    state = load_state()
    today = today_ksa_str()

    if state.get("last_premarket_confirmation_date") == today:
        return

    if not (ny.hour == 4 and ny.minute >= 15):
        return

    strong = redis_get_json(KEY_STRONG, default={})
    if not isinstance(strong, dict) or not strong:
        log_warn("No strong candidates for premarket confirmation.")
        return

    sent = 0
    sorted_strong = sorted(strong.values(), key=lambda x: x.get("score", 0), reverse=True)[:MAX_DAILY_PICKS]

    for row in sorted_strong:
        symbol = row.get("symbol")
        try:
            evaluation = evaluate_symbol(symbol, include_news=True)
            if evaluation.get("score", 0) >= STRONG_SCORE:
                msg = make_premarket_confirmation_alert(evaluation)
                if send_telegram(msg):
                    sent += 1
            else:
                cancel_msg = f"""{bot_header()}

❌ إلغاء مرشح البري ماركت

السهم: {symbol}
الدرجة الحالية: {evaluation.get("score", 0)}/100

الأسباب:
• تراجع التصنيف عن مستوى المرشح القوي
• لم تظهر سيولة كافية بعد بداية البري ماركت
• الزخم الحالي لا يدعم فرضية الأهداف العالية
"""
                send_telegram(cancel_msg)
                sent += 1

        except Exception as e:
            log_error(f"Premarket confirmation failed for {symbol}: {e}")

    state["last_premarket_confirmation_date"] = today
    state["last_premarket_confirmation_at"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    runtime_stats["last_premarket_confirmation"] = state["last_premarket_confirmation_at"]
    log_info(f"Premarket confirmations sent: {sent}")


# ============================================================
# Active Monitoring
# ============================================================

def send_management_alert(symbol, title, body):
    message = f"""{bot_header()}

{title}

السهم: {symbol}

{body}
"""
    return send_telegram(message)


def monitor_active_positions():
    active = redis_get_json(KEY_ACTIVE, default={})
    if not isinstance(active, dict) or not active:
        return

    changed = False
    to_remove = []

    for symbol, pos in list(active.items()):
        try:
            snap = get_snapshot_data(symbol)
            price = safe_float(snap.get("price", 0), 0)

            if price <= 0:
                continue

            current_stop = safe_float(pos.get("current_stop", pos.get("stop", 0)), 0)
            t1 = safe_float(pos.get("t1", 0), 0)
            t2 = safe_float(pos.get("t2", 0), 0)
            t3 = safe_float(pos.get("t3", 0), 0)
            extended = safe_float(pos.get("extended", 0), 0)
            entry = safe_float(pos.get("entry", 0), 0)

            if current_stop > 0 and price <= current_stop:
                alert_key = f"stop_break:{symbol}:{today_ksa_str()}"
                if not already_alerted(alert_key):
                    body = f"""❌ تم كسر وقف الخسارة

السعر الحالي: {format_price(price)}
الوقف: {format_price(current_stop)}

📌 السبب:
• السعر كسر مستوى الحماية
• تم إنهاء المتابعة النشطة لهذا المرشح
"""
                    send_management_alert(symbol, "❌ كسر وقف الخسارة", body)
                    mark_alert_sent(alert_key)

                to_remove.append(symbol)
                changed = True
                continue

            if not pos.get("t1_hit") and t1 > 0 and price >= t1:
                pos["t1_hit"] = True
                new_stop = max(current_stop, entry)
                pos["current_stop"] = round(new_stop, 4)

                body = f"""✅ تم تحقيق الهدف الأول

السعر الحالي: {format_price(price)}
T1: {format_price(t1)}

🔼 رفع الوقف إلى: {format_price(new_stop)}

📌 الأسباب:
• تم تحقيق الهدف الأول
• حماية رأس المال
• الزخم ما زال قائماً طالما السعر أعلى الوقف الجديد
"""
                send_management_alert(symbol, "✅ تحقق الهدف الأول + رفع الوقف", body)
                changed = True

            if not pos.get("t2_hit") and t2 > 0 and price >= t2:
                pos["t2_hit"] = True
                new_stop = max(safe_float(pos.get("current_stop", 0), 0), t1)
                pos["current_stop"] = round(new_stop, 4)

                body = f"""✅ تم تحقيق الهدف الثاني

السعر الحالي: {format_price(price)}
T2: {format_price(t2)}

🔼 رفع الوقف إلى: {format_price(new_stop)}

📌 الأسباب:
• استمرار الزخم
• تحقيق الهدف الثاني
• حماية جزء أكبر من الربح
"""
                send_management_alert(symbol, "✅ تحقق الهدف الثاني + رفع الوقف", body)
                changed = True

            if not pos.get("t3_hit") and t3 > 0 and price >= t3:
                pos["t3_hit"] = True
                new_stop = max(safe_float(pos.get("current_stop", 0), 0), t2)
                pos["current_stop"] = round(new_stop, 4)

                body = f"""✅ تم تحقيق الهدف الثالث

السعر الحالي: {format_price(price)}
T3: {format_price(t3)}

🔼 رفع الوقف إلى: {format_price(new_stop)}

🚀 الهدف الممتد ما زال قائماً إذا استمرت السيولة والزخم.

📌 الأسباب:
• السهم تحول إلى Runner قوي
• السعر تجاوز الهدف الثالث
• حماية الربح مع ترك مجال للامتداد
"""
                send_management_alert(symbol, "✅ تحقق الهدف الثالث + متابعة الهدف الممتد", body)
                changed = True

            if pos.get("extended_active", True) and extended > 0 and price >= extended:
                body = f"""🚀 تم الوصول إلى الهدف الممتد المتوقع

السعر الحالي: {format_price(price)}
الهدف الممتد: {format_price(extended)}

📌 التوصية:
مراقبة لصيقة جداً، ويفضل حماية الربح لأن السهم وصل إلى منطقة امتداد عالية.
"""
                send_management_alert(symbol, "🚀 تحقق الهدف الممتد", body)
                pos["extended_active"] = False
                changed = True

            pos["last_price"] = price
            pos["last_update"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            log_error(f"Active monitoring failed for {symbol}: {e}")

    for symbol in to_remove:
        active.pop(symbol, None)

    if changed:
        redis_set_json(KEY_ACTIVE, active)
        runtime_stats["active_count"] = len(active)


# ============================================================
# Time Logic
# ============================================================

def should_send_daily_candidates_now():
    sa = now_ksa()
    return sa.hour == 0 and sa.minute <= 20


def should_run_float_refresh():
    state = load_state()
    today = today_ksa_str()
    if state.get("last_float_refresh_date") == today:
        return False
    sa = now_ksa()
    return sa.hour >= FLOAT_REFRESH_HOUR_KSA


def should_build_universe():
    state = load_state()
    today = today_ksa_str()
    if state.get("last_universe_build_date") == today:
        return False
    sa = now_ksa()
    return sa.hour >= UNIVERSE_BUILD_HOUR_KSA


# ============================================================
# Startup Catch-up
# ============================================================

def startup_catchup():
    log_info("🚀 Startup catch-up started...")

    collections = load_runtime_collections()

    stored_float = redis_get_json(KEY_FLOAT_CACHE, default={})
    if isinstance(stored_float, dict) and stored_float:
        global float_cache
        float_cache = normalize_float_cache(stored_float)
        runtime_stats["float_count"] = len(float_cache)
        log_info(f"📥 Float restored from Redis: {len(float_cache)}")

    state = load_state()

    if state.get("last_float_refresh_date") != today_ksa_str():
        log_info("Float not refreshed today. Loading now...")
        load_float_cache_from_source()

    if state.get("last_universe_build_date") != today_ksa_str():
        log_info("Universe not built today. Building now...")
        build_daily_universe(force=True)

    collections = load_runtime_collections()

    summary = {
        "float_count": runtime_stats.get("float_count", 0),
        "universe_count": len(collections["universe"]),
        "watchlist_count": len(collections["watchlist"]),
        "strong_count": len(collections["strong"]),
        "active_count": len(collections["active"]),
    }

    send_startup_alert(summary)
    log_info("✅ Startup catch-up completed.")


# ============================================================
# Main Loops
# ============================================================

def scheduler_loop():
    log_info("Scheduler loop started.")

    last_eval_ts = 0
    last_monitor_ts = 0

    while True:
        try:
            now_ts = time.time()

            if should_run_float_refresh():
                load_float_cache_from_source()

            if should_build_universe():
                build_daily_universe(force=True)

            if now_ts - last_eval_ts >= CANDIDATE_REFRESH_SEC:
                evaluate_universe_cycle()
                last_eval_ts = now_ts

            if should_send_daily_candidates_now():
                send_daily_candidates_alert()

            send_premarket_confirmation_if_due()

            if now_ts - last_monitor_ts >= ACTIVE_MONITOR_INTERVAL_SEC:
                monitor_active_positions()
                last_monitor_ts = now_ts

            time.sleep(10)

        except Exception as e:
            log_error(f"Scheduler loop error: {e}")
            log_error(traceback.format_exc())
            time.sleep(30)


# ============================================================
# Main
# ============================================================

def main():
    runtime_stats["started_at"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")

    log_info("==================================================")
    log_info("🚀 بوت الأهداف العالية Starting...")
    log_info("==================================================")

    if not redis_enabled():
        log_warn("Upstash Redis is not configured. Persistence will not work.")

    init_alpaca()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    startup_catchup()

    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=False)
    scheduler_thread.start()


if __name__ == "__main__":
    main()
