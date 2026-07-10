import os
import time
import json
import threading
import requests
import numpy as np
import pandas as pd
import alpaca_trade_api as tradeapi

from flask import Flask
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# =========================================================
# TIMEZONES
# =========================================================

KSA_TZ = ZoneInfo("Asia/Riyadh")
NY_TZ = ZoneInfo("America/New_York")


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)

total_scans = 0
alerts_sent = 0
last_scan_time = "Never"
last_universe_build_time = "Never"
preliminary_universe_count = 0
final_universe_count = 0
active_monitoring_count = 0


@app.route("/")
def home():
    return (
        "💵 Penny Cents Radar Running Perfectly 24/7!<br>"
        f"📊 Total Scans: {total_scans}<br>"
        f"🚨 Alerts Sent: {alerts_sent}<br>"
        f"👀 Active Monitoring: {active_monitoring_count}<br>"
        f"📋 Preliminary Universe: {preliminary_universe_count}<br>"
        f"✅ Final Universe: {final_universe_count}<br>"
        f"⏱️ Last Scan: {last_scan_time}<br>"
        f"🧱 Last Universe Build: {last_universe_build_time}"
    ), 200


def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


# =========================================================
# ENV
# =========================================================

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
FLOAT_CACHE_FILENAME = "float_cache.json"


# =========================================================
# ALPACA
# =========================================================

api = tradeapi.REST(
    APCA_API_KEY_ID,
    APCA_API_SECRET_KEY,
    APCA_API_BASE_URL,
    api_version="v2"
)


# =========================================================
# SETTINGS
# =========================================================

PRICE_MIN = 0.20
PRICE_MAX = 1.50

MAX_FLOAT = 50_000_000
MAX_SPREAD_PCT = 2.0

MIN_SCORE = 90

SCAN_INTERVAL_SEC = 60
MONITOR_INTERVAL_SEC = 30

RE_ALERT_BLOCK_HOURS = 2
MAX_MONITOR_MINUTES = 120
WEAKNESS_GRACE_SECONDS = 120
BARS_LIMIT = 120
ATR_PERIOD = 14

REDIS_UNIVERSE_PRELIM = "penny:v2:universe:preliminary"
REDIS_UNIVERSE_FINAL = "penny:v2:universe:final"
REDIS_ACTIVE_TRADES = "penny:v2:active_trades"
REDIS_SENT_ALERTS = "penny:v2:sent_alerts"
REDIS_LAST_PRELIM_DATE = "penny:v2:last_prelim_date"
REDIS_LAST_FINAL_DATE = "penny:v2:last_final_date"

BAD_NAME_KEYWORDS = [
    "ETF", "ETN", "FUND", "TRUST", "INDEX",
    "WARRANT", "WARRANTS", "UNIT", "RIGHT",
    "SPAC", "ACQUISITION", "BLANK CHECK",
    "PREFERRED", "NOTE", "BOND"
]

SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR",
    "BUD", "TAP", "STZ", "DEO", "PM", "MO", "BTI",
    "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL", "AMC", "CNK", "IMAX"
}

# Cache for bad news to avoid duplicate API calls in same scan cycle
_bad_news_cache = {}
_bad_news_cache_time = {}
BAD_NEWS_CACHE_TTL_SEC = 3600  # 1 hour


# =========================================================
# REDIS
# =========================================================

def redis_headers():
    return {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json"
    }


def redis_command(command):
    try:
        r = requests.post(
            UPSTASH_REDIS_REST_URL,
            headers=redis_headers(),
            json=command,
            timeout=15
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"Redis command error {command}: {e}")
        return None


def redis_get(key, default=None):
    result = redis_command(["GET", key])

    if result is None:
        return default

    try:
        parsed = json.loads(result)

        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except Exception:
                pass

        return parsed

    except Exception:
        return result


def redis_set(key, value):
    payload = json.dumps(value)
    result = redis_command(["SET", key, payload])
    return result == "OK"


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    global alerts_sent

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env missing")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        r = requests.post(url, json=payload, timeout=10)

        if r.status_code == 200:
            alerts_sent += 1
            return True

        print("Telegram error:", r.text)
        return False

    except Exception as e:
        print("Telegram exception:", e)
        return False


def send_startup_message():
    try:
        msg = (
            "✅ <b>Penny Cents Radar Started Successfully</b>\n\n"
            f"🕒 Time KSA: {now_ksa().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "💵 Mode: Penny Cents Alerts\n"
            "📡 Status: Running"
        )
        send_telegram(msg)
    except Exception as e:
        print(f"Startup message error: {e}")


def send_universe_ready_message():
    try:
        prelim = redis_get(REDIS_UNIVERSE_PRELIM, [])
        final = redis_get(REDIS_UNIVERSE_FINAL, [])

        msg = (
            "✅ <b>Penny Universe Ready</b>\n\n"
            f"📋 Preliminary Universe: {len(prelim)}\n"
            f"✅ Final Universe: {len(final)}\n"
            f"🕒 Time KSA: {now_ksa().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram(msg)
    except Exception as e:
        print(f"Universe ready message error: {e}")


# =========================================================
# GIST FLOAT CACHE
# =========================================================

def load_float_cache():
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()

        gist = r.json()
        content = gist["files"][FLOAT_CACHE_FILENAME]["content"]

        return json.loads(content)

    except Exception as e:
        print(f"Float cache load error: {e}")
        return {}


def get_float_value(float_cache, symbol):
    val = float_cache.get(symbol)

    if isinstance(val, dict):
        val = (
            val.get("float")
            or val.get("floatShares")
            or val.get("shares_float")
            or val.get("freeFloat")
        )

    try:
        return float(val)
    except Exception:
        return None


# =========================================================
# TIME HELPERS
# =========================================================

def now_ksa():
    return datetime.now(KSA_TZ)


def now_ny():
    return datetime.now(NY_TZ)


def today_ksa_str():
    return now_ksa().strftime("%Y-%m-%d")


def is_scan_time_allowed():
    n = now_ny()

    if n.weekday() >= 5:
        return False

    start = n.replace(hour=4, minute=0, second=0, microsecond=0)
    end = n.replace(hour=16, minute=0, second=0, microsecond=0)

    return start <= n <= end


def should_build_preliminary():
    n = now_ksa()
    last_date = redis_get(REDIS_LAST_PRELIM_DATE)
    return n.hour >= 10 and last_date != today_ksa_str()


def should_build_final():
    n = now_ksa()
    last_date = redis_get(REDIS_LAST_FINAL_DATE)
    return n.hour >= 11 and last_date != today_ksa_str()


# =========================================================
# CLEAN FILTERS
# =========================================================

def is_clean_symbol(symbol):
    if not symbol:
        return False

    if len(symbol) > 5:
        return False

    if not symbol.isalpha():
        return False

    if symbol.endswith(("W", "U", "R", "Q", "Y", "F")):
        return False

    return True


def has_bad_name(name):
    if not name:
        return False

    upper_name = name.upper()
    return any(k in upper_name for k in BAD_NAME_KEYWORDS)


def is_blacklisted(symbol):
    return symbol.upper() in SYMBOL_BLACKLIST


# =========================================================
# NEWS EXCLUSION — WITH LOCAL CACHE
# =========================================================

def has_bad_news(symbol):
    if not FINNHUB_API_KEY:
        return False

    # Return cached result if still fresh
    now_ts = time.time()
    if symbol in _bad_news_cache:
        if now_ts - _bad_news_cache_time.get(symbol, 0) < BAD_NEWS_CACHE_TTL_SEC:
            return _bad_news_cache[symbol]

    try:
        to_date = datetime.now(timezone.utc).date()
        from_date = to_date - timedelta(days=7)

        url = "https://finnhub.io/api/v1/company-news"
        params = {
            "symbol": symbol,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "token": FINNHUB_API_KEY
        }

        r = requests.get(url, params=params, timeout=10)

        if r.status_code != 200:
            return False

        news = r.json()[:20]

        bad_words = [
            "reverse split",
            "reverse stock split",
            "delisting",
            "delist",
            "bankruptcy",
            "chapter 11",
            "non-compliance",
            "nasdaq notice",
            "nyse notice",
            "liquidation",
            "going concern"
        ]

        result = False
        for item in news:
            text = f"{item.get('headline', '')} {item.get('summary', '')}".lower()
            if any(w in text for w in bad_words):
                result = True
                break

        _bad_news_cache[symbol] = result
        _bad_news_cache_time[symbol] = now_ts
        return result

    except Exception as e:
        print(f"News check error {symbol}: {e}")
        return False


# =========================================================
# MARKET DATA
# =========================================================

def get_snapshot_price_and_spread(symbol):
    try:
        snap = api.get_snapshot(symbol)

        price = None
        bid = None
        ask = None

        if getattr(snap, "latest_trade", None):
            price = float(snap.latest_trade.price)

        if getattr(snap, "latest_quote", None):
            bid = float(snap.latest_quote.bid_price or 0)
            ask = float(snap.latest_quote.ask_price or 0)

        if not price or price <= 0:
            return None, None

        spread_pct = None

        if bid and ask and bid > 0 and ask > 0 and ask >= bid:
            mid = (bid + ask) / 2
            spread_pct = ((ask - bid) / mid) * 100 if mid > 0 else None

        return price, spread_pct

    except Exception as e:
        msg = str(e).lower()
        if "no snapshot" not in msg:
            print(f"Snapshot error {symbol}: {e}")
        return None, None


def get_1m_bars(symbol, limit=BARS_LIMIT):
    try:
        bars = api.get_bars(symbol, tradeapi.TimeFrame.Minute, limit=limit).df

        if bars.empty:
            return None

        if "symbol" in bars.columns:
            bars = bars[bars["symbol"] == symbol]

        return bars.tail(limit)

    except Exception as e:
        print(f"Bars error {symbol}: {e}")
        return None


# =========================================================
# INDICATORS
# =========================================================

def calc_atr(df, period=ATR_PERIOD):
    if len(df) < period + 2:
        return None

    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ],
        axis=1
    ).max(axis=1)

    atr = tr.rolling(period).mean().iloc[-1]

    return float(atr) if not np.isnan(atr) else None


def calc_obv(df):
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

    return pd.Series(obv, index=df.index)


def calc_rvol(df):
    if len(df) < 30:
        return 0

    current_vol = df["volume"].tail(5).mean()
    avg_vol = df["volume"].iloc[:-5].mean()

    if avg_vol <= 0:
        return 0

    return float(current_vol / avg_vol)


def score_rvol(rvol):
    if rvol >= 10:
        return 30
    if rvol >= 7:
        return 25
    if rvol >= 5:
        return 20
    if rvol >= 3:
        return 15
    return 0


def score_volume_acceleration(df):
    if len(df) < 12:
        return 0, 0

    last_3 = df["volume"].tail(3).mean()
    prev_7 = df["volume"].iloc[-10:-3].mean()

    if prev_7 <= 0:
        return 0, 0

    ratio = last_3 / prev_7

    if ratio >= 4:
        return 25, ratio
    if ratio >= 3:
        return 20, ratio
    if ratio >= 2:
        return 15, ratio
    if ratio >= 1.5:
        return 8, ratio

    return 0, ratio


def score_float(float_shares):
    if float_shares <= 5_000_000:
        return 25
    if float_shares <= 10_000_000:
        return 22
    if float_shares <= 20_000_000:
        return 18
    if float_shares <= 30_000_000:
        return 12
    if float_shares <= 50_000_000:
        return 5
    return 0


def score_obv(df):
    if len(df) < 20:
        return 0, False

    obv = calc_obv(df)
    obv_ema10 = obv.ewm(span=10, adjust=False).mean()

    if obv.iloc[-1] > obv_ema10.iloc[-1] and obv.iloc[-1] > obv.iloc[-3]:
        return 10, True

    if obv.iloc[-1] > obv_ema10.iloc[-1]:
        return 7, True

    return 0, False


def score_breakout(df):
    if len(df) < 30:
        return 0, None

    current = float(df["close"].iloc[-1])
    recent_high = float(df["high"].tail(20).max())
    hod = float(df["high"].max())

    nearest_level = recent_high

    if abs(hod - current) < abs(recent_high - current):
        nearest_level = hod

    if current >= nearest_level:
        return 10, nearest_level

    distance_pct = ((nearest_level - current) / current) * 100

    if distance_pct <= 0.5:
        return 7, nearest_level
    if distance_pct <= 1.0:
        return 4, nearest_level

    return 0, nearest_level


def find_recent_swing_low(df, lookback=20):
    return float(df["low"].tail(lookback).min())


# =========================================================
# UNIVERSE BUILD
# =========================================================

def build_preliminary_universe():
    global preliminary_universe_count, last_universe_build_time

    print("Building preliminary penny universe...", flush=True)

    universe = []

    try:
        assets = api.list_assets(status="active")

        for asset in assets:
            symbol = asset.symbol.upper()
            name = getattr(asset, "name", "") or ""

            exchange = str(getattr(asset, "exchange", "") or "").upper()
            if not any(x in exchange for x in ["NASDAQ", "NYSE", "AMEX"]):
                continue

            if not getattr(asset, "tradable", False):
                continue

            if not is_clean_symbol(symbol):
                continue

            if is_blacklisted(symbol):
                continue

            if has_bad_name(name):
                continue

            price, spread_pct = get_snapshot_price_and_spread(symbol)

            if price is None:
                continue

            if not (PRICE_MIN <= price <= PRICE_MAX):
                continue

            if spread_pct is not None and spread_pct > MAX_SPREAD_PCT:
                continue

            universe.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "price": price,
                    "spread_pct": spread_pct,
                    "built_at": datetime.now(timezone.utc).isoformat()
                }
            )

            time.sleep(0.05)

        redis_set(REDIS_UNIVERSE_PRELIM, universe)
        redis_set(REDIS_LAST_PRELIM_DATE, today_ksa_str())

        preliminary_universe_count = len(universe)
        last_universe_build_time = now_ksa().strftime("%Y-%m-%d %H:%M:%S KSA")

        print(f"Preliminary universe built: {len(universe)} symbols", flush=True)

    except Exception as e:
        print(f"Preliminary universe build error: {e}")


def build_final_universe(force=False):
    global final_universe_count, last_universe_build_time

    print("Building final penny universe...", flush=True)

    float_cache = load_float_cache()
    preliminary = redis_get(REDIS_UNIVERSE_PRELIM, [])

    if not isinstance(preliminary, list):
        preliminary = []

    if not preliminary or force:
        build_preliminary_universe()
        preliminary = redis_get(REDIS_UNIVERSE_PRELIM, [])

    if not isinstance(preliminary, list):
        preliminary = []

    final = []

    for item in preliminary:
        if not isinstance(item, dict):
            continue

        symbol = item.get("symbol")

        if not symbol:
            continue

        float_shares = get_float_value(float_cache, symbol)

        if float_shares is None:
            continue

        if float_shares > MAX_FLOAT:
            continue

        if has_bad_news(symbol):
            continue

        item["float"] = float_shares
        item["final_built_at"] = datetime.now(timezone.utc).isoformat()
        final.append(item)

    redis_set(REDIS_UNIVERSE_FINAL, final)
    redis_set(REDIS_LAST_FINAL_DATE, today_ksa_str())

    final_universe_count = len(final)
    last_universe_build_time = now_ksa().strftime("%Y-%m-%d %H:%M:%S KSA")

    print(f"Final universe built: {len(final)} symbols", flush=True)


def startup_universe_check():
    final = redis_get(REDIS_UNIVERSE_FINAL, [])
    last_final_date = redis_get(REDIS_LAST_FINAL_DATE)

    if not isinstance(final, list):
        final = []

    if not final or last_final_date != today_ksa_str():
        print("No valid final universe for today. Building immediately...")
        build_preliminary_universe()
        build_final_universe(force=False)
        send_universe_ready_message()


# =========================================================
# ALERT LOGIC
# =========================================================

def already_alerted_recently(symbol):
    sent = redis_get(REDIS_SENT_ALERTS, {})

    if not isinstance(sent, dict):
        sent = {}

    last = sent.get(symbol)

    if not last:
        return False

    try:
        last_dt = datetime.fromisoformat(last)
        return datetime.now(timezone.utc) - last_dt < timedelta(hours=RE_ALERT_BLOCK_HOURS)
    except Exception:
        return False


def mark_alerted(symbol):
    sent = redis_get(REDIS_SENT_ALERTS, {})

    if not isinstance(sent, dict):
        sent = {}

    sent[symbol] = datetime.now(timezone.utc).isoformat()
    redis_set(REDIS_SENT_ALERTS, sent)


def analyze_symbol(item):
    if not isinstance(item, dict):
        return None

    symbol = item.get("symbol")
    float_shares = item.get("float")

    if not symbol or not float_shares:
        return None

    price, spread_pct = get_snapshot_price_and_spread(symbol)

    if price is None:
        return None

    if not (PRICE_MIN <= price <= PRICE_MAX):
        return None

    if spread_pct is not None and spread_pct > MAX_SPREAD_PCT:
        return None

    df = get_1m_bars(symbol)

    if df is None or len(df) < 40:
        return None

    rvol = calc_rvol(df)
    rvol_score = score_rvol(rvol)

    vol_score, vol_acc_ratio = score_volume_acceleration(df)
    float_score = score_float(float_shares)
    obv_score, obv_ok = score_obv(df)
    breakout_score, breakout_level = score_breakout(df)

    total_score = rvol_score + vol_score + float_score + obv_score + breakout_score

    if total_score >= 70:
        print(
            f"📊 {symbol} Score={total_score} | "
            f"RVOL={rvol_score} | "
            f"Accel={vol_score} | "
            f"Float={float_score} | "
            f"OBV={obv_score} | "
            f"Breakout={breakout_score}",
            flush=True
        )

    if total_score < MIN_SCORE:
        return None

    atr = calc_atr(df)

    if not atr or atr <= 0:
        return None

    live_price, live_spread_pct = get_snapshot_price_and_spread(symbol)

    if live_price is None:
        return None

    if not (PRICE_MIN <= live_price <= PRICE_MAX):
        return None

    if (
        live_spread_pct is not None
        and live_spread_pct > MAX_SPREAD_PCT
    ):
        return None

    entry = round(float(live_price), 4)
    t1 = round(entry + (1.5 * atr), 4)
    t2 = round(entry + (3.0 * atr), 4)
    
    swing_low = find_recent_swing_low(df)
    max_stop = entry * 0.95
    stop = max(swing_low, max_stop)
    stop = round(stop, 4)

    if stop >= entry:
        return None

    return {
        "symbol": symbol,
        "entry": entry,
        "t1": t1,
        "t2": t2,
        "stop": stop,
        "score": total_score,
        "rvol": round(rvol, 2),
        "volume_acceleration": round(vol_acc_ratio, 2),
        "float": float_shares,
        "spread_pct": (
            round(live_spread_pct, 2)
            if live_spread_pct is not None
            else None
        ), 
        "breakout_level": breakout_level,
        "alerted_at": datetime.now(timezone.utc).isoformat(),
        "status": "active"
    }


def format_float(float_shares):
    if float_shares >= 1_000_000:
        return f"{float_shares / 1_000_000:.1f}M"

    return f"{float_shares:,.0f}"

def send_entry_alert(data):
    msg = (
        "💵 <b>دخول ربح سنتات</b>\n\n"
        f"📌 السهم: <b>{data['symbol']}</b>\n"
        f"💰 الدخول: <b>{data['entry']}</b>\n"
        f"🎯 الهدف الأول: <b>{data['t1']}</b>\n"
        f"🚀 الهدف الثاني: <b>{data['t2']}</b>\n"
        f"🛑 وقف الخسارة: <b>{data['stop']}</b>\n\n"
        f"🏆 الدرجة: <b>{data['score']}/100</b>\n"
        f"📊 القوة النسبية للحجم: <b>{data['rvol']}</b>\n"
        f"⚡ تسارع الحجم: <b>{data['volume_acceleration']}x</b>\n"
        f"🪶 الفلوت: <b>{format_float(data['float'])}</b>\n"
        f"↔️ السبريد: <b>{data['spread_pct']}%</b>\n"
        f"📈 مستوى الاختراق: <b>{round(data['breakout_level'], 4) if data['breakout_level'] else 'غير متوفر'}</b>\n\n"
        f"⏱️ مدة المتابعة: <b>{MAX_MONITOR_MINUTES} دقيقة</b>"
    )

    return send_telegram(msg)
    
def save_active_trade(data):
    global active_monitoring_count

    trades = redis_get(REDIS_ACTIVE_TRADES, {})

    if not isinstance(trades, dict):
        trades = {}

    trades[data["symbol"]] = data
    redis_set(REDIS_ACTIVE_TRADES, trades)

    active_monitoring_count = len(trades)

# =========================================================
# MONITORING
# =========================================================

def check_trade_weakness(symbol, trade, current_price):
    df = get_1m_bars(symbol, limit=60)

    if df is None or len(df) < 20:
        return False, "لا توجد بيانات كافية"

    price = float(current_price)

    if price <= trade["stop"]:
        return True, "كسر وقف الخسارة"

    typical = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_volume = df["volume"].cumsum()

    if cumulative_volume.iloc[-1] <= 0:
        return False, ""

    vwap = (typical * df["volume"]).cumsum() / cumulative_volume

    if price < float(vwap.iloc[-1]):
        return True, "فقدان متوسط السعر المرجح بالحجم"

    _, obv_ok = score_obv(df)

    if not obv_ok:
        return True, "تحول مؤشر تدفق الحجم إلى سلبي"

    _, ratio = score_volume_acceleration(df)

    if ratio < 0.8:
        return True, "انهيار واضح في تسارع الحجم"

    return False, ""

def monitor_trades_loop():
    global active_monitoring_count

    while True:
        try:
            trades = redis_get(REDIS_ACTIVE_TRADES, {})

            if not isinstance(trades, dict):
                trades = {}

            active_monitoring_count = len(trades)
            changed = False

            for symbol, trade in list(trades.items()):
                if not isinstance(trade, dict):
                    trades.pop(symbol, None)
                    changed = True
                    continue

                price, _ = get_snapshot_price_and_spread(symbol)

                if price is None:
                    continue

                alerted_at = datetime.fromisoformat(trade["alerted_at"])
                age = datetime.now(timezone.utc) - alerted_at

                if price >= trade["t2"]:
                    send_telegram(
                        f"🚀 <b>تحقق الهدف الثاني</b>\n\n"
                        f"السهم: <b>{symbol}</b>\n"
                        f"السعر الحالي: <b>{round(price, 4)}</b>\n"
                        f"الهدف الثاني: <b>{trade['t2']}</b>"
                    )

                    trades.pop(symbol, None)
                    changed = True
                    continue

                if price >= trade["t1"] and not trade.get("t1_sent"):
                    send_telegram(
                        f"🎯 <b>تحقق الهدف الأول</b>\n\n"
                        f"السهم: <b>{symbol}</b>\n"
                        f"السعر الحالي: <b>{round(price, 4)}</b>\n"
                        f"الهدف الأول: <b>{trade['t1']}</b>"
                    )

                    trade["t1_sent"] = True
                    trades[symbol] = trade
                    changed = True

                if price <= trade["stop"]:
                    send_telegram(
                        f"⚠️ <b>إشارة خروج / ضعف</b>\n\n"
                        f"السهم: <b>{symbol}</b>\n"
                        f"السعر الحالي: <b>{round(price, 4)}</b>\n"
                        f"السبب: <b>كسر وقف الخسارة</b>"
                    )

                    trades.pop(symbol, None)
                    changed = True
                    continue

                if age.total_seconds() >= WEAKNESS_GRACE_SECONDS:
                    weak, reason = check_trade_weakness(
                        symbol,
                        trade,
                        price
                    )

                    if weak:
                        send_telegram(
                            f"⚠️ <b>إشارة خروج / ضعف</b>\n\n"
                            f"السهم: <b>{symbol}</b>\n"
                            f"السعر الحالي: <b>{round(price, 4)}</b>\n"
                            f"السبب: <b>{reason}</b>"
                        )

                        trades.pop(symbol, None)
                        changed = True
                        continue

                if age >= timedelta(minutes=MAX_MONITOR_MINUTES):
                    send_telegram(
                        f"⏱️ <b>انتهاء فترة المراقبة</b>\n\n"
                        f"السهم: <b>{symbol}</b>\n"
                        f"السعر الحالي: <b>{round(price, 4)}</b>\n"
                        "انتهت نافذة المتابعة المحددة."
                    )

                    trades.pop(symbol, None)
                    changed = True

            if changed:
                redis_set(REDIS_ACTIVE_TRADES, trades)

        except Exception as e:
            print(f"Monitor loop error: {e}")

        time.sleep(MONITOR_INTERVAL_SEC)


# =========================================================
# MAIN SCANNER
# =========================================================

def scanner_loop():
    global total_scans, last_scan_time, final_universe_count

    print("🚀 Scanner Loop Started", flush=True)
    
    startup_universe_check()

    while True:
        try:
            if should_build_preliminary():
                build_preliminary_universe()

            if should_build_final():
                build_final_universe()
                send_universe_ready_message()

            if not is_scan_time_allowed():
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            universe = redis_get(REDIS_UNIVERSE_FINAL, [])

            if not isinstance(universe, list):
                universe = []

            final_universe_count = len(universe)

            if not universe:
                startup_universe_check()
                universe = redis_get(REDIS_UNIVERSE_FINAL, [])

                if not isinstance(universe, list):
                    universe = []

            total_scans += 1
            last_scan_time = now_ksa().strftime("%Y-%m-%d %H:%M:%S KSA")

            print(
                f"🔍 Scan #{total_scans} | "
                f"Universe={len(universe)} | "
                f"Time={now_ksa().strftime('%H:%M:%S')} KSA",
                flush=True
            )

            for item in universe:
                if not isinstance(item, dict):
                    continue

                symbol = item.get("symbol")

                if not symbol:
                    continue

                if already_alerted_recently(symbol):
                    continue

                signal = analyze_symbol(item)

                if not signal:
                    continue

                if send_entry_alert(signal):
                    mark_alerted(symbol)
                    save_active_trade(signal)

                time.sleep(0.15)

        except Exception as e:
            print(f"Scanner loop error: {e}")

        time.sleep(SCAN_INTERVAL_SEC)


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(2)

    threading.Thread(target=send_startup_message, daemon=True).start()
    threading.Thread(target=monitor_trades_loop, daemon=True).start()

    print("➡️ About to start scanner_loop")

    scanner_loop()
