# elite_explosion_bot.py
# -*- coding: utf-8 -*-

import os
import json
import time
import math
import requests
import traceback
import sys
import builtins
import functools

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

print = functools.partial(builtins.print, flush=True)

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

from alpaca_trade_api.rest import REST, TimeFrame

from config import *


# =========================================================
# ENV
# =========================================================
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
GIST_ID = os.getenv("GIST_ID")
GIST_TOKEN = os.getenv("GIST_TOKEN")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")


# =========================================================
# CLIENTS
# =========================================================
api = REST(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    api_version="v2"
)


# =========================================================
# GLOBAL STATE
# =========================================================
float_cache = {}
state = {}
active_monitoring = {}
sent_alerts = {}
priority_universe = []
score_history = {}
news_cache = {}
daily_statistics = {}

news_queue = []
news_cursor = 0
scan_cursor = 0

runtime_stats = {
    "started_at": None,
    "last_scan": None,
    "last_universe_build": None,
    "last_float_load": None,
    "last_news_cycle": None,

    "float_count": 0,
    "universe_count": 0,
    "batch_scanned": 0,

    "assets_loaded": 0,
    "passed_basic_filter": 0,
    "rejected_blacklist": 0,
    "rejected_bad_symbol": 0,
    "rejected_bad_name": 0,
    "rejected_price": 0,
    "rejected_missing_float": 0,

    "passed_activity_filter": 0,
    "news_queue_count": 0,
    "news_processed_this_cycle": 0,

    "reached_score_engine": 0,
    "reached_decision_engine": 0,
    "alerts_sent": 0,

    "active_monitoring_count": 0,
    "t1_hits": 0,
    "t2_hits": 0,
    "t3_hits": 0,
    "momentum_continues": 0,
    "exits": 0,

    "last_alert": None,
}


# =========================================================
# TIME HELPERS
# =========================================================
def now_ksa():
    return datetime.now(ZoneInfo(TIMEZONE_KSA))


def now_ny():
    return datetime.now(ZoneInfo(TIMEZONE_NY))


def today_ksa_str():
    return now_ksa().strftime("%Y-%m-%d")


def parse_ksa_time(time_str):
    h, m = map(int, time_str.split(":"))
    n = now_ksa()
    return n.replace(hour=h, minute=m, second=0, microsecond=0)


def is_work_time():
    now = now_ksa()
    start = parse_ksa_time(WORK_START_KSA)
    end = parse_ksa_time(WORK_END_KSA)

    if end <= start:
        return now >= start or now <= end

    return start <= now <= end


def seconds_until_next_work_start():
    now = now_ksa()
    start = parse_ksa_time(WORK_START_KSA)

    if now < start:
        return max(30, int((start - now).total_seconds()))

    tomorrow_start = start + timedelta(days=1)
    return max(30, int((tomorrow_start - now).total_seconds()))


def get_session_profile_name():
    ny = now_ny()
    minutes = ny.hour * 60 + ny.minute

    premarket_start = 4 * 60
    regular_start = 9 * 60 + 30
    power_hour_start = 15 * 60
    regular_end = 16 * 60
    after_end = 20 * 60

    if premarket_start <= minutes < regular_start:
        return "PREMARKET"

    if regular_start <= minutes < power_hour_start:
        return "REGULAR"

    if power_hour_start <= minutes < regular_end:
        return "POWER_HOUR"

    if regular_end <= minutes <= after_end:
        return "AFTER_HOURS"

    return "AFTER_HOURS"


def get_session_profile():
    name = get_session_profile_name()
    return SESSION_PROFILES.get(name, SESSION_PROFILES["REGULAR"])


# =========================================================
# REDIS HELPERS - UPSTASH REST
# =========================================================
def redis_request(command):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        print("⚠️ Redis env missing")
        return None

    try:
        url = UPSTASH_REDIS_REST_URL.rstrip("/")
        headers = {
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
            "Content-Type": "application/json",
        }

        r = requests.post(
            url,
            headers=headers,
            json=command,
            timeout=10,
        )

        if r.status_code != 200:
            print(f"⚠️ Redis error {r.status_code}: {r.text}")
            return None

        return r.json().get("result")

    except Exception as e:
        print(f"⚠️ Redis request failed: {e}")
        return None


def redis_get_json(key, default):
    try:
        result = redis_request(["GET", key])
        if result is None:
            return default
        return json.loads(result)
    except Exception as e:
        print(f"⚠️ Redis load failed for {key}: {e}")
        return default


def redis_set_json(key, value):
    try:
        redis_request(["SET", key, json.dumps(value, ensure_ascii=False)])
    except Exception as e:
        print(f"⚠️ Redis save failed for {key}: {e}")


def load_all_state():
    global state, active_monitoring, sent_alerts, priority_universe
    global score_history, news_cache, daily_statistics

    print("📥 Loading Redis state...")

    state = redis_get_json(REDIS_KEYS["state"], {})
    active_monitoring = redis_get_json(REDIS_KEYS["active_monitoring"], {})
    sent_alerts = redis_get_json(REDIS_KEYS["sent_alerts"], {})
    priority_universe = redis_get_json(REDIS_KEYS["priority_universe"], [])
    score_history = redis_get_json(REDIS_KEYS["score_history"], {})
    news_cache = redis_get_json(REDIS_KEYS["news_cache"], {})
    daily_statistics = redis_get_json(REDIS_KEYS["daily_statistics"], {})

    runtime_stats["active_monitoring_count"] = len(active_monitoring)
    runtime_stats["universe_count"] = len(priority_universe)

    print("✅ Redis restored:")
    print(f"   Active Monitoring: {len(active_monitoring)}")
    print(f"   Sent Alerts: {len(sent_alerts)}")
    print(f"   Priority Universe: {len(priority_universe)}")
    print(f"   News Cache: {len(news_cache)}")


def save_runtime_state():
    redis_set_json(REDIS_KEYS["state"], state)
    redis_set_json(REDIS_KEYS["active_monitoring"], active_monitoring)
    redis_set_json(REDIS_KEYS["sent_alerts"], sent_alerts)
    redis_set_json(REDIS_KEYS["priority_universe"], priority_universe)
    redis_set_json(REDIS_KEYS["score_history"], score_history)
    redis_set_json(REDIS_KEYS["news_cache"], news_cache)
    redis_set_json(REDIS_KEYS["daily_statistics"], daily_statistics)
    redis_set_json(REDIS_KEYS["runtime_stats"], runtime_stats)


# =========================================================
# TELEGRAM
# =========================================================
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram env missing")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        r = requests.post(url, data=payload, timeout=10)

        if r.status_code == 200:
            return True

        print(f"⚠️ Telegram failed: {r.status_code} {r.text}")
        return False

    except Exception as e:
        print(f"⚠️ Telegram exception: {e}")
        return False


def send_startup_message():
    msg = f"""
🚀 <b>{BOT_NAME}</b>

✅ تم تشغيل البوت

🕒 الوقت: {now_ksa().strftime('%Y-%m-%d %H:%M:%S')} KSA
🧭 الجلسة: {get_session_profile_name()}

📦 Float Count: {runtime_stats.get("float_count", 0)}
📊 Universe: {runtime_stats.get("universe_count", 0)}
🎯 Active Monitoring: {len(active_monitoring)}
📰 News Cache: {len(news_cache)}

⚙️ وضع التشغيل: Background Worker
"""
    send_telegram(msg.strip())


# =========================================================
# FLOAT CACHE
# =========================================================
def load_float_cache():
    global float_cache

    if not GIST_ID:
        print("⚠️ GIST_ID is missing")
        float_cache = {}
        runtime_stats["float_count"] = 0
        return

    print("📥 Loading float cache from Gist...")

    try:
        headers = {}

        if GIST_TOKEN:
            headers["Authorization"] = f"token {GIST_TOKEN}"

        url = f"https://api.github.com/gists/{GIST_ID}"

        r = requests.get(
            url,
            headers=headers,
            timeout=30,
        )

        if r.status_code != 200:
            print(f"⚠️ Gist request failed: {r.status_code}")
            float_cache = {}
            runtime_stats["float_count"] = 0
            return

        gist = r.json()

        files = gist.get("files", {})

        if "float_cache.json" not in files:
            print("⚠️ float_cache.json not found in Gist")
            float_cache = {}
            runtime_stats["float_count"] = 0
            return

        content = files["float_cache.json"]["content"]

        float_cache = json.loads(content)

        runtime_stats["float_count"] = len(float_cache)
        runtime_stats["last_float_load"] = now_ksa().isoformat()

        print(f"✅ Float records loaded: {len(float_cache)}")

    except Exception as e:
        print(f"⚠️ Float load exception: {e}")
        float_cache = {}
        runtime_stats["float_count"] = 0


def get_float(symbol):
    try:
        item = float_cache.get(symbol)

        if item is None:
            return None

        if isinstance(item, dict):
            for key in ["float", "floatShares", "shareFloat", "shares_float"]:
                if key in item and item[key] is not None:
                    return float(item[key])

        return float(item)

    except Exception:
        return None


def get_float_bonus(symbol):
    f = get_float(symbol)

    if f is None:
        return MISSING_FLOAT_BONUS

    for limit, bonus in FLOAT_BONUS_TIERS:
        if f <= limit:
            return bonus

    return 0


# =========================================================
# SYMBOL FILTERS
# =========================================================
def is_clean_symbol(symbol):
    if not symbol:
        return False

    if len(symbol) > MAX_SYMBOL_LENGTH:
        return False

    for ch in BAD_SYMBOL_CHARS:
        if ch in symbol:
            return False

    if ALLOW_ONLY_ALPHA_SYMBOLS and not symbol.isalpha():
        return False

    if symbol[-1:] in BAD_SYMBOL_SUFFIXES:
        return False

    return True


def is_blacklisted_symbol(symbol):
    return symbol.upper() in SYMBOL_BLACKLIST


def has_bad_company_name(name):
    if not name:
        return False

    n = name.lower()

    for kw in BAD_NAME_KEYWORDS:
        if kw.lower() in n:
            return True

    return False


def should_reject_by_float(symbol):
    if not MISSING_FLOAT_REJECT:
        return False

    return get_float(symbol) is None


def basic_asset_filter(asset):
    symbol = getattr(asset, "symbol", "").upper()
    name = getattr(asset, "name", "") or ""

    if not symbol:
        return False, "missing_symbol"

    if not getattr(asset, "tradable", False):
        return False, "not_tradable"

    if getattr(asset, "status", "") != "active":
        return False, "not_active"

    if not is_clean_symbol(symbol):
        runtime_stats["rejected_bad_symbol"] += 1
        return False, "bad_symbol"

    if is_blacklisted_symbol(symbol):
        runtime_stats["rejected_blacklist"] += 1
        return False, "blacklist"

    if has_bad_company_name(name):
        runtime_stats["rejected_bad_name"] += 1
        return False, "bad_name"

    if should_reject_by_float(symbol):
        runtime_stats["rejected_missing_float"] += 1
        return False, "missing_float"

    return True, "ok"


# =========================================================
# MARKET DATA HELPERS
# =========================================================
def safe_float(x, default=0.0):
    try:
        if x is None:
            return default
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def get_snapshot_price_data(symbol):
    try:
        snap = api.get_snapshot(symbol)

        latest_trade = getattr(snap, "latest_trade", None)
        latest_quote = getattr(snap, "latest_quote", None)
        daily_bar = getattr(snap, "daily_bar", None)
        prev_daily_bar = getattr(snap, "prev_daily_bar", None)

        price = None

        if latest_trade and getattr(latest_trade, "p", None):
            price = float(latest_trade.p)
        elif daily_bar and getattr(daily_bar, "c", None):
            price = float(daily_bar.c)

        bid = safe_float(getattr(latest_quote, "bp", 0)) if latest_quote else 0
        ask = safe_float(getattr(latest_quote, "ap", 0)) if latest_quote else 0

        spread_pct = 999
        if bid > 0 and ask > 0 and price and price > 0:
            spread_pct = ((ask - bid) / price) * 100

        day_volume = safe_float(getattr(daily_bar, "v", 0)) if daily_bar else 0
        day_open = safe_float(getattr(daily_bar, "o", 0)) if daily_bar else 0
        day_high = safe_float(getattr(daily_bar, "h", 0)) if daily_bar else 0
        prev_close = safe_float(getattr(prev_daily_bar, "c", 0)) if prev_daily_bar else 0

        price_change_pct = 0
        if prev_close > 0 and price:
            price_change_pct = ((price - prev_close) / prev_close) * 100

        dollar_volume = day_volume * price if price else 0

        return {
            "symbol": symbol,
            "price": price,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "day_volume": day_volume,
            "day_open": day_open,
            "day_high": day_high,
            "prev_close": prev_close,
            "price_change_pct": price_change_pct,
            "dollar_volume": dollar_volume,
        }

    except Exception as e:
        print(f"⚠️ Snapshot failed for {symbol}: {e}")
        return None

# =========================================================
# BULK SNAPSHOT HELPERS
# =========================================================
def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def normalize_snapshot_item(symbol, snap):
    try:
        latest_trade = getattr(snap, "latest_trade", None)
        latest_quote = getattr(snap, "latest_quote", None)
        daily_bar = getattr(snap, "daily_bar", None)
        prev_daily_bar = getattr(snap, "prev_daily_bar", None)

        price = None

        if latest_trade and getattr(latest_trade, "p", None):
            price = float(latest_trade.p)
        elif daily_bar and getattr(daily_bar, "c", None):
            price = float(daily_bar.c)

        bid = safe_float(getattr(latest_quote, "bp", 0)) if latest_quote else 0
        ask = safe_float(getattr(latest_quote, "ap", 0)) if latest_quote else 0

        spread_pct = 999
        if bid > 0 and ask > 0 and price and price > 0:
            spread_pct = ((ask - bid) / price) * 100

        day_volume = safe_float(getattr(daily_bar, "v", 0)) if daily_bar else 0
        prev_close = safe_float(getattr(prev_daily_bar, "c", 0)) if prev_daily_bar else 0

        price_change_pct = 0
        if prev_close > 0 and price:
            price_change_pct = ((price - prev_close) / prev_close) * 100

        dollar_volume = day_volume * price if price else 0

        return {
            "symbol": symbol,
            "price": price,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "day_volume": day_volume,
            "prev_close": prev_close,
            "price_change_pct": price_change_pct,
            "dollar_volume": dollar_volume,
        }

    except Exception:
        return None


def get_bulk_snapshots(symbols):
    snapshots = {}

    try:
        raw = api.get_snapshots(symbols)

        if isinstance(raw, dict):
            for symbol, snap in raw.items():
                item = normalize_snapshot_item(symbol, snap)
                if item:
                    snapshots[symbol] = item

        return snapshots

    except Exception as e:
        print(f"⚠️ Bulk snapshots failed for batch size {len(symbols)}: {e}")
        return snapshots
        
# =========================================================
# UNIVERSE BUILDER
# =========================================================
def reset_universe_counters():
    runtime_stats["assets_loaded"] = 0
    runtime_stats["passed_basic_filter"] = 0
    runtime_stats["rejected_blacklist"] = 0
    runtime_stats["rejected_bad_symbol"] = 0
    runtime_stats["rejected_bad_name"] = 0
    runtime_stats["rejected_price"] = 0
    runtime_stats["rejected_missing_float"] = 0

def build_universe():
    global priority_universe

    start_ts = time.time()

    print_cycle_header("📦 Universe Builder")

    reset_universe_counters()

    prefiltered_symbols = []
    final_symbols = []

    price_passed = 0
    dollar_volume_passed = 0
    spread_passed = 0
    snapshot_batches = 0
    snapshot_received = 0

    try:
        assets_start = time.time()
        assets = api.list_assets(status="active")
        runtime_stats["assets_loaded"] = len(assets)

        print(f"📥 Assets Loaded: {len(assets)} | Time: {fmt_sec(assets_start)}")

        basic_start = time.time()

        for asset in assets:
            ok, reason = basic_asset_filter(asset)
            if not ok:
                continue

            symbol = asset.symbol.upper()
            prefiltered_symbols.append(symbol)

        print(f"✅ After Basic Filter: {len(prefiltered_symbols)} | Time: {fmt_sec(basic_start)}")
        print(f"🚫 Rejected Blacklist: {runtime_stats.get('rejected_blacklist', 0)}")
        print(f"🚫 Rejected Bad Symbol: {runtime_stats.get('rejected_bad_symbol', 0)}")
        print(f"🚫 Rejected Bad Name: {runtime_stats.get('rejected_bad_name', 0)}")
        print(f"🚫 Rejected Missing Float: {runtime_stats.get('rejected_missing_float', 0)}")

        bulk_start = time.time()

        batches = list(chunk_list(prefiltered_symbols, BULK_SNAPSHOT_BATCH_SIZE))
        total_batches = len(batches)

        for idx, batch in enumerate(batches, start=1):
            batch_start = time.time()
            snapshots = get_bulk_snapshots(batch)

            snapshot_batches += 1
            snapshot_received += len(snapshots)

            print(
                f"📡 Bulk Snapshot Batch {idx}/{total_batches} | "
                f"Requested: {len(batch)} | "
                f"Received: {len(snapshots)} | "
                f"Time: {fmt_sec(batch_start)}"
            )

            for symbol, snap_data in snapshots.items():
                price = snap_data.get("price")
                dollar_volume = snap_data.get("dollar_volume", 0)
                spread_pct = snap_data.get("spread_pct", 999)

                if not price or price < PRICE_MIN or price > PRICE_MAX:
                    runtime_stats["rejected_price"] += 1
                    continue

                price_passed += 1

                if dollar_volume < MIN_DOLLAR_VOLUME:
                    continue

                dollar_volume_passed += 1

                if spread_pct > MAX_SPREAD_PCT:
                    continue

                spread_passed += 1

                final_symbols.append(symbol)
                runtime_stats["passed_basic_filter"] += 1

            time.sleep(0.2)

        priority_universe = sorted(list(set(final_symbols)))

        runtime_stats["universe_count"] = len(priority_universe)
        runtime_stats["last_universe_build"] = now_ksa().isoformat()

        state["last_universe_build_date"] = today_ksa_str()
        state["last_universe_build_at"] = runtime_stats["last_universe_build"]

        redis_set_json(REDIS_KEYS["priority_universe"], priority_universe)
        redis_set_json(REDIS_KEYS["state"], state)

        print("")
        print("📊 Universe Diagnostic")
        print(f"   Prefiltered Symbols: {len(prefiltered_symbols)}")
        print(f"   Snapshot Batches: {snapshot_batches}")
        print(f"   Snapshot Received: {snapshot_received}")
        print(f"   Price Passed: {price_passed}")
        print(f"   Dollar Volume Passed: {dollar_volume_passed}")
        print(f"   Spread Passed: {spread_passed}")
        print(f"   Universe Final: {len(priority_universe)}")
        print(f"   Bulk Time: {fmt_sec(bulk_start)}")
        print(f"   Total Build Time: {fmt_sec(start_ts)}")
        print("══════════════════════════════════════════")

        return priority_universe

    except Exception as e:
        print(f"🔥 Universe build error: {e}")
        traceback.print_exc()
        print(f"⏱ Failed After: {fmt_sec(start_ts)}")
        return priority_universe
        
def should_rebuild_universe():
    if not priority_universe:
        return True

    last = runtime_stats.get("last_universe_build") or state.get("last_universe_build_at")
    if not last:
        return True

    try:
        last_dt = datetime.fromisoformat(last)
        age_min = (now_ksa() - last_dt).total_seconds() / 60
        return age_min >= FULL_UNIVERSE_REFRESH_MIN
    except Exception:
        return True


# =========================================================
# LOGGING
# =========================================================
def print_universe_summary():
    print("")
    print("📦 Universe Summary")
    print(f"   Assets Loaded: {runtime_stats.get('assets_loaded', 0)}")
    print(f"   Universe Count: {runtime_stats.get('universe_count', 0)}")
    print(f"   Passed Basic: {runtime_stats.get('passed_basic_filter', 0)}")
    print(f"   Rejected Blacklist: {runtime_stats.get('rejected_blacklist', 0)}")
    print(f"   Rejected Bad Symbol: {runtime_stats.get('rejected_bad_symbol', 0)}")
    print(f"   Rejected Bad Name: {runtime_stats.get('rejected_bad_name', 0)}")
    print(f"   Rejected Price: {runtime_stats.get('rejected_price', 0)}")
    print(f"   Rejected Missing Float: {runtime_stats.get('rejected_missing_float', 0)}")
    print("")


def print_scan_summary():
    print("")
    print("📊 Scan Summary")
    print(f"   Time KSA: {now_ksa().strftime('%H:%M:%S')}")
    print(f"   Session: {get_session_profile_name()}")
    print(f"   Universe: {runtime_stats.get('universe_count', 0)}")
    print(f"   Batch Scanned: {runtime_stats.get('batch_scanned', 0)}")
    print(f"   Passed Activity: {runtime_stats.get('passed_activity_filter', 0)}")
    print(f"   Reached Score Engine: {runtime_stats.get('reached_score_engine', 0)}")
    print(f"   Reached Decision Engine: {runtime_stats.get('reached_decision_engine', 0)}")
    print(f"   Alerts Sent: {runtime_stats.get('alerts_sent', 0)}")
    print(f"   Active Monitoring: {len(active_monitoring)}")
    print("")


def print_news_summary():
    print("")
    print("📰 News Queue")
    print(f"   Active News Symbols: {runtime_stats.get('news_queue_count', 0)}")
    print(f"   Processed This Cycle: {runtime_stats.get('news_processed_this_cycle', 0)}")
    print(f"   Cached News: {len(news_cache)}")
    print("")


def print_monitoring_summary():
    print("")
    print("🎯 Monitoring")
    print(f"   Active Trades: {len(active_monitoring)}")
    print(f"   T1 Hit: {runtime_stats.get('t1_hits', 0)}")
    print(f"   T2 Hit: {runtime_stats.get('t2_hits', 0)}")
    print(f"   T3 Hit: {runtime_stats.get('t3_hits', 0)}")
    print(f"   Momentum Continues: {runtime_stats.get('momentum_continues', 0)}")
    print(f"   Exits: {runtime_stats.get('exits', 0)}")
    print("")


# =========================================================
# STARTUP
# =========================================================
def startup():
    runtime_stats["started_at"] = now_ksa().isoformat()

    print("====================================")
    print(f"🚀 Starting {BOT_NAME}")
    print(f"🕒 KSA: {now_ksa().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🧭 Session: {get_session_profile_name()}")
    print("====================================")

    load_all_state()
    load_float_cache()

    if is_work_time():
        if should_rebuild_universe():
            build_universe()

    send_startup_message()

    print("✅ Startup completed")

# =========================================================
# INDICATORS
# =========================================================
def get_bars_df(symbol, timeframe=TimeFrame.Minute, limit=120):
    try:
        bars = api.get_bars(symbol, timeframe, limit=limit).df

        if bars is None or bars.empty:
            return None

        if "symbol" in bars.columns:
            bars = bars[bars["symbol"] == symbol]

        if bars.empty:
            return None

        bars = bars.reset_index()
        return bars

    except Exception as e:
        print(f"⚠️ Bars failed for {symbol}: {e}")
        return None


def calculate_vwap(df):
    try:
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        volume = df["volume"].replace(0, np.nan)
        vwap = (typical_price * volume).cumsum() / volume.cumsum()
        return vwap.fillna(method="ffill").fillna(df["close"])
    except Exception:
        return pd.Series([np.nan] * len(df))


def calculate_obv(df):
    try:
        obv = [0]
        closes = df["close"].values
        volumes = df["volume"].values

        for i in range(1, len(df)):
            if closes[i] > closes[i - 1]:
                obv.append(obv[-1] + volumes[i])
            elif closes[i] < closes[i - 1]:
                obv.append(obv[-1] - volumes[i])
            else:
                obv.append(obv[-1])

        return pd.Series(obv)
    except Exception:
        return pd.Series([0] * len(df))


def calculate_atr(df, period=14):
    try:
        high = df["high"]
        low = df["low"]
        close = df["close"]

        prev_close = close.shift(1)

        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)

        atr = tr.rolling(period).mean()
        return float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0
    except Exception:
        return 0


def calculate_resistance(df, lookback=50):
    try:
        recent = df.tail(lookback)
        body_high = recent[["open", "close"]].max(axis=1)
        return float(body_high.max())
    except Exception:
        return 0


def calculate_volume_acceleration(df):
    try:
        if len(df) < 12:
            return {
                "ratio": 1.0,
                "last_1m_vs_avg": 1.0,
                "last_3m_vs_prev_7m": 1.0,
                "volume_trend_up": False,
                "volume_peak_recent": False,
            }

        vol = df["volume"].astype(float)

        last_1m = vol.iloc[-1]
        avg_10m = vol.iloc[-11:-1].mean()

        recent_3m = vol.iloc[-3:].mean()
        prev_7m = vol.iloc[-10:-3].mean()

        ratio_1m = last_1m / avg_10m if avg_10m > 0 else 1.0
        ratio_3m = recent_3m / prev_7m if prev_7m > 0 else 1.0

        volume_trend_up = (
            vol.iloc[-1] > vol.iloc[-2] and
            vol.iloc[-2] >= vol.iloc[-3]
        )

        peak_idx = vol.tail(5).idxmax()
        volume_peak_recent = peak_idx in vol.tail(2).index

        return {
            "ratio": max(ratio_1m, ratio_3m),
            "last_1m_vs_avg": ratio_1m,
            "last_3m_vs_prev_7m": ratio_3m,
            "volume_trend_up": volume_trend_up,
            "volume_peak_recent": volume_peak_recent,
        }

    except Exception:
        return {
            "ratio": 1.0,
            "last_1m_vs_avg": 1.0,
            "last_3m_vs_prev_7m": 1.0,
            "volume_trend_up": False,
            "volume_peak_recent": False,
        }


def calculate_rvol(df):
    try:
        if len(df) < 30:
            return 1.0

        recent_vol = df["volume"].tail(5).mean()
        avg_vol = df["volume"].iloc[:-5].tail(60).mean()

        if avg_vol <= 0:
            return 1.0

        return float(recent_vol / avg_vol)

    except Exception:
        return 1.0


def reject_one_candle_spike(df):
    try:
        if len(df) < 6:
            return False

        last = df.iloc[-1]
        prev = df.iloc[-2]

        last_range = last["high"] - last["low"]
        if last_range <= 0:
            return False

        close_pos = (last["close"] - last["low"]) / last_range
        last_vol = last["volume"]
        prev_avg_vol = df["volume"].iloc[-6:-1].mean()

        big_spike = last_vol > prev_avg_vol * 4 if prev_avg_vol > 0 else False
        weak_close = close_pos < 0.55
        red_or_flat = last["close"] <= prev["close"]

        return big_spike and (weak_close or red_or_flat)

    except Exception:
        return False


def sustained_breakout_ok(df, resistance):
    try:
        if len(df) < 5 or resistance <= 0:
            return False

        closes = df["close"].tail(3)
        return all(closes > resistance * 0.997)

    except Exception:
        return False


# =========================================================
# NEWS HELPERS
# =========================================================
def get_cached_news(symbol):
    item = news_cache.get(symbol)

    if not item:
        return None

    try:
        expires_at = item.get("expires_at")
        reject_until = item.get("reject_until")

        now = now_ksa()

        if reject_until:
            ru = datetime.fromisoformat(reject_until)
            if now <= ru:
                return item

        if expires_at:
            exp = datetime.fromisoformat(expires_at)
            if now <= exp:
                return item

        return None

    except Exception:
        return None


def classify_news(headlines):
    text = " ".join(headlines).lower()

    for kw in SERIOUS_NEGATIVE_KEYWORDS:
        if kw.lower() in text:
            return {
                "sentiment": "negative",
                "risk_level": "serious",
                "news_score": -30,
                "reason": kw,
            }

    for kw in MEDIUM_NEGATIVE_KEYWORDS:
        if kw.lower() in text:
            return {
                "sentiment": "negative",
                "risk_level": "medium",
                "news_score": -10,
                "reason": kw,
            }

    for kw in POSITIVE_CATALYST_KEYWORDS:
        if kw.lower() in text:
            return {
                "sentiment": "positive",
                "risk_level": "none",
                "news_score": 10,
                "reason": kw,
            }

    return {
        "sentiment": "neutral",
        "risk_level": "none",
        "news_score": 0,
        "reason": None,
    }


def fetch_symbol_news(symbol):
    if not FINNHUB_API_KEY:
        return {
            "sentiment": "neutral",
            "risk_level": "none",
            "headline": "",
            "source": "none",
            "news_score": 0,
            "reject_until": None,
        }

    cached = get_cached_news(symbol)

    if cached:
        runtime_stats["news_cache_hits"] = runtime_stats.get("news_cache_hits", 0) + 1
        return cached
        
    try:
        to_date = now_ksa().date()
        from_date = to_date - timedelta(hours=NEWS_LOOKBACK_HOURS)

        url = "https://finnhub.io/api/v1/company-news"
        params = {
            "symbol": symbol,
            "from": from_date.strftime("%Y-%m-%d"),
            "to": to_date.strftime("%Y-%m-%d"),
            "token": FINNHUB_API_KEY,
        }

        runtime_stats["news_api_requests"] = runtime_stats.get("news_api_requests", 0) + 1

        r = requests.get(
            url,
            params=params,
            timeout=10,
        )
        
        if r.status_code != 200:
            print(f"⚠️ Finnhub news failed {symbol}: {r.status_code}")
            return {
                "sentiment": "neutral",
                "risk_level": "none",
                "headline": "",
                "source": "finnhub_error",
                "news_score": 0,
                "reject_until": None,
            }

        data = r.json()
        headlines = []

        if isinstance(data, list):
            for item in data[:10]:
                headline = item.get("headline", "")
                if headline:
                    headlines.append(headline)

        classification = classify_news(headlines)

        now = now_ksa()
        expires_at = now + timedelta(seconds=NEWS_CACHE_TTL)
        reject_until = None

        if classification["risk_level"] == "serious":
            reject_until = now + timedelta(hours=SERIOUS_NEGATIVE_REJECT_HOURS)
        elif classification["sentiment"] == "positive":
            expires_at = now + timedelta(hours=POSITIVE_NEWS_BONUS_HOURS)

        result = {
            "fetched_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "sentiment": classification["sentiment"],
            "risk_level": classification["risk_level"],
            "headline": headlines[0] if headlines else "",
            "source": "Finnhub",
            "news_score": classification["news_score"],
            "reason": classification["reason"],
            "reject_until": reject_until.isoformat() if reject_until else None,
        }

        news_cache[symbol] = result
        return result

    except Exception as e:
        print(f"⚠️ News fetch exception {symbol}: {e}")
        return {
            "sentiment": "neutral",
            "risk_level": "none",
            "headline": "",
            "source": "exception",
            "news_score": 0,
            "reject_until": None,
        }


# =========================================================
# ACTIVITY FILTER
# =========================================================
def calculate_activity_score(metrics):
    score = 0

    rvol = metrics.get("rvol", 1)
    price_change = metrics.get("price_change_pct", 0)
    dollar_volume = metrics.get("dollar_volume", 0)
    volume_accel = metrics.get("volume_acceleration", {}).get("ratio", 1)
    near_high_pct = metrics.get("near_high_pct", 999)
    spread_pct = metrics.get("spread_pct", 999)

    if rvol >= 4:
        score += 25
    elif rvol >= 3:
        score += 20
    elif rvol >= 2:
        score += 15

    if price_change >= 10:
        score += 20
    elif price_change >= 5:
        score += 15
    elif price_change >= 3:
        score += 10

    if dollar_volume >= 2_000_000:
        score += 20
    elif dollar_volume >= 1_000_000:
        score += 15
    elif dollar_volume >= ACTIVITY_FILTER["min_dollar_volume"]:
        score += 10

    if volume_accel >= 2.5:
        score += 20
    elif volume_accel >= 2:
        score += 15
    elif volume_accel >= 1.5:
        score += 10

    if near_high_pct <= 1:
        score += 10
    elif near_high_pct <= 2:
        score += 7
    elif near_high_pct <= 3:
        score += 5

    if spread_pct <= 1:
        score += 5
    elif spread_pct <= 2:
        score += 3

    return min(score, 100)


def passes_activity_filter(metrics):
    if metrics.get("rvol", 1) < ACTIVITY_FILTER["min_rvol"]:
        return False

    if metrics.get("price_change_pct", 0) < ACTIVITY_FILTER["min_price_change_pct"]:
        return False

    if metrics.get("dollar_volume", 0) < ACTIVITY_FILTER["min_dollar_volume"]:
        return False

    activity_score = calculate_activity_score(metrics)
    metrics["activity_score"] = activity_score

    return activity_score >= ACTIVITY_FILTER["min_activity_score"]


def rebuild_news_queue(active_candidates):
    global news_queue

    ranked = sorted(
        active_candidates,
        key=lambda x: x.get("activity_score", 0),
        reverse=True
    )

    capped = ranked[:NEWS_ACTIVE_QUEUE_CAP]
    symbols = [x["symbol"] for x in capped]

    for symbol in active_monitoring.keys():
        if symbol not in symbols:
            symbols.insert(0, symbol)

    news_queue = list(dict.fromkeys(symbols))
    runtime_stats["news_queue_count"] = len(news_queue)


def process_news_queue():
    global news_cursor

    if not news_queue:
        runtime_stats["news_processed_this_cycle"] = 0
        return

    max_per_cycle = NEWS_REQUESTS_PER_MINUTE
    processed = 0

    for _ in range(max_per_cycle):
        if not news_queue:
            break

        if news_cursor >= len(news_queue):
            news_cursor = 0

        symbol = news_queue[news_cursor]
        news_cursor += 1

        fetch_symbol_news(symbol)
        processed += 1

    runtime_stats["news_processed_this_cycle"] = processed
    runtime_stats["last_news_cycle"] = now_ksa().isoformat()

    redis_set_json(REDIS_KEYS["news_cache"], news_cache)


# =========================================================
# SCORE ENGINE
# =========================================================
def score_rvol(rvol):
    if rvol >= 6:
        return SCORE_WEIGHTS["rvol"]
    if rvol >= 4:
        return SCORE_WEIGHTS["rvol"] * 0.85
    if rvol >= 3:
        return SCORE_WEIGHTS["rvol"] * 0.65
    if rvol >= 2:
        return SCORE_WEIGHTS["rvol"] * 0.45
    return 0


def score_volume_acceleration(volume_accel):
    ratio = volume_accel.get("ratio", 1)
    trend_up = volume_accel.get("volume_trend_up", False)
    peak_recent = volume_accel.get("volume_peak_recent", False)

    score = 0

    if ratio >= 3:
        score += SCORE_WEIGHTS["volume_acceleration"] * 0.75
    elif ratio >= 2:
        score += SCORE_WEIGHTS["volume_acceleration"] * 0.55
    elif ratio >= 1.5:
        score += SCORE_WEIGHTS["volume_acceleration"] * 0.35

    if trend_up:
        score += SCORE_WEIGHTS["volume_acceleration"] * 0.15

    if peak_recent:
        score += SCORE_WEIGHTS["volume_acceleration"] * 0.10

    return min(score, SCORE_WEIGHTS["volume_acceleration"])


def score_price_change(price_change):
    if price_change >= 20:
        return SCORE_WEIGHTS["price_change"]
    if price_change >= 12:
        return SCORE_WEIGHTS["price_change"] * 0.85
    if price_change >= 8:
        return SCORE_WEIGHTS["price_change"] * 0.65
    if price_change >= 5:
        return SCORE_WEIGHTS["price_change"] * 0.45
    return 0


def score_breakout(metrics):
    resistance = metrics.get("resistance", 0)
    price = metrics.get("price", 0)
    near_high_pct = metrics.get("near_high_pct", 999)

    score = 0

    if resistance > 0 and price > resistance:
        score += SCORE_WEIGHTS["breakout"] * 0.70
    elif resistance > 0 and price >= resistance * 0.985:
        score += SCORE_WEIGHTS["breakout"] * 0.45

    if near_high_pct <= 1:
        score += SCORE_WEIGHTS["breakout"] * 0.30
    elif near_high_pct <= 2:
        score += SCORE_WEIGHTS["breakout"] * 0.20

    return min(score, SCORE_WEIGHTS["breakout"])


def score_obv(metrics):
    if metrics.get("obv_positive") and metrics.get("obv_rising"):
        return SCORE_WEIGHTS["obv"]
    if metrics.get("obv_positive"):
        return SCORE_WEIGHTS["obv"] * 0.65
    return 0


def score_liquidity(metrics):
    dollar_volume = metrics.get("dollar_volume", 0)
    spread_pct = metrics.get("spread_pct", 999)

    score = 0

    if dollar_volume >= 2_000_000:
        score += SCORE_WEIGHTS["liquidity"] * 0.65
    elif dollar_volume >= 1_000_000:
        score += SCORE_WEIGHTS["liquidity"] * 0.45
    elif dollar_volume >= 500_000:
        score += SCORE_WEIGHTS["liquidity"] * 0.30

    if spread_pct <= 1:
        score += SCORE_WEIGHTS["liquidity"] * 0.35
    elif spread_pct <= 2:
        score += SCORE_WEIGHTS["liquidity"] * 0.20

    return min(score, SCORE_WEIGHTS["liquidity"])


def calculate_final_score(symbol, metrics):
    runtime_stats["reached_score_engine"] += 1

    rvol_score = score_rvol(metrics.get("rvol", 1))
    accel_score = score_volume_acceleration(metrics.get("volume_acceleration", {}))
    price_score = score_price_change(metrics.get("price_change_pct", 0))
    breakout_score = score_breakout(metrics)
    obv_score = score_obv(metrics)
    liquidity_score = score_liquidity(metrics)

    float_bonus_raw = get_float_bonus(symbol)
    float_score = min(float_bonus_raw, SCORE_WEIGHTS["float_quality"])

    news = get_cached_news(symbol) or {}
    news_score = news.get("news_score", 0)

    raw_score = (
        rvol_score +
        accel_score +
        price_score +
        breakout_score +
        obv_score +
        float_score +
        liquidity_score +
        news_score
    )

    final_score = max(0, min(100, raw_score))

    breakdown = {
        "rvol_score": round(rvol_score, 2),
        "accel_score": round(accel_score, 2),
        "price_score": round(price_score, 2),
        "breakout_score": round(breakout_score, 2),
        "obv_score": round(obv_score, 2),
        "float_score": round(float_score, 2),
        "liquidity_score": round(liquidity_score, 2),
        "news_score": round(news_score, 2),
        "final_score": round(final_score, 2),
    }

    return final_score, breakdown


# =========================================================
# METRICS BUILDER
# =========================================================
def build_symbol_metrics(symbol):
    try:
        snap = get_snapshot_price_data(symbol)
        if not snap:
            return None

        price = snap.get("price")
        if not price or price < PRICE_MIN or price > PRICE_MAX:
            return None

        profile = get_session_profile()

        if snap.get("spread_pct", 999) > profile.get("max_spread_pct", MAX_SPREAD_PCT):
            return None

        if snap.get("dollar_volume", 0) < profile.get("min_dollar_volume", MIN_DOLLAR_VOLUME):
            return None

        df = get_bars_df(symbol, TimeFrame.Minute, limit=120)
        if df is None or len(df) < 30:
            return None

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["open", "high", "low", "close", "volume"])

        if len(df) < 30:
            return None

        df["vwap"] = calculate_vwap(df)
        df["ema9"] = df["close"].ewm(span=9).mean()
        df["ema20"] = df["close"].ewm(span=20).mean()
        df["obv"] = calculate_obv(df)
        df["obv_ema10"] = df["obv"].ewm(span=10).mean()

        last = df.iloc[-1]

        vwap = safe_float(last["vwap"])
        ema9 = safe_float(last["ema9"])
        ema20 = safe_float(last["ema20"])

        if profile.get("require_above_vwap", True) and price < vwap:
            return None

        if ema9 < ema20 * 0.995:
            return None

        if profile.get("reject_one_candle_spike", True) and reject_one_candle_spike(df):
            return None

        resistance = calculate_resistance(df, lookback=50)
        volume_accel = calculate_volume_acceleration(df)
        rvol = calculate_rvol(df)
        atr = calculate_atr(df, ATR_PERIOD)

        day_high = snap.get("day_high", 0)
        near_high_pct = 999
        if day_high and day_high > 0 and price:
            near_high_pct = ((day_high - price) / day_high) * 100

        obv_positive = last["obv"] > last["obv_ema10"]
        obv_rising = df["obv"].iloc[-1] > df["obv"].iloc[-3] if len(df) >= 3 else False

        if profile.get("require_sustained_breakout", False):
            if resistance > 0 and price > resistance:
                if not sustained_breakout_ok(df, resistance):
                    return None

        metrics = {
            "symbol": symbol,
            "price": price,
            "bid": snap.get("bid", 0),
            "ask": snap.get("ask", 0),
            "spread_pct": snap.get("spread_pct", 999),
            "day_volume": snap.get("day_volume", 0),
            "dollar_volume": snap.get("dollar_volume", 0),
            "price_change_pct": snap.get("price_change_pct", 0),
            "day_high": day_high,
            "near_high_pct": near_high_pct,

            "rvol": rvol,
            "volume_acceleration": volume_accel,
            "vwap": vwap,
            "ema9": ema9,
            "ema20": ema20,
            "resistance": resistance,
            "obv_positive": bool(obv_positive),
            "obv_rising": bool(obv_rising),
            "atr": atr,
            "float": get_float(symbol),
            "float_bonus": get_float_bonus(symbol),
        }

        return metrics

    except Exception as e:
        print(f"⚠️ Metrics failed for {symbol}: {e}")
        return None


# =========================================================
# SCANNER
# =========================================================
def get_next_scan_batch():
    global scan_cursor

    if not priority_universe:
        return []

    batch_size = 200
    batch = []

    for _ in range(min(batch_size, len(priority_universe))):
        if scan_cursor >= len(priority_universe):
            scan_cursor = 0

        batch.append(priority_universe[scan_cursor])
        scan_cursor += 1

    return batch


def scan_market_batch():
    runtime_stats["batch_scanned"] = 0
    runtime_stats["passed_activity_filter"] = 0
    runtime_stats["reached_score_engine"] = 0
    runtime_stats["reached_decision_engine"] = 0

    active_candidates = []
    scored_candidates = []

    batch = get_next_scan_batch()
    runtime_stats["batch_scanned"] = len(batch)

    if not batch:
        return [], []

    for symbol in batch:
        try:
            metrics = build_symbol_metrics(symbol)
            if not metrics:
                continue

            if passes_activity_filter(metrics):
                runtime_stats["passed_activity_filter"] += 1
                active_candidates.append(metrics)

            score, breakdown = calculate_final_score(symbol, metrics)
            metrics["final_score"] = score
            metrics["score_breakdown"] = breakdown

            scored_candidates.append(metrics)

        except Exception as e:
            print(f"⚠️ Scan error {symbol}: {e}")

    rebuild_news_queue(active_candidates)

    return scored_candidates, active_candidates

# =========================================================
# DECISION ENGINE
# =========================================================
def was_alert_sent_recently(symbol):
    item = sent_alerts.get(symbol)
    if not item:
        return False

    try:
        last_sent = datetime.fromisoformat(item.get("sent_at"))
        age_hours = (now_ksa() - last_sent).total_seconds() / 3600
        return age_hours < REPEAT_BLOCK_HOURS
    except Exception:
        return False


def has_serious_negative_news(symbol):
    news = get_cached_news(symbol)
    if not news:
        return False, None

    if news.get("risk_level") == "serious":
        return True, news.get("reason") or news.get("headline")

    return False, None


def passes_decision_engine(metrics):
    runtime_stats["reached_decision_engine"] += 1

    symbol = metrics["symbol"]
    profile = get_session_profile()

    if was_alert_sent_recently(symbol):
        return False, "repeat_block"

    if symbol in active_monitoring:
        return False, "already_monitoring"

    serious_news, reason = has_serious_negative_news(symbol)
    if serious_news:
        return False, f"serious_negative_news:{reason}"

    if metrics.get("final_score", 0) < profile.get("min_score", ENTRY_MIN_SCORE):
        return False, "low_score"

    if metrics.get("rvol", 1) < profile.get("min_rvol", 3.0):
        return False, "low_rvol"

    if metrics.get("price_change_pct", 0) < profile.get("min_price_change_pct", 5.0):
        return False, "low_price_change"

    if metrics.get("volume_acceleration", {}).get("ratio", 1) < profile.get("min_volume_acceleration", 1.6):
        return False, "low_volume_acceleration"

    if metrics.get("spread_pct", 999) > profile.get("max_spread_pct", MAX_SPREAD_PCT):
        return False, "wide_spread"

    if metrics.get("dollar_volume", 0) < profile.get("min_dollar_volume", MIN_DOLLAR_VOLUME):
        return False, "low_dollar_volume"

    if profile.get("require_above_vwap", True):
        if metrics.get("price", 0) < metrics.get("vwap", 0):
            return False, "below_vwap"

    if not metrics.get("obv_positive", False):
        return False, "obv_not_positive"

    return True, "ok"


def select_best_entry_candidate(scored_candidates):
    valid = []

    for metrics in scored_candidates:
        ok, reason = passes_decision_engine(metrics)
        metrics["decision_reason"] = reason

        if ok:
            valid.append(metrics)

    if not valid:
        return None

    valid = sorted(
        valid,
        key=lambda x: (
            x.get("final_score", 0),
            x.get("activity_score", 0),
            x.get("rvol", 0),
            x.get("volume_acceleration", {}).get("ratio", 0),
        ),
        reverse=True,
    )

    return valid[0]


# =========================================================
# TARGETS / RISK
# =========================================================
def calculate_trade_plan(metrics):
    price = metrics.get("price", 0)
    atr = metrics.get("atr", 0)

    if not price or price <= 0:
        return None

    if not atr or atr <= 0:
        atr = price * 0.03

    stop = price - (atr * STOP_ATR_MULTIPLIER)
    t1 = price + (atr * TARGETS["T1_ATR"])
    t2 = price + (atr * TARGETS["T2_ATR"])
    t3 = price + (atr * TARGETS["T3_ATR"])

    max_stop = price * 0.94
    if stop < max_stop:
        stop = max_stop

    return {
        "entry": round(price, 4),
        "stop": round(stop, 4),
        "t1": round(t1, 4),
        "t2": round(t2, 4),
        "t3": round(t3, 4),
        "atr": round(atr, 4),
    }


# =========================================================
# TELEGRAM ALERTS
# =========================================================
def format_float_value(value):
    try:
        if value is None:
            return "غير متوفر"
        value = float(value)
        if value >= 1_000_000_000:
            return f"{value / 1_000_000_000:.2f}B"
        if value >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        return f"{value:,.0f}"
    except Exception:
        return "غير متوفر"


def send_entry_alert(metrics, plan):
    symbol = metrics["symbol"]
    breakdown = metrics.get("score_breakdown", {})
    volume_accel = metrics.get("volume_acceleration", {})
    news = get_cached_news(symbol) or {}

    msg = f"""
{TELEGRAM_ENTRY_TITLE}

📌 <b>{symbol}</b>
💵 السعر: <b>{plan["entry"]}</b>
⭐ السكور: <b>{metrics.get("final_score", 0):.1f}/100</b>
🧭 الجلسة: {get_session_profile_name()}

📊 <b>سبب الدخول</b>
• RVOL: {metrics.get("rvol", 1):.2f}
• تسارع الفوليوم: {volume_accel.get("ratio", 1):.2f}x
• تغير السعر: {metrics.get("price_change_pct", 0):.2f}%
• Dollar Volume: ${metrics.get("dollar_volume", 0):,.0f}
• Spread: {metrics.get("spread_pct", 0):.2f}%
• OBV: {"إيجابي" if metrics.get("obv_positive") else "محايد"}
• Float: {format_float_value(metrics.get("float"))}

🎯 <b>الخطة</b>
الدخول: {plan["entry"]}
وقف الخسارة: {plan["stop"]}
T1: {plan["t1"]}
T2: {plan["t2"]}
T3: {plan["t3"]}

📰 الأخبار: {news.get("sentiment", "neutral")}
{("• " + news.get("headline", "")) if news.get("headline") else ""}

🧮 <b>تفصيل السكور</b>
RVOL: {breakdown.get("rvol_score", 0)}
Accel: {breakdown.get("accel_score", 0)}
Price: {breakdown.get("price_score", 0)}
Breakout: {breakdown.get("breakout_score", 0)}
OBV: {breakdown.get("obv_score", 0)}
Float: {breakdown.get("float_score", 0)}
Liquidity: {breakdown.get("liquidity_score", 0)}
News: {breakdown.get("news_score", 0)}
"""
    ok = send_telegram(msg.strip())

    if ok:
        runtime_stats["alerts_sent"] += 1
        runtime_stats["last_alert"] = {
            "symbol": symbol,
            "sent_at": now_ksa().isoformat(),
            "price": plan["entry"],
            "score": metrics.get("final_score", 0),
        }

    return ok


def send_target_alert(symbol, title, trade, price, target_name):
    msg = f"""
{title}

📌 <b>{symbol}</b>
💵 السعر الحالي: <b>{price:.4f}</b>
🎯 الهدف: {target_name}

الدخول: {trade.get("entry")}
الوقف الحالي: {trade.get("stop")}
أعلى سعر بعد الدخول: {trade.get("highest_price", trade.get("entry"))}
"""
    send_telegram(msg.strip())


def send_momentum_alert(symbol, trade, price):
    msg = f"""
{TELEGRAM_MOMENTUM_TITLE}

📌 <b>{symbol}</b>
💵 السعر الحالي: <b>{price:.4f}</b>

السهم تجاوز الأهداف والزخم ما زال قوي.
نواصل المراقبة مع Trailing Stop.

الدخول: {trade.get("entry")}
الوقف الحالي: {trade.get("stop")}
أعلى سعر: {trade.get("highest_price")}
"""
    send_telegram(msg.strip())


def send_exit_alert(symbol, trade, price, reason):
    msg = f"""
{TELEGRAM_EXIT_TITLE}

📌 <b>{symbol}</b>
💵 السعر الحالي: <b>{price:.4f}</b>
📍 سبب الخروج: {reason}

الدخول: {trade.get("entry")}
الوقف: {trade.get("stop")}
أعلى سعر بعد الدخول: {trade.get("highest_price", trade.get("entry"))}
"""
    send_telegram(msg.strip())


# =========================================================
# ENTER TRADE / ACTIVE MONITORING
# =========================================================
def register_entry(metrics, plan):
    symbol = metrics["symbol"]

    active_monitoring[symbol] = {
        "symbol": symbol,
        "entry": plan["entry"],
        "stop": plan["stop"],
        "t1": plan["t1"],
        "t2": plan["t2"],
        "t3": plan["t3"],
        "atr": plan["atr"],
        "entered_at": now_ksa().isoformat(),
        "highest_price": plan["entry"],
        "t1_hit": False,
        "t2_hit": False,
        "t3_hit": False,
        "momentum_alert_sent": False,
        "last_check": None,
        "entry_score": metrics.get("final_score", 0),
        "session": get_session_profile_name(),
    }

    sent_alerts[symbol] = {
        "sent_at": now_ksa().isoformat(),
        "price": plan["entry"],
        "score": metrics.get("final_score", 0),
    }

    runtime_stats["active_monitoring_count"] = len(active_monitoring)

    redis_set_json(REDIS_KEYS["active_monitoring"], active_monitoring)
    redis_set_json(REDIS_KEYS["sent_alerts"], sent_alerts)


def execute_entry_if_any(scored_candidates):
    candidate = select_best_entry_candidate(scored_candidates)

    if not candidate:
        return False

    plan = calculate_trade_plan(candidate)
    if not plan:
        return False

    ok = send_entry_alert(candidate, plan)

    # الإرسال أولًا، ثم الحفظ والتسجيل
    if ok:
        register_entry(candidate, plan)

    return ok


# =========================================================
# MONITORING HELPERS
# =========================================================
def get_current_price(symbol):
    snap = get_snapshot_price_data(symbol)
    if not snap:
        return None
    return snap.get("price")


def update_trailing_stop(trade, price):
    try:
        if price > trade.get("highest_price", trade["entry"]):
            trade["highest_price"] = price

        highest = trade.get("highest_price", trade["entry"])
        trailing_stop = highest * (1 - TRAILING_STOP_PCT / 100)

        if trailing_stop > trade.get("stop", 0):
            trade["stop"] = round(trailing_stop, 4)

    except Exception:
        pass


def check_monitoring_weakness(symbol, trade, price):
    try:
        df = get_bars_df(symbol, TimeFrame.Minute, limit=60)
        if df is None or len(df) < 20:
            return None

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        if len(df) < 20:
            return None

        df["vwap"] = calculate_vwap(df)
        df["obv"] = calculate_obv(df)
        df["obv_ema10"] = df["obv"].ewm(span=10).mean()

        last = df.iloc[-1]

        if EXIT_ON_LOSE_VWAP:
            if price < safe_float(last["vwap"]):
                return "فقد VWAP"

        if EXIT_ON_OBV_WEAKNESS:
            if safe_float(last["obv"]) < safe_float(last["obv_ema10"]):
                return "ضعف OBV"

        if EXIT_ON_VOLUME_COLLAPSE:
            vol_recent = df["volume"].tail(3).mean()
            vol_prev = df["volume"].iloc[-13:-3].mean()
            if vol_prev > 0 and vol_recent < vol_prev * 0.45:
                return "انهيار الفوليوم"

        return None

    except Exception:
        return None


def monitor_trade(symbol, trade):
    price = get_current_price(symbol)
    if not price:
        return

    trade["last_check"] = now_ksa().isoformat()
    update_trailing_stop(trade, price)

    if price <= trade.get("stop", 0):
        send_exit_alert(symbol, trade, price, "ضرب وقف الخسارة")
        active_monitoring.pop(symbol, None)
        runtime_stats["exits"] += 1
        return

    if not trade.get("t1_hit") and price >= trade.get("t1", 999999):
        trade["t1_hit"] = True
        runtime_stats["t1_hits"] += 1
        send_target_alert(symbol, TELEGRAM_T1_TITLE, trade, price, "T1")

    if not trade.get("t2_hit") and price >= trade.get("t2", 999999):
        trade["t2_hit"] = True
        runtime_stats["t2_hits"] += 1
        send_target_alert(symbol, TELEGRAM_T2_TITLE, trade, price, "T2")

    if not trade.get("t3_hit") and price >= trade.get("t3", 999999):
        trade["t3_hit"] = True
        runtime_stats["t3_hits"] += 1
        send_target_alert(symbol, TELEGRAM_T3_TITLE, trade, price, "T3")

    if trade.get("t3_hit") and not trade.get("momentum_alert_sent"):
        weakness = check_monitoring_weakness(symbol, trade, price)
        if weakness is None:
            trade["momentum_alert_sent"] = True
            runtime_stats["momentum_continues"] += 1
            send_momentum_alert(symbol, trade, price)

    weakness = check_monitoring_weakness(symbol, trade, price)
    if weakness:
        send_exit_alert(symbol, trade, price, weakness)
        active_monitoring.pop(symbol, None)
        runtime_stats["exits"] += 1
        return

    try:
        entered_at = datetime.fromisoformat(trade.get("entered_at"))
        age_min = (now_ksa() - entered_at).total_seconds() / 60

        if age_min >= MAX_MONITOR_MINUTES and not trade.get("t1_hit"):
            send_exit_alert(symbol, trade, price, "انتهاء وقت المراقبة بدون تحقيق T1")
            active_monitoring.pop(symbol, None)
            runtime_stats["exits"] += 1
            return

    except Exception:
        pass


def monitor_active_trades():
    if not active_monitoring:
        return

    symbols = list(active_monitoring.keys())

    for symbol in symbols:
        try:
            trade = active_monitoring.get(symbol)
            if not trade:
                continue
            monitor_trade(symbol, trade)
        except Exception as e:
            print(f"⚠️ Monitor error {symbol}: {e}")

    runtime_stats["active_monitoring_count"] = len(active_monitoring)
    redis_set_json(REDIS_KEYS["active_monitoring"], active_monitoring)


# =========================================================
# DAILY / STARTUP RECOVERY
# =========================================================
def recover_active_monitoring_after_restart():
    if not active_monitoring:
        return

    print("🔄 Recovering active monitoring after restart...")

    symbols = list(active_monitoring.keys())

    for symbol in symbols:
        try:
            trade = active_monitoring.get(symbol)
            if not trade:
                continue

            price = get_current_price(symbol)
            if not price:
                continue

            if price <= trade.get("stop", 0):
                send_exit_alert(symbol, trade, price, "ضرب وقف الخسارة أثناء توقف البوت")
                active_monitoring.pop(symbol, None)
                continue

            if not trade.get("t1_hit") and price >= trade.get("t1", 999999):
                trade["t1_hit"] = True
                send_target_alert(symbol, TELEGRAM_T1_TITLE, trade, price, "T1 أثناء التوقف")

            if not trade.get("t2_hit") and price >= trade.get("t2", 999999):
                trade["t2_hit"] = True
                send_target_alert(symbol, TELEGRAM_T2_TITLE, trade, price, "T2 أثناء التوقف")

            if not trade.get("t3_hit") and price >= trade.get("t3", 999999):
                trade["t3_hit"] = True
                send_target_alert(symbol, TELEGRAM_T3_TITLE, trade, price, "T3 أثناء التوقف")

        except Exception as e:
            print(f"⚠️ Recovery error {symbol}: {e}")

    redis_set_json(REDIS_KEYS["active_monitoring"], active_monitoring)

# =========================================================
# DIAGNOSTIC LOGGING
# =========================================================
def fmt_sec(start_ts):
    try:
        return f"{time.time() - start_ts:.2f}s"
    except Exception:
        return "0.00s"


def print_cycle_header(title):
    print("")
    print("══════════════════════════════════════════")
    print(f"{title}")
    print(f"🕒 KSA: {now_ksa().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🧭 Session: {get_session_profile_name()}")
    print("══════════════════════════════════════════")


def print_top_scores(scored_candidates, limit=5):
    if not scored_candidates:
        print("⭐ Top Scores: لا يوجد")
        return

    ranked = sorted(
        scored_candidates,
        key=lambda x: x.get("final_score", 0),
        reverse=True,
    )[:limit]

    print("⭐ Top Scores:")
    for item in ranked:
        print(
            f"   {item.get('symbol')} | "
            f"Score: {item.get('final_score', 0):.1f} | "
            f"RVOL: {item.get('rvol', 0):.2f} | "
            f"Accel: {item.get('volume_acceleration', {}).get('ratio', 0):.2f}x | "
            f"Change: {item.get('price_change_pct', 0):.2f}%"
        )
        
# =========================================================
# MAIN CYCLE
# =========================================================
def run_scan_cycle():
    start_ts = time.time()

    print_cycle_header("🔍 Scan Cycle")

    if should_rebuild_universe():
        build_universe()

    scan_start = time.time()
    scored_candidates, active_candidates = scan_market_batch()
    scan_time = fmt_sec(scan_start)

    alert_start = time.time()
    alert_sent = execute_entry_if_any(scored_candidates)
    alert_time = fmt_sec(alert_start)

    runtime_stats["last_scan"] = now_ksa().isoformat()

    print("")
    print("📊 Scan Diagnostic")
    print(f"   Universe: {runtime_stats.get('universe_count', 0)}")
    print(f"   Batch Scanned: {runtime_stats.get('batch_scanned', 0)}")
    print(f"   Passed Activity: {runtime_stats.get('passed_activity_filter', 0)}")
    print(f"   Active Candidates: {len(active_candidates)}")
    print(f"   Scored Candidates: {len(scored_candidates)}")
    print(f"   Reached Score Engine: {runtime_stats.get('reached_score_engine', 0)}")
    print(f"   Reached Decision Engine: {runtime_stats.get('reached_decision_engine', 0)}")
    print(f"   Alert Sent This Cycle: {alert_sent}")
    print(f"   Total Alerts Sent: {runtime_stats.get('alerts_sent', 0)}")
    print(f"   News Queue Count: {runtime_stats.get('news_queue_count', 0)}")
    print(f"   Active Monitoring: {len(active_monitoring)}")
    print(f"   Scan Time: {scan_time}")
    print(f"   Decision/Alert Time: {alert_time}")

    print_top_scores(scored_candidates)

    print(f"⏱ Total Scan Cycle Time: {fmt_sec(start_ts)}")
    print("══════════════════════════════════════════")

def run_news_cycle():
    start_ts = time.time()

    print_cycle_header("📰 News Cycle")

    before_cache = len(news_cache)
    before_cursor = news_cursor
    before_queue = len(news_queue)

    runtime_stats["news_api_requests"] = 0
    runtime_stats["news_cache_hits"] = 0

    process_news_queue()

    after_queue = len(news_queue)

    positive = 0
    negative = 0
    serious = 0
    neutral = 0

    for item in news_cache.values():
        sentiment = item.get("sentiment")
        risk = item.get("risk_level")

        if risk == "serious":
            serious += 1
        elif sentiment == "positive":
            positive += 1
        elif sentiment == "negative":
            negative += 1
        else:
            neutral += 1

    print("")
    print("📰 News Diagnostic")
    print(f"   Queue Before: {before_queue}")
    print(f"   Queue After: {after_queue}")
    print(f"   Cursor Before: {before_cursor}")
    print(f"   Cursor After: {news_cursor}")
    print(f"   Processed This Cycle: {runtime_stats.get('news_processed_this_cycle', 0)}")
    print(f"   API Requests: {runtime_stats.get('news_api_requests', 0)}")
    print(f"   Cache Hits: {runtime_stats.get('news_cache_hits', 0)}")
    print(f"   Cache Before: {before_cache}")
    print(f"   Cache After: {len(news_cache)}")
    print(f"   Positive Cached: {positive}")
    print(f"   Negative Cached: {negative}")
    print(f"   Serious Reject Cached: {serious}")
    print(f"   Neutral Cached: {neutral}")
    print(f"⏱ News Cycle Time: {fmt_sec(start_ts)}")
    print("══════════════════════════════════════════")
    
def run_monitor_cycle():
    start_ts = time.time()

    print_cycle_header("🎯 Monitoring Cycle")

    before_count = len(active_monitoring)

    monitor_active_trades()

    after_count = len(active_monitoring)

    print("")
    print("🎯 Monitoring Diagnostic")
    print(f"   Active Before: {before_count}")
    print(f"   Active After: {after_count}")
    print(f"   T1 Hits Total: {runtime_stats.get('t1_hits', 0)}")
    print(f"   T2 Hits Total: {runtime_stats.get('t2_hits', 0)}")
    print(f"   T3 Hits Total: {runtime_stats.get('t3_hits', 0)}")
    print(f"   Momentum Continues Total: {runtime_stats.get('momentum_continues', 0)}")
    print(f"   Exits Total: {runtime_stats.get('exits', 0)}")

    if active_monitoring:
        print("📌 Active Symbols:")
        for symbol, trade in active_monitoring.items():
            print(
                f"   {symbol} | "
                f"Entry: {trade.get('entry')} | "
                f"Stop: {trade.get('stop')} | "
                f"T1: {trade.get('t1')} | "
                f"T2: {trade.get('t2')} | "
                f"T3: {trade.get('t3')}"
            )
    else:
        print("📌 Active Symbols: لا يوجد")

    print(f"⏱ Monitor Cycle Time: {fmt_sec(start_ts)}")
    print("══════════════════════════════════════════")

def save_cycle():
    start_ts = time.time()

    save_runtime_state()

    print("")
    print("💾 Redis Save")
    print(f"   State Saved: ✅")
    print(f"   Active Monitoring: {len(active_monitoring)}")
    print(f"   Sent Alerts: {len(sent_alerts)}")
    print(f"   Priority Universe: {len(priority_universe)}")
    print(f"   News Cache: {len(news_cache)}")
    print(f"   Runtime Stats Saved: ✅")
    print(f"⏱ Save Time: {fmt_sec(start_ts)}")
    print("")

# =========================================================
# MAIN LOOP
# =========================================================

def run_phase(name, func):
    start_ts = time.time()

    print("")
    print(f"▶️ START {name} | {now_ksa().strftime('%H:%M:%S')}")

    try:
        result = func()
        print(f"✅ END {name} | Time: {fmt_sec(start_ts)}")
        return result

    except Exception as e:
        print(f"🔥 ERROR in {name}: {e}")
        traceback.print_exc()
        return None


def main_loop():
    last_scan_ts = 0
    last_monitor_ts = 0
    last_news_ts = 0
    last_save_ts = 0
    last_heartbeat_ts = 0

    while True:
        try:
            now_ts = time.time()

            # =====================================================
            # HEARTBEAT
            # =====================================================
            if now_ts - last_heartbeat_ts >= 15:
                print(
                    f"💓 Heartbeat | "
                    f"KSA {now_ksa().strftime('%H:%M:%S')} | "
                    f"Session={get_session_profile_name()} | "
                    f"Universe={len(priority_universe)} | "
                    f"Monitoring={len(active_monitoring)} | "
                    f"NewsQueue={len(news_queue)}"
                )

                last_heartbeat_ts = now_ts

            # =====================================================
            # WORK TIME
            # =====================================================
            if not is_work_time():
                wait_seconds = min(300, seconds_until_next_work_start())

                print(
                    f"⏸ خارج وقت العمل | "
                    f"KSA {now_ksa().strftime('%H:%M:%S')} | "
                    f"Next Check={wait_seconds}s"
                )

                time.sleep(wait_seconds)
                continue

            # =====================================================
            # MAIN SCAN
            # =====================================================
            if now_ts - last_scan_ts >= MAIN_SCAN_INTERVAL:
                last_scan_ts = now_ts
                run_phase("SCAN", run_scan_cycle)

            # =====================================================
            # MONITOR
            # =====================================================
            if now_ts - last_monitor_ts >= MONITOR_INTERVAL:
                last_monitor_ts = now_ts
                run_phase("MONITOR", run_monitor_cycle)

            # =====================================================
            # NEWS
            # =====================================================
            if now_ts - last_news_ts >= NEWS_QUEUE_INTERVAL:
                last_news_ts = now_ts
                run_phase("NEWS", run_news_cycle)

            # =====================================================
            # SAVE
            # =====================================================
            if now_ts - last_save_ts >= 60:
                last_save_ts = now_ts
                run_phase("SAVE", save_cycle)

            time.sleep(1)

        except KeyboardInterrupt:
            print("🛑 Bot stopped manually")
            save_runtime_state()
            break

        except Exception as e:
            print(f"🔥 Main Loop Error: {e}")
            traceback.print_exc()
            time.sleep(10)

# =========================================================
# ENTRY POINT
# =========================================================
if __name__ == "__main__":
    startup()

    if RESTORE_ACTIVE_MONITORING:
        recover_active_monitoring_after_restart()

    main_loop()
    
