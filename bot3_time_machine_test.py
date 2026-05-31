import os
import time
import json
import requests
import pandas as pd
import alpaca_trade_api as tradeapi
from datetime import datetime, timedelta, timezone
import pytz
from flask import Flask
import threading

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_BOT3_CHAT_ID = os.getenv("TELEGRAM_BOT3_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN_BOT3")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
BOT3_EARLY_CANDIDATES_REDIS_KEY = "bot3_early_candidates"
BOT3_ACTIVE_TRADES_REDIS_KEY = "bot3_active_trades"

LIVE_MOVERS_REDIS_KEY = "live_movers"

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)
saudi_tz = pytz.timezone("Asia/Riyadh")
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot 3 Time Machine Test Running"


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
watchlist = {}
sent_alerts = {}
active_trades = {}
explosion_tracking = {}
last_saved_active_trades = ""
pending_watchlist = {}
momentum_watchlist = {}
last_saved_pending_candidates = ""

PRICE_MIN = 0.4
PRICE_MAX = 25
# =========================
# TIME MACHINE TEST SETTINGS
# =========================

TIME_MACHINE_MODE = True

TEST_DATE = "2026-05-28"  # الأربعاء - عدل التاريخ حسب اليوم المطلوب
TEST_START_HOUR_NY = 9
TEST_START_MINUTE_NY = 30
TEST_DURATION_MINUTES = 120

WATCH_MINUTES = 45
SCAN_INTERVAL = 20
PENDING_MAX_AGE_MINUTES = 90
BULK_BARS_CHUNK_SIZE = 100

LIVE_MOVERS_FILE = "live_movers.json" 
MASTER_LIST_FILE = "master_list.json"
BOT2_FINAL_FILE = "bot2_final_results.json"
BOT3_ACTIVE_TRADES_FILE = "bot3_active_trades.json"
BOT3_EARLY_CANDIDATES_FILE = "bot3_early_candidates.json"

SELF_SCAN_COUNT = 1500


def send_telegram_msg(message, chat_id):
    if TIME_MACHINE_MODE:
        message = "🧪 TIME MACHINE TEST\n\n" + message
        
    if not TELEGRAM_TOKEN or not chat_id:
        print("Telegram keys missing", flush=True)
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e, flush=True)


def can_send_trade_alerts():
    now = datetime.now(saudi_tz)
    weekday = now.weekday()
    current_minutes = now.hour * 60 + now.minute

    if weekday in [5, 6]:
        return False
    
    if 4 * 60 <= current_minutes <= 10 * 60 + 45:
        return False

    return True

def is_trading_time():
    return True

def read_gist_file(filename, default=None):
    if default is None:
        default = []

    if not GIST_ID or not GITHUB_TOKEN:
        return default

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        res = requests.get(url, headers=headers, timeout=15)
        data = res.json()

        file_data = data.get("files", {}).get(filename)
        if not file_data:
            return default

        content = file_data.get("content", "")
        if not content:
            return default

        return json.loads(content)

    except Exception as e:
        print(f"Gist read error ({filename}): {e}", flush=True)
        return default


def save_gist_file(filename, data):
    if not GIST_ID or not GITHUB_TOKEN:
        print("❌ GIST_ID or GITHUB_TOKEN missing", flush=True)
        return

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        content = json.dumps(data, ensure_ascii=False)

        res = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    filename: {
                        "content": content
                    }
                }
            },
            timeout=15
        )

        if res.status_code not in [200, 201]:
            print(f"❌ Save failed {filename}: {res.text[:300]}", flush=True)
            return

        print(f"✅ Saved {filename}", flush=True)

    except Exception as e:
        print(f"❌ Save gist error ({filename}): {e}", flush=True)


def load_master_list():
    data = read_gist_file(MASTER_LIST_FILE, default=[])
    symbols = []

    if isinstance(data, list):
        for item in data:

            if isinstance(item, str):
                symbol = item

            elif isinstance(item, dict):
                symbol = item.get("symbol")

                # ✅ إضافة مصدر السهم
                item["source_group"] = "MASTER LIST"

            else:
                continue

            if (
                symbol
                and isinstance(symbol, str)
                and "." not in symbol
                and "^" not in symbol
                and "-" not in symbol
                and "/" not in symbol
            ):
                symbols.append(symbol.upper().strip())

    return list(dict.fromkeys(symbols))

def save_json_to_redis(redis_key, data):
    try:
        if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
            print("⚠️ Upstash Redis env vars missing", flush=True)
            return False

        url = f"{UPSTASH_REDIS_REST_URL}/set/{redis_key}"

        headers = {
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"
        }

        payload = json.dumps(data)

        r = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10
        )

        if r.status_code in [200, 201]:
            print(f"✅ Saved to Redis: {redis_key}", flush=True)
            return True

        print(f"❌ Redis save failed {redis_key}: {r.status_code} {r.text}", flush=True)
        return False

    except Exception as e:
        print(f"❌ Redis save exception {redis_key}: {e}", flush=True)
        return False

def load_json_from_redis(redis_key, default=None):
    if default is None:
        default = {}

    try:
        if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
            print("⚠️ Upstash Redis env vars missing", flush=True)
            return default

        url = f"{UPSTASH_REDIS_REST_URL}/get/{redis_key}"

        headers = {
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"
        }

        r = requests.get(
            url,
            headers=headers,
            timeout=10
        )

        if r.status_code != 200:
            print(f"❌ Redis read failed {redis_key}: {r.status_code} {r.text}", flush=True)
            return default

        data = r.json().get("result")

        if not data:
            return default

        loaded = json.loads(data)

        if isinstance(loaded, str):
            loaded = json.loads(loaded)

        return loaded

    except Exception as e:
        print(f"❌ Redis read exception {redis_key}: {e}", flush=True)
        return default
        
def load_live_radar_from_redis():
    try:
        if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
            print("⚠️ Upstash Redis env vars missing", flush=True)
            return []

        url = f"{UPSTASH_REDIS_REST_URL}/get/{LIVE_MOVERS_REDIS_KEY}"

        headers = {
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"
        }

        r = requests.get(
            url,
            headers=headers,
            timeout=10
        )

        if r.status_code != 200:
            print(f"❌ Redis read failed: {r.status_code} {r.text}", flush=True)
            return []

        data = r.json().get("result")

        if not data:
            return []

        movers = json.loads(data)
        if isinstance(movers, str):
            movers = json.loads(movers)

        symbols = []
        now_ts = time.time()

        for item in movers:
            symbol = str(item.get("symbol", "")).upper().strip()
            
            if not symbol:
                continue

            ts = item.get("timestamp", 0)
            if ts and now_ts - ts > 180:
                continue

            symbols.append(symbol)

        symbols = list(dict.fromkeys(symbols))

        print(f"✅ Loaded live movers from Redis: {len(symbols)}", flush=True)
        return symbols

    except Exception as e:
        print(f"❌ Redis read exception: {e}", flush=True)
        return []
        
def load_live_movers():
    data = read_gist_file(LIVE_MOVERS_FILE, default=[])

    symbols = []
    now_ts = time.time()

    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                symbol = item
                item_time = now_ts
            elif isinstance(item, dict):
                symbol = item.get("symbol")
                item_time = item.get("time", 0)
            else:
                continue

            if not symbol:
                continue

            symbol = symbol.upper().strip()

            if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
                continue

            age = now_ts - item_time if item_time else 999999

            if age > 180:
                continue

            symbols.append(symbol)

    symbols = list(dict.fromkeys(symbols))

    print(
        f"⚡ Loaded Live Movers symbols: {len(symbols)}",
        flush=True
    )

    return symbols

def get_time_machine_window():
    ny_tz = pytz.timezone("America/New_York")

    test_day = datetime.strptime(
        TEST_DATE,
        "%Y-%m-%d"
    )

    start_ny = ny_tz.localize(
        datetime(
            test_day.year,
            test_day.month,
            test_day.day,
            TEST_START_HOUR_NY,
            TEST_START_MINUTE_NY
        )
    )

    end_ny = start_ny + timedelta(
        minutes=TEST_DURATION_MINUTES
    )

    global TIME_MACHINE_END_NY

    TIME_MACHINE_END_NY = end_ny

    return (
        start_ny.astimezone(pytz.UTC),
        end_ny.astimezone(pytz.UTC)
    )
    
def get_alpaca_bars_bulk(symbols, minutes=120):
    try:
        if TIME_MACHINE_MODE:
            start, end = get_time_machine_window()
        else:
            end = datetime.now(pytz.UTC)
            start = end - timedelta(days=1)
            
        bars = api.get_bars(
            symbols,
            tradeapi.TimeFrame.Minute,
            start=start.isoformat(),
            end=end.isoformat(),
            adjustment="raw"
        ).df

        print(
            f"🕰️ Time Machine bulk request: {start} → {end} | symbols={len(symbols)}",
            flush=True
        )

        if bars is None or bars.empty:
            return {}

        result = {}

        for symbol in symbols:
            try:
                df = bars[bars["symbol"] == symbol].copy()

                if df.empty:
                    continue

                df = df.rename(columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume"
                })

                needed = [
                    "Open",
                    "High",
                    "Low",
                    "Close",
                    "Volume"
                ]

                df = df[needed].dropna().tail(minutes)

                if not df.empty:
                    result[symbol] = df

            except Exception:
                continue

        return result

    except Exception as e:
        print(f"Bulk bars error: {e}", flush=True)
        return {}
        
def get_alpaca_bars(symbol, minutes=120):
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=1)

        bars = api.get_bars(
            symbol,
            tradeapi.TimeFrame.Minute,
            start=start.isoformat(),
            end=end.isoformat(),
            adjustment="raw"
        ).df

        if bars is None or bars.empty:
            return pd.DataFrame()

        df = bars.copy()

        if "symbol" in df.columns:
            df = df[df["symbol"] == symbol]

        df = df.rename(columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume"
        })

        needed = ["Open", "High", "Low", "Close", "Volume"]

        for col in needed:
            if col not in df.columns:
                return pd.DataFrame()

        return df[needed].dropna().tail(minutes)

    except Exception as e:
        print(f"Alpaca bars error {symbol}: {e}", flush=True)
        return pd.DataFrame()


def get_latest_price(symbol, df=None, use_realtime=True):
    if not use_realtime and df is not None and not df.empty:
        return float(df["Close"].iloc[-1])

    try:
        trade = api.get_latest_trade(symbol)
        return float(trade.price)

    except Exception:
        if df is not None and not df.empty:
            return float(df["Close"].iloc[-1])

        return 0

def calculate_rsi(close, period=14):
    if len(close) < period + 1:
        return 50

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=period).mean()

    if loss.iloc[-1] == 0:
        return 100

    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


def add_to_watchlist(symbol, source, price=0, bot2_grade="", bot2_score=0):
    now = datetime.now(saudi_tz)
    source_score = 1

    if "Bot 2" in source:
        source_score = 3
    elif "فحص ذاتي" in source:
        source_score = 2

    if symbol not in watchlist:
        watchlist[symbol] = {
            "source": source,
            "sources": [source],
            "priority_score": source_score,
            "first_price": float(price) if price else 0,
            "bot2_grade": bot2_grade,
            "bot2_score": bot2_score,
            "created_at": now,
            "alerted": False
        }

        print(f"🧠 Added watchlist: {symbol} | {source}", flush=True)

    else:
        if source not in watchlist[symbol].get("sources", []):
            watchlist[symbol]["sources"].append(source)
            watchlist[symbol]["priority_score"] += source_score

        watchlist[symbol]["source"] = " + ".join(watchlist[symbol]["sources"])

        if bot2_score > watchlist[symbol].get("bot2_score", 0):
            watchlist[symbol]["bot2_score"] = bot2_score
            watchlist[symbol]["bot2_grade"] = bot2_grade


def update_watchlist_from_bot2():
    bot2_final = read_gist_file(BOT2_FINAL_FILE, default=[])

    if not isinstance(bot2_final, list):
        return

    fresh = []
    now_ts = time.time()

    for r in bot2_final:
        symbol = r.get("symbol")
        if not symbol:
            continue

        grade = r.get("grade", "")

        try:
            age = now_ts - float(r.get("time", now_ts))
        except Exception:
            age = 0

        if age > 3600:
            continue

        if grade not in ["A", "A+", "A++"]:
            continue

        fresh.append(r)

    fresh = sorted(fresh, key=lambda x: x.get("final_score", 0), reverse=True)

    for r in fresh[:80]:
        add_to_watchlist(
            r["symbol"],
            "Bot 2 Final Result",
            r.get("price", 0),
            r.get("grade", ""),
            float(r.get("final_score", 0) or 0)
        )

def self_scan_top_400():

    live_symbols = []
    master_symbols = load_master_list()

    symbols = master_symbols[:SELF_SCAN_COUNT]

    print(
        f"🧪 Time Machine using Master List: {len(symbols)} symbols",
        flush=True
    )

    bars_map = {}

    for i in range(0, len(symbols), BULK_BARS_CHUNK_SIZE):
        chunk = symbols[i:i + BULK_BARS_CHUNK_SIZE]

        bars_map.update(
            get_alpaca_bars_bulk(
                chunk,
                minutes=120
            )
        )

    print(
        f"📦 Time Machine self-scan bulk bars loaded: {len(bars_map)}/{len(symbols)}",
        flush=True
    )
    
    symbols = list(dict.fromkeys(symbols))

    symbols = symbols[:SELF_SCAN_COUNT]

    if not symbols:
        print("⚠️ No symbols available", flush=True)
        return
        
    for i, symbol in enumerate(symbols, start=1):

        if i % 100 == 0:
            print(
                f"🔎 Bot 3 scanned {i}/{len(symbols)}",
                flush=True
            )

        try:

            df = bars_map.get(symbol, pd.DataFrame())
            
            if df.empty or len(df) < 30 or df["Volume"].mean() == 0:
                continue

            cp = get_latest_price(
                symbol,
                df,
                use_realtime=False
            )
            
            if not (PRICE_MIN <= cp <= PRICE_MAX):
                continue

            vwap = float((df["Close"] * df["Volume"]).sum() / df["Volume"].sum())
            rsi = calculate_rsi(df["Close"])
            instant_rvol = df["Volume"].tail(3).mean() / df["Volume"].mean()
            recent_move = ((cp - df["Close"].iloc[-10]) / df["Close"].iloc[-10]) * 100

            df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
            ema9 = float(df["EMA9"].iloc[-1])

            last_open = float(df["Open"].iloc[-1])
            last_close = float(df["Close"].iloc[-1])
            last_high = float(df["High"].iloc[-1])
            last_low = float(df["Low"].iloc[-1])

            candle_range = last_high - last_low
            if candle_range <= 0:
                continue

            close_position = (last_close - last_low) / candle_range
            upper_wick_pct = (last_high - last_close) / candle_range
            body_ratio = abs(last_close - last_open) / candle_range

            last_3_volume = float(df["Volume"].tail(3).mean())
            prev_10_volume = float(df["Volume"].tail(13).head(10).mean())

            volume_acceleration = last_3_volume >= prev_10_volume * 1.6

            strong_candle = (
                close_position >= 0.65
                and upper_wick_pct <= 0.35
                and body_ratio >= 0.35
            )

            vwap_reclaim = (
                float(df["Close"].iloc[-2]) < vwap
                and last_close > vwap
            )

            ema_reclaim = (
                float(df["Close"].iloc[-2]) < ema9
                and last_close > ema9
            )

            behavior_change = (
                recent_move >= 0.35
                and volume_acceleration
                and strong_candle
            )

            explosion_building_setup = (
                instant_rvol >= 2.5
                and 1.0 <= recent_move <= 6.0
                and volume_acceleration
                and strong_candle
                and cp > vwap
                and cp > ema9
                and close_position >= 0.70
                and upper_wick_pct <= 0.35
            )

            self_setup = (
                1.8 <= instant_rvol <= 6.0
                and 48 <= rsi <= 70
                and cp > vwap
                and cp > ema9
                and 0.30 <= recent_move <= 2.8
                and volume_acceleration
                and strong_candle
                and (vwap_reclaim or ema_reclaim or behavior_change)
            )

            if explosion_building_setup:
                add_to_watchlist(
                    symbol,
                    "🚀 Explosion Building - بداية انفجار محتمل",
                    cp
                )

            elif self_setup:
                add_to_watchlist(symbol, "فحص ذاتي Bot 3", cp)

            elif (
                instant_rvol >= 1.6
                and volume_acceleration
                and cp > ema9 * 0.995
            ):
                add_to_pending(
                    symbol,
                    cp,
                    "قريب من الانفجار"
                )

            if i % 50 == 0:
                print(f"🔎 Bot 3 scanned {i}/{len(symbols)}", flush=True)

            time.sleep(0.03)

        except Exception as e:
            print(f"Self scan error {symbol}: {e}", flush=True)
            continue

    print(
        f"✅ Bot 3 self scan completed: {len(symbols)} symbols",
        flush=True
    )
            
    
def add_to_pending(symbol, price, reason=""):
    symbol = str(symbol).upper().strip()
    now_ts = time.time()

    if symbol not in pending_watchlist:

        pending_watchlist[symbol] = {
            "symbol": symbol,
            "first_price": float(price),
            "last_price": float(price),
            "best_price": float(price),

            "reason": reason,
            "pending_score": 50,

            "times_checked": 0,
            "improve_count": 0,
            "weak_count": 0,


            "first_seen": now_ts,
            "last_update": now_ts,
            "expires_at": now_ts + (PENDING_MAX_AGE_MINUTES * 60),

            "status": "PENDING"
        }

        print(f"🟡 Added pending candidate: {symbol} | {reason}", flush=True)

    else:

        p = pending_watchlist[symbol]

        p["last_price"] = float(price)
        p["best_price"] = max(float(p.get("best_price", price)), float(price))
        p["last_update"] = now_ts

        if reason and reason not in str(p.get("reason", "")):
            p["reason"] = str(p.get("reason", "")) + " | " + reason
def update_pending_behavior(symbol, price, instant_rvol, recent_move, volume_acceleration, strong_candle, vwap_reclaim, ema_reclaim, distribution_score):
    if symbol not in pending_watchlist:
        return

    p = pending_watchlist[symbol]

    p["times_checked"] = int(p.get("times_checked", 0)) + 1
    p["last_price"] = float(price)
    p["best_price"] = max(float(p.get("best_price", price)), float(price))
    p["last_update"] = time.time()

    improved = (
        instant_rvol >= 2.0
        and recent_move >= 0.60
        and volume_acceleration
        and (strong_candle or vwap_reclaim or ema_reclaim)
        and distribution_score < 40
    )

    weak = (
        recent_move < 0.30
        or instant_rvol < 1.4
        or distribution_score >= 45
    )

    if improved:
        p["improve_count"] = int(p.get("improve_count", 0)) + 1
        p["pending_score"] = min(float(p.get("pending_score", 50)) + 8, 100)

    elif weak:
        p["weak_count"] = int(p.get("weak_count", 0)) + 1
        p["pending_score"] = max(float(p.get("pending_score", 50)) - 10, 0)

    if int(p.get("weak_count", 0)) >= 3:
        print(f"🧹 Removed pending after repeated weakness: {symbol}", flush=True)
        pending_watchlist.pop(symbol, None)
        return

    if float(p.get("pending_score", 50)) >= 75:
        p["status"] = "HOT_PENDING"
    elif float(p.get("pending_score", 50)) >= 55:
        p["status"] = "ACTIVE_PENDING"
    else:
        p["status"] = "WEAK_PENDING"

    pending_watchlist[symbol] = p
    
def clean_old_pending_watchlist():

    expired = []
    now_ts = time.time()

    for symbol, data in list(pending_watchlist.items()):

        try:
            expires_at = float(data.get("expires_at", 0))

            if expires_at <= 0:
                expires_at = float(data.get("first_seen", now_ts)) + (
                    PENDING_MAX_AGE_MINUTES * 60
                )

            if now_ts >= expires_at:
                expired.append(symbol)

        except Exception:
            expired.append(symbol)

    for symbol in expired:

        pending_watchlist.pop(symbol, None)

        print(
            f"🧹 Removed old pending candidate: {symbol}",
            flush=True
        )

def save_pending_candidates_if_changed():

    global last_saved_pending_candidates

    simplified = []

    for symbol, data in pending_watchlist.items():

        simplified.append({
            "symbol": symbol,
            "first_price": data.get("first_price", 0),
            "last_price": data.get("last_price", 0),
            "best_price": data.get("best_price", 0),
            "reason": data.get("reason", ""),
            "pending_score": data.get("pending_score", 0),
            "status": data.get("status", ""),
            "improve_count": data.get("improve_count", 0),
            "weak_count": data.get("weak_count", 0),
            "first_seen": data.get("first_seen", 0),
            "last_update": data.get("last_update", 0),
            "expires_at": data.get("expires_at", 0),
            "early_alert_sent": data.get("early_alert_sent", False)
        })

    simplified = sorted(
        simplified,
        key=lambda x: x.get("pending_score", 0),
        reverse=True
    )

    current_json = json.dumps(
        simplified,
        sort_keys=True
    )

    if current_json != last_saved_pending_candidates:

        save_json_to_redis(
            BOT3_EARLY_CANDIDATES_REDIS_KEY,
            simplified
        )

        last_saved_pending_candidates = current_json
        
def clean_old_watchlist():
    now = datetime.now(saudi_tz)
    expired = []

    for symbol, data in watchlist.items():
        if now - data["created_at"] > timedelta(minutes=WATCH_MINUTES):
            expired.append(symbol)

    for symbol in expired:
        watchlist.pop(symbol, None)


def grade_from_score(final_score):
    if final_score >= 90:
        return "A++"
    elif final_score >= 80:
        return "A+"
    elif final_score >= 70:
        return "A"
    elif final_score >= 60:
        return "B"
    else:
        return "C"


def detect_hidden_distribution(df, instant_rvol, recent_move, real_breakout):
    try:
        if df.empty or len(df) < 20:
            return {
                "hidden_distribution": False,
                "distribution_score": 0,
                "distribution_reasons": []
            }

        recent = df.tail(8).copy()

        opens = recent["Open"]
        highs = recent["High"]
        lows = recent["Low"]
        closes = recent["Close"]
        volumes = recent["Volume"]

        candle_range = (highs - lows).replace(0, 0.000001)
        body = (closes - opens).abs()
        upper_wicks = highs - closes

        close_position = (closes - lows) / candle_range
        upper_wick_pct = upper_wicks / candle_range
        body_ratio = body / candle_range

        distribution_score = 0
        reasons = []

        if instant_rvol >= 3.0 and recent_move < 0.75:
            distribution_score += 15
            reasons.append(
                "السيولة عالية لكن السعر لا يستجيب بقوة"
            )

        upper_wick_count = ((upper_wick_pct >= 0.40) & (close_position < 0.60)).sum()
        if upper_wick_count >= 3:
            distribution_score += 18
            reasons.append("ذيول علوية متكررة")

        avg_range_pct = ((highs - lows) / closes).mean() * 100
        vol_now = volumes.tail(3).mean()
        vol_prev = volumes.head(5).mean()

        if vol_now >= vol_prev * 1.5 and avg_range_pct < 0.45:
            distribution_score += 12
            reasons.append(
                "فوليوم قوي لكن حركة السعر ضعيفة"
            )

        higher_highs = highs.iloc[-1] > highs.iloc[-3]
        weak_close = closes.iloc[-1] < highs.iloc[-1] * 0.995 and close_position.iloc[-1] < 0.60

        if higher_highs and weak_close and not real_breakout:
            distribution_score += 15
            reasons.append(
                "قمم أعلى لكن بدون استمرار بالشراء"
            )

        last_move = ((closes.iloc[-1] - closes.iloc[-4]) / closes.iloc[-4]) * 100
        prev_move = ((closes.iloc[-4] - closes.iloc[-8]) / closes.iloc[-8]) * 100

        if instant_rvol >= 2.5 and last_move < prev_move * 0.45 and prev_move > 0.5:
            distribution_score += 12
            reasons.append("تقلص الزخم رغم استمرار RVOL")

        recent_high = highs.iloc[-5:-1].max()
        breakout_failed = (
            highs.iloc[-1] >= recent_high
            and closes.iloc[-1] < recent_high
            and close_position.iloc[-1] < 0.55
            and not real_breakout
        )

        if breakout_failed:
            distribution_score += 18
            reasons.append("فشل متابعة الاختراق")

        rise_speed = max(closes.tail(8).max() - closes.tail(8).min(), 0)
        drop_speed = max(highs.tail(4).max() - closes.iloc[-1], 0)

        if rise_speed > 0 and drop_speed / rise_speed >= 0.65:
            distribution_score += 10
            reasons.append("هبوط سريع مقارنة بالصعود")

        high_volume_weak_body = (
            vol_now >= vol_prev * 1.5
            and body_ratio.tail(3).mean() < 0.28
            and close_position.tail(3).mean() < 0.60
        )

        if high_volume_weak_body:
            distribution_score += 12
            reasons.append("فوليوم عالي مع جسم شموع ضعيف")

        return {
            "hidden_distribution": distribution_score >= 40,
            "distribution_score": distribution_score,
            "distribution_reasons": reasons
        }

    except Exception as e:
        print(f"Hidden distribution error: {e}", flush=True)
        return {
            "hidden_distribution": False,
            "distribution_score": 0,
            "distribution_reasons": []
        }

def add_to_momentum_watch(symbol, price, reason=""):
    symbol = str(symbol).upper().strip()

    if symbol not in momentum_watchlist:
        momentum_watchlist[symbol] = {
            "symbol": symbol,
            "first_price": float(price),
            "last_price": float(price),
            "best_price": float(price),
            "reason": reason,
            "checks": 0,
            "started_at": time.time(),
            "last_update": time.time(),
            "confirmed": False
        }

        print(
            f"🟠 Added momentum watch: {symbol} | {reason}",
            flush=True
        )
def estimate_target_timing(
    signal_type,
    instant_rvol,
    recent_move,
    move_3m,
    move_5m,
    acceleration_ok,
    scenario_explosion_setup,
    runner_escape_mode,
    early_momentum_mode
):
    if runner_escape_mode:
        return (
            "⚡ نوع الفرصة: انفجار سريع جدًا\n"
            "⏱️ المتوقع: هدف 1 خلال 5–15 دقيقة إذا استمر الزخم"
        )

    if scenario_explosion_setup:
        return (
            "🎯 نوع الفرصة: سيناريو انفجار محتمل\n"
            "⏱️ المتوقع: تأكيد الحركة أو هدف 1 خلال 15–45 دقيقة"
        )

    if early_momentum_mode:
        return (
            "🟡 نوع الفرصة: دخول مبكر\n"
            "⏱️ المتوقع: قد يحتاج 20–60 دقيقة قبل الانطلاق الكامل"
        )

    if (
        instant_rvol >= 4
        and recent_move >= 1.5
        and move_3m >= 0.50
        and acceleration_ok
    ):
        return (
            "🔥 نوع الفرصة: زخم قوي\n"
            "⏱️ المتوقع: هدف 1 خلال 10–25 دقيقة"
        )

    return (
        "✅ نوع الفرصة: دخول مؤكد\n"
        "⏱️ المتوقع: هدف 1 خلال 15–35 دقيقة إذا استمر الثبات فوق VWAP/EMA9"
    ) 

def classify_setup_strength(data):
    source = data.get("source", "")
    score = float(data.get("final_score", 0) or 0)
    rvol = float(data.get("instant_rvol", 0) or 0)
    move_3m = float(data.get("move_3m", 0) or 0)
    close_position = float(data.get("close_position", 0) or 0)
    distribution_score = float(data.get("distribution_score", 0) or 0)

    real_breakout = bool(data.get("real_breakout", False))
    volume_acceleration = bool(data.get("volume_acceleration", False))

    if (
        source == "LIVE_MOVERS"
        and score >= 88
        and rvol >= 4
        and move_3m >= 0.60
        and close_position >= 0.78
        and distribution_score < 15
        and volume_acceleration
        and real_breakout
    ):
        return "💎 فرصة ذهبية"

    if (
        score >= 80
        and rvol >= 3
        and move_3m >= 0.40
        and close_position >= 0.70
        and distribution_score < 25
        and volume_acceleration
    ):
        return "🔥 فرصة قوية جدًا"

    return "🟢 فرصة ممتازة"

def entry_quality_guard(
    symbol,
    df,
    cp,
    recent_move,
    move_3m,
    move_5m,
    instant_rvol,
    volume_acceleration,
    close_position,
    upper_wick_pct,
    vwap_reclaim,
    ema_reclaim,
    real_breakout,
    scenario_explosion_setup,
    runner_escape_mode,
    strong_explosion_candidate,
    distribution_score,
    cp_above_vwap,
    cp_above_ema9
):
    try:
        previous_highs = df["High"].iloc[:-1].tail(80)

        resistance_levels = previous_highs[
            previous_highs > cp * 1.003
        ].sort_values()

        nearest_resistance = None
        next_resistance = None
        distance_to_resistance_pct = 999

        if len(resistance_levels) > 0:
            nearest_resistance = float(resistance_levels.iloc[0])
            distance_to_resistance_pct = (
                (nearest_resistance - cp) / cp
            ) * 100

            if len(resistance_levels) > 1:
                next_resistance = float(resistance_levels.iloc[1])

        strong_exception = (
            scenario_explosion_setup
            or runner_escape_mode
            or strong_explosion_candidate
        )

        # 1) Air Space / nearest resistance
        if (
            distance_to_resistance_pct != 999
            and distance_to_resistance_pct < 0.80
            and not real_breakout
            and not strong_exception
        ):
            strong_air_space_exception = (
                instant_rvol >= 3.5
                and volume_acceleration
                and close_position >= 0.78
                and upper_wick_pct <= 0.25
                and move_3m >= 0.45
                and distribution_score < 15
                and cp_above_vwap
                and cp_above_ema9
            )

            if not strong_air_space_exception:
                return {
                    "ok": False,
                    "reason": f"No air space near resistance {distance_to_resistance_pct:.2f}%",
                    "nearest_resistance": nearest_resistance,
                    "next_resistance": next_resistance,
                    "distance_to_resistance_pct": distance_to_resistance_pct
                }

        # 2) Freshness / late move
        if (
            recent_move >= 2.2
            and move_3m < move_5m * 0.55
            and not strong_exception
        ):
            return {
                "ok": False,
                "reason": "Late move / freshness weak",
                "nearest_resistance": nearest_resistance,
                "next_resistance": next_resistance,
                "distance_to_resistance_pct": distance_to_resistance_pct
            }

        # 3) Continuation strength
        if (
            move_5m > 0
            and move_3m < move_5m * 0.50
            and recent_move >= 1.4
            and not strong_exception
        ):
            return {
                "ok": False,
                "reason": "Continuation slowing",
                "nearest_resistance": nearest_resistance,
                "next_resistance": next_resistance,
                "distance_to_resistance_pct": distance_to_resistance_pct
            }

        # 4) Market intent مبسط
        market_intent_score = 0

        if volume_acceleration:
            market_intent_score += 1
        if close_position >= 0.65:
            market_intent_score += 1
        if cp_above_vwap:
            market_intent_score += 1
        if cp_above_ema9:
            market_intent_score += 1
        if vwap_reclaim or ema_reclaim or real_breakout:
            market_intent_score += 1
        if distribution_score < 20:
            market_intent_score += 1

        if (
            market_intent_score < 4
            and not strong_exception
        ):
            return {
                "ok": False,
                "reason": f"Weak market intent score {market_intent_score}",
                "nearest_resistance": nearest_resistance,
                "next_resistance": next_resistance,
                "distance_to_resistance_pct": distance_to_resistance_pct
            }

        return {
            "ok": True,
            "reason": "PASS",
            "nearest_resistance": nearest_resistance,
            "next_resistance": next_resistance,
            "distance_to_resistance_pct": distance_to_resistance_pct
        }

    except Exception as e:
        print(f"Entry quality guard error {symbol}: {e}", flush=True)
        return {
            "ok": True,
            "reason": "GUARD_ERROR_PASS",
            "nearest_resistance": None,
            "next_resistance": None,
            "distance_to_resistance_pct": 999
        }
def breakout_follow_through_confirmed(
    df,
    cp,
    vwap,
    ema9,
    real_breakout,
    scenario_explosion_setup,
    runner_escape_mode,
    move_3m,
    move_5m,
    close_position,
    upper_wick_pct,
    volume_acceleration,
    distribution_score
):
    try:
        last_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])
        prev_high = float(df["High"].iloc[-2])

        last_3_closes = df["Close"].tail(3)
        last_3_lows = df["Low"].tail(3)

        holding_above_breakout = (
            last_close > prev_high
            and prev_close >= prev_high * 0.995
        )

        holding_above_vwap_ema = (
            cp > vwap
            and cp > ema9
            and last_3_closes.min() > vwap * 0.995
            and last_3_closes.min() > ema9 * 0.995
        )

        higher_lows = (
            last_3_lows.iloc[-1] >= last_3_lows.iloc[-2] * 0.995
            and last_3_lows.iloc[-2] >= last_3_lows.iloc[-3] * 0.995
        )

        continuation_ok = (
            move_5m > 0
            and move_3m >= move_5m * 0.55
            and move_3m >= 0.35
        )

        candle_ok = (
            close_position >= 0.65
            and upper_wick_pct <= 0.35
        )

        return (
            volume_acceleration
            and distribution_score < 25
            and candle_ok
            and holding_above_vwap_ema
            and higher_lows
            and continuation_ok
            and (
                holding_above_breakout
                or scenario_explosion_setup
                or runner_escape_mode
                or real_breakout
            )
        )

    except Exception as e:
        print(f"Follow-through check error: {e}", flush=True)
        return False

def get_real_buying_pressure(symbol, cp, df, vwap, ema9, volume_acceleration):
    try:
        quote = api.get_latest_quote(symbol)

        bid_price = float(getattr(quote, "bid_price", 0) or 0)
        ask_price = float(getattr(quote, "ask_price", 0) or 0)
        bid_size = float(getattr(quote, "bid_size", 0) or 0)
        ask_size = float(getattr(quote, "ask_size", 0) or 0)
        print(
            f"📊 {symbol} | "
            f"bid={bid_price} "
            f"ask={ask_price} "
            f"bid_size={bid_size} "
            f"ask_size={ask_size}",
            flush=True
        )

        if bid_price <= 0 or ask_price <= 0:
            return False, 0

        spread_pct = ((ask_price - bid_price) / cp) * 100 if cp > 0 else 999

        bid_ask_strength = (
            bid_size >= ask_size * 1.2
            and spread_pct <= 0.35
        )

        last_close = float(df["Close"].iloc[-1])
        last_open = float(df["Open"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])

        price_pressure = (
            last_close > last_open
            and last_close >= prev_close
            and cp > vwap
            and cp > ema9
        )

        green_candles_5 = (
            df["Close"].tail(5) > df["Open"].tail(5)
        ).sum()

        sustained_buying = green_candles_5 >= 3

        buying_pressure_score = 0

        if bid_ask_strength:
            buying_pressure_score += 2

        if price_pressure:
            buying_pressure_score += 2

        if sustained_buying:
            buying_pressure_score += 1

        if volume_acceleration:
            buying_pressure_score += 1

        real_buying_pressure = buying_pressure_score >= 4

        return real_buying_pressure, buying_pressure_score

    except Exception as e:
        print(f"Buying pressure error {symbol}: {e}", flush=True)
        return False, 0
def ignition_confirmation_ok(
    df,
    cp,
    vwap,
    ema9,
    move_3m,
    move_5m,
    volume_acceleration,
    close_position,
    upper_wick_pct,
    distribution_score
):
    try:
        last_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])
        price_3min_ago = float(df["Close"].iloc[-3])

        last_3_volume = float(df["Volume"].tail(3).mean())
        prev_7_volume = float(df["Volume"].tail(10).head(7).mean())

        price_expanding_now = (
            last_close > prev_close
            and cp > price_3min_ago
            and move_3m >= 0.35
            and move_3m >= move_5m * 0.55
        )

        volume_expanding_now = (
            volume_acceleration
            and last_3_volume >= prev_7_volume * 1.25
        )

        candle_confirms_now = (
            close_position >= 0.72
            and upper_wick_pct <= 0.28
        )

        trend_holding_now = (
            cp > vwap
            and cp > ema9
        )

        clean_enough = (
            distribution_score < 18
        )

        return (
            price_expanding_now
            and volume_expanding_now
            and candle_confirms_now
            and trend_holding_now
            and clean_enough
        )

    except Exception as e:
        print(f"Ignition confirmation error: {e}", flush=True)
        return False

def continuation_quality_ok(
    cp,
    vwap,
    ema9,
    move_3m,
    move_5m,
    instant_rvol,
    volume_acceleration,
    close_position,
    upper_wick_pct,
    distribution_score
):
    try:

        continuation_strength = (
            move_5m > 0
            and move_3m >= move_5m * 0.65
            and move_3m >= 0.40
        )

        structure_ok = (
            cp > vwap
            and cp > ema9
        )

        volume_ok = (
            volume_acceleration
            and instant_rvol >= 2.0
        )

        candle_ok = (
            close_position >= 0.72
            and upper_wick_pct <= 0.28
        )

        clean_ok = (
            distribution_score < 18
        )

        return (
            continuation_strength
            and structure_ok
            and volume_ok
            and candle_ok
            and clean_ok
        )

    except Exception as e:
        print(
            f"Continuation quality error: {e}",
            flush=True
        )

        return False
        
def check_ready_entry(symbol, data, df=None):
    try:
        if df is None:
            df = get_alpaca_bars(symbol, minutes=120)

        if df is None:
            return None

        if df.empty or len(df) < 30 or df["Volume"].mean() == 0:
            return None

        cp = get_latest_price(
            symbol,
            df,
            use_realtime=False
        )

        day_high = float(df["High"].max())
        price_10min_ago = float(df["Close"].iloc[-10])
        price_5min_ago = float(df["Close"].iloc[-5])
        price_3min_ago = float(df["Close"].iloc[-3])

        if (
            cp <= 0
            or day_high <= 0
            or price_10min_ago <= 0
            or price_5min_ago <= 0
            or price_3min_ago <= 0
        ):
            return None

        vwap = float((df["Close"] * df["Volume"]).sum() / df["Volume"].sum())

        df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

        ema9 = float(df["EMA9"].iloc[-1])
        ema20 = float(df["EMA20"].iloc[-1])

        rsi = calculate_rsi(df["Close"])

        strong_explosion_candidate = False

        instant_rvol = df["Volume"].tail(3).mean() / df["Volume"].mean()
        recent_move = ((cp - price_10min_ago) / price_10min_ago) * 100
        move_3m = ((cp - df["Close"].iloc[-3]) / df["Close"].iloc[-3]) * 100
        move_5m = ((cp - df["Close"].iloc[-5]) / df["Close"].iloc[-5]) * 100

        strong_explosion_candidate = (
            instant_rvol >= 5.0
            and (
                move_3m >= 2.0
                or move_5m >= 3.5
            )
            and recent_move >= 1.2
        )
        scenario_explosion_setup = False
        runner_escape_mode = (
            instant_rvol >= 3.5
            and recent_move >= 2.0
            and move_3m >= 0.80
            and move_5m >= 1.20
            and cp > vwap
            and cp > ema9
            and rsi <= 82
        )
        
        move_5m = ((cp - price_5min_ago) / price_5min_ago) * 100
        move_3m = ((cp - price_3min_ago) / price_3min_ago) * 100

        acceleration_ok = (
            move_5m >= 0.45
            and move_3m >= 0.25
            and move_3m >= move_5m * 0.35
        )
        # استبعاد الأسهم الثقيلة بطيئة الاستجابة
        early_momentum_mode = False

        if instant_rvol >= 3 and recent_move < 0.6:

            add_to_momentum_watch(
                symbol,
                cp,
                "RVOL عالي لكن السعر لم يستجب بعد"
            )

            return None
            
        if (
            recent_move >= 1.80
            and not acceleration_ok
            and move_3m < 0.10
            and move_5m < 0.40
        ):
            print(
                f"❌ Rejected weak follow-through: {symbol} | "
                f"10m={recent_move:.2f}% | "
                f"5m={move_5m:.2f}% | "
                f"3m={move_3m:.2f}%",
                flush=True
            )
            return None

        recent_highs = df["High"].tail(10)
        touches = (recent_highs >= day_high * 0.995).sum()

        last_open = float(df["Open"].iloc[-1])
        last_close = float(df["Close"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])

        prev_close = float(df["Close"].iloc[-2])
        prev_high = float(df["High"].iloc[-2])

        candle_range = last_high - last_low
        if candle_range <= 0:
            return None

        upper_wick_pct = (last_high - last_close) / candle_range
        close_position = (last_close - last_low) / candle_range
        body_ratio = abs(last_close - last_open) / candle_range

        last_3_volume = float(df["Volume"].tail(3).mean())
        prev_10_volume = float(df["Volume"].tail(13).head(10).mean())

        volume_acceleration = last_3_volume >= prev_10_volume * 1.6

        real_buying_pressure, buying_pressure_score = get_real_buying_pressure(
            symbol=symbol,
            cp=cp,
            df=df,
            vwap=vwap,
            ema9=ema9,
            volume_acceleration=volume_acceleration
        )

        strong_candle = (
            close_position >= 0.68
            and upper_wick_pct <= 0.30
            and body_ratio >= 0.35
        )

        vwap_reclaim = (
            float(df["Close"].iloc[-2]) < vwap
            and last_close > vwap
        )

        ema_reclaim = (
            float(df["Close"].iloc[-2]) < ema9
            and last_close > ema9
        )

        real_breakout = (
            last_close > prev_high
            and prev_close > prev_high * 0.998
            and instant_rvol >= 2.5
        )

        fake_breakout_risk = (
            upper_wick_pct >= 0.45
            and close_position < 0.55
            and instant_rvol >= 2.5
        )

        repeated_rejection = (
            touches >= 2
            and close_position < 0.60
            and not real_breakout
        )

        distribution_risk = fake_breakout_risk or repeated_rejection

        hidden_dist = detect_hidden_distribution(
            df,
            instant_rvol,
            recent_move,
            real_breakout
        )

        hidden_distribution = hidden_dist["hidden_distribution"]
        distribution_score = hidden_dist["distribution_score"]
        distribution_reasons = hidden_dist["distribution_reasons"]
        if symbol in pending_watchlist:
            update_pending_behavior(
                symbol,
                cp,
                instant_rvol,
                recent_move,
                volume_acceleration,
                strong_candle,
                vwap_reclaim,
                ema_reclaim,
                distribution_score
            )

            pending_score = float(
                pending_watchlist[symbol].get("pending_score", 50)
            )
            pending = pending_watchlist.get(symbol, {})

            if (
                pending_score >= 75
                and int(pending.get("improve_count", 0)) >= 2
                and int(pending.get("weak_count", 0)) == 0
                and not pending.get("early_alert_sent", False)
            ):
                early_momentum_mode = True
                pending["early_alert_sent"] = True
                pending_watchlist[symbol] = pending

            if pending_score < 40:
                print(f"🧹 Removed weak pending: {symbol}", flush=True)
                pending_watchlist.pop(symbol, None)
                return None

        overextended = (
            rsi > 75
            or recent_move > 3.2
            or touches >= 3
        )
        # =========================
        # ENTRY STAGE CLASSIFICATION
        # =========================

        if (
            recent_move <= 1.0
            and volume_acceleration
            and (vwap_reclaim or ema_reclaim)
        ):

            entry_stage = (
                "🟡 EARLY WAKE-UP - بداية دخول سيولة"
            )

        elif (
            real_breakout
            and recent_move <= 1.8
        ):

            entry_stage = (
                "🟢 EARLY BREAKOUT - اختراق مبكر صحي"
            )

        elif (
            real_breakout
            and 1.8 < recent_move <= 3.0
            and not overextended
        ):

            entry_stage = (
                "🔥 MOMENTUM RUNNING - الزخم مستمر"
            )

        elif (
            recent_move > 3.0
            or rsi >= 73
            or touches >= 3
        ):

            entry_stage = (
                "⚠️ LATE ENTRY RISK - الدخول متأخر نسبيًا"
            )

        else:

            entry_stage = (
                "🟢 CONFIRMED ENTRY - دخول مؤكد"
        )
        if (
            "LATE ENTRY RISK" in entry_stage
            and not strong_explosion_candidate
            and not runner_escape_mode
        ):
            print(
                f"❌ Rejected late entry risk: {symbol}",
                flush=True
            )
            return None

        if (
            distribution_score >= 25
            and not strong_candle
            and not strong_explosion_candidate
            and not runner_escape_mode
        ):
            print(
                f"❌ Rejected weak candle with distribution: {symbol}",
                flush=True
            )
            return None

        if (
            "MOMENTUM RUNNING" in entry_stage
            and distribution_score >= 15
            and not volume_acceleration
            and not vwap_reclaim
            and not ema_reclaim
            and not strong_explosion_candidate
            and not runner_escape_mode
        ):
            print(
                f"❌ Rejected late running momentum with distribution: {symbol}",
                flush=True
            )
            return None
            
        if (
            recent_move >= 3.0
            and move_5m < 0.50
            and move_3m < 0.10
            and distribution_score >= 20
        ):
            print(
                f"❌ Momentum cooling after spike: {symbol}",
                flush=True
            )
            return None

        fresh_acceleration = (
            move_3m >= move_5m * 0.60
        )

        if (
            not fresh_acceleration
            and recent_move >= 2.50
            and distribution_score >= 20
            and not strong_explosion_candidate
            and not runner_escape_mode
        ):
            print(
                f"❌ Weak fresh acceleration: {symbol}",
                flush=True
            )
            return None
            
        if (
            close_position < 0.55
            and recent_move >= 2.0
        ):
            print(
                f"❌ Weak close after move: {symbol}",
                flush=True
            )
            return None
        if (
            recent_move >= 3.0
            and move_3m < 0
            and move_5m < recent_move * 0.35
            and distribution_score >= 20
        ):
            print(
                f"❌ Momentum fading: {symbol}",
                flush=True
            )
            return None

        # منع الأسهم التي تملك سيولة قوية لكن السعر لم يعد يستجيب

        if (
            instant_rvol >= 5
            and recent_move < 1.0
            and move_3m < 0.15
            and move_5m < 0.35
        ):
            add_to_momentum_watch(
                symbol,
                cp,
                "RVOL عالي لكن السعر يحتاج وقت للتأكيد"
            )

            print(
                f"🟠 High RVOL weak response moved to momentum watch: {symbol}",
                flush=True
            )
            return None


        # منع ارتفاع RVOL مع ضعف التقدم السعري

        if (
            instant_rvol >= 5
            and recent_move < 1.8
        ):
            print(
                f"❌ High RVOL weak response: {symbol}",
                flush=True
            )
            return None


        # سيناريو انفجار محتمل

        scenario_explosion_setup = (
            instant_rvol >= 3.0
            and recent_move >= 1.0
            and move_3m >= 0.45
            and move_5m >= 0.80
            and move_3m >= move_5m * 0.65
            and cp > vwap
            and cp > ema9
            and close_position >= 0.72
            and distribution_score < 25
            and rsi <= 78
            and volume_acceleration
            and strong_candle
        )

        ignition_quality = (
            volume_acceleration
            and close_position >= 0.78
            and upper_wick_pct <= 0.25
            and distribution_score < 18
            and (
                scenario_explosion_setup
                or runner_escape_mode
                or (
                    real_breakout
                    and move_3m >= 0.45
                    and move_5m >= 0.75
                    and move_3m >= move_5m * 0.60
                )
            )
        )

        if not ignition_quality:
            add_to_pending(
                symbol,
                cp,
                "فرصة جيدة لكن جودة الدخول الآن غير كافية"
            )

            print(
                f"🟡 Good setup but weak entry quality: {symbol}",
                flush=True
            )

            return None

        follow_through_ok = breakout_follow_through_confirmed(
            df=df,
            cp=cp,
            vwap=vwap,
            ema9=ema9,
            real_breakout=real_breakout,
            scenario_explosion_setup=scenario_explosion_setup,
            runner_escape_mode=runner_escape_mode,
            move_3m=move_3m,
            move_5m=move_5m,
            close_position=close_position,
            upper_wick_pct=upper_wick_pct,
            volume_acceleration=volume_acceleration,
            distribution_score=distribution_score
        )

        if not follow_through_ok:
            add_to_pending(
                symbol,
                cp,
                "اختراق جيد لكن Follow-Through غير مؤكد بعد"
            )

            print(
                f"🟡 Breakout needs follow-through confirmation: {symbol}",
                flush=True
            )

            return None

        if not real_buying_pressure:
            add_to_pending(
                symbol,
                cp,
                "الاختراق جيد لكن ضغط الشراء غير كافي"
            )

            print(
                f"🟡 Breakout ok but buying pressure weak: {symbol} | score={buying_pressure_score}",
                flush=True
            )

            return None

        ignition_confirmed = ignition_confirmation_ok(
            df=df,
            cp=cp,
            vwap=vwap,
            ema9=ema9,
            move_3m=move_3m,
            move_5m=move_5m,
            volume_acceleration=volume_acceleration,
            close_position=close_position,
            upper_wick_pct=upper_wick_pct,
            distribution_score=distribution_score
        )

        if not ignition_confirmed:
            add_to_pending(
                symbol,
                cp,
                "السهم جيد لكن الانطلاق الفعلي غير مؤكد الآن"
            )

            print(
                f"🟡 Ignition not confirmed yet: {symbol}",
                flush=True
            )

            return None

        continuation_ok = continuation_quality_ok(
            cp=cp,
            vwap=vwap,
            ema9=ema9,
            move_3m=move_3m,
            move_5m=move_5m,
            instant_rvol=instant_rvol,
            volume_acceleration=volume_acceleration,
            close_position=close_position,
            upper_wick_pct=upper_wick_pct,
            distribution_score=distribution_score
        )

        if not continuation_ok:
            add_to_pending(
                symbol,
                cp,
                "الانطلاق بدأ لكن الاستمرار غير مؤكد بعد"
            )

            print(
                f"🟡 Continuation not confirmed: {symbol}",
                flush=True
            )

            return None

        recent_resistance = float(df["High"].tail(80).max())

        air_space_pct = (
            (recent_resistance - cp) / cp
        ) * 100

        near_resistance_pressure_required = (
            recent_resistance > cp
            and air_space_pct < 0.80
        )

        if near_resistance_pressure_required:
            strong_pressure_near_resistance = (
                buying_pressure_score >= 5
                and follow_through_ok
                and volume_acceleration
                and close_position >= 0.78
                and upper_wick_pct <= 0.25
                and move_3m >= 0.45
                and distribution_score < 15
            )

            if not strong_pressure_near_resistance:
                add_to_pending(
                    symbol,
                    cp,
                    f"مقاومة قريبة جدًا وتحتاج تأكيد أقوى ({air_space_pct:.2f}%)"
                )

                print(
                    f"🟡 Near resistance needs stronger pressure: {symbol} | air={air_space_pct:.2f}% | pressure={buying_pressure_score}",
                    flush=True
                )

                return None
    
        quality_guard = entry_quality_guard(
            symbol=symbol,
            df=df,
            cp=cp,
            recent_move=recent_move,
            move_3m=move_3m,
            move_5m=move_5m,
            instant_rvol=instant_rvol,
            volume_acceleration=volume_acceleration,
            close_position=close_position,
            upper_wick_pct=upper_wick_pct,
            vwap_reclaim=vwap_reclaim,
            ema_reclaim=ema_reclaim,
            real_breakout=real_breakout,
            scenario_explosion_setup=scenario_explosion_setup,
            runner_escape_mode=runner_escape_mode,
            strong_explosion_candidate=strong_explosion_candidate,
            distribution_score=distribution_score,
            cp_above_vwap=cp > vwap,
            cp_above_ema9=cp > ema9
        )

        if not quality_guard.get("ok", True):
            print(
                f"❌ Entry quality rejected: {symbol} | {quality_guard.get('reason')}",
                flush=True
            )
            return None

        nearest_resistance = quality_guard.get("nearest_resistance")
        next_resistance = quality_guard.get("next_resistance")
        distance_to_resistance_pct = quality_guard.get("distance_to_resistance_pct", 999)


        # منع Runner Escape المتأخر أو الضعيف

        if (
            runner_escape_mode
            and (
                "LATE ENTRY RISK" in entry_stage
                or not strong_candle
                or not real_breakout
            )
        ):
            print(
                f"❌ Rejected weak Runner Escape: {symbol}",
                flush=True
            )
            return None


        # منع التنبيه بعد شمعة انفجار كبيرة خلال آخر 3 دقائق

        if (
            move_3m >= 6.0
            and recent_move >= 1.5
            and not scenario_explosion_setup
        ):
            print(
                f"❌ Too late after 3m spike: {symbol}",
                flush=True
            )
            return None

        # =========================
        # منع الاختراقات البطيئة / المملة
        # =========================

        if (
            not scenario_explosion_setup
            and not runner_escape_mode
            and not strong_explosion_candidate
            and recent_move < 1.20
            and move_3m < 0.80
        ):
            print(
                f"❌ Slow breakout rejected: {symbol}",
                flush=True
            )
            return None


        # =========================
        # منع الزخم الذي لا يتسارع الآن
        # =========================

        if (
            not scenario_explosion_setup
            and not runner_escape_mode
            and not strong_explosion_candidate
            and move_5m > 0
            and move_3m < move_5m * 0.50
            and distribution_score >= 20
            and recent_move >= 1.80
        ):
            print(
                f"❌ Not enough current acceleration: {symbol}",
                flush=True
            )
            return None

        # =================================
        # رفض الأسهم البطيئة الثقيلة
        # =================================

        slow_grind = (
            instant_rvol >= 2.5
            and recent_move <= 2
            and move_3m <= 1.5
            and close_position < 0.85
        )

        if (
            slow_grind
            and not scenario_explosion_setup
            and not runner_escape_mode
            and not strong_explosion_candidate
        ):
            print(
                f"❌ Slow grind / weak expansion: {symbol}",
                flush=True
            )
            return None

        if not vwap_reclaim and not ema_reclaim and not real_breakout:
            print(f"❌ Weak entry rejected: no VWAP/EMA reclaim and no real breakout: {symbol}", flush=True)
            return None

        if (
            recent_move >= 1.2
            and move_3m <= move_5m * 0.75
            and not strong_explosion_candidate
        ):
            print(
                f"❌ Momentum slowing down: {symbol}",
                flush=True
            )
            return None

        ready_to_alert = (
            real_breakout
            or runner_escape_mode
            or scenario_explosion_setup
            or (
                instant_rvol >= 2.5
                and recent_move >= 0.65
                and 50 <= rsi <= 70
                and cp > vwap
                and cp > ema9
                and ema9 >= ema20 * 0.995
                and volume_acceleration
                and strong_candle
                and close_position >= 0.70
                and upper_wick_pct <= 0.30
                and (vwap_reclaim or ema_reclaim)
                and not fake_breakout_risk
            )
        )

        advanced_entry = (
            cp > vwap
            and cp > ema9
            and ema9 >= ema20 * 0.995
            and touches < 3
            and not overextended
            and not (distribution_risk and not real_breakout)
            and not (hidden_distribution and not real_breakout)
            and ready_to_alert
        )
        # =========================
        # ROUTE RISKY EARLY WAKE-UP TO PENDING
        # =========================

        if (
            "EARLY WAKE-UP" in entry_stage
            and distribution_score >= 35
            and not real_breakout
        ):

            add_to_pending(
                symbol,
                cp,
                "استيقاظ مبكر لكن عليه ملاحظات تصريف - يحتاج تأكيد"
            )

            return None

        # =========================
        # FILTER WEAK EARLY WAKE-UP
        # =========================

        if (
            "EARLY WAKE-UP" in entry_stage
            and (
                instant_rvol < 3.0
                or not volume_acceleration
                or not strong_candle
            )
            and not real_breakout
        ):

            add_to_pending(
                symbol,
                cp,
                "استيقاظ مبكر ضعيف - تحت المراقبة"
            )

            return None

        # =========================
        # ROUTE WEAK EARLY BREAKOUT TO PENDING
        # =========================

        if (
            "EARLY BREAKOUT" in entry_stage
            and (
                not strong_candle or (
                not vwap_reclaim
                and not ema_reclaim
                )
            )
            and distribution_score >= 15
        ):

            add_to_pending(
                symbol,
                cp,
                "اختراق مبكر لكن التأكيد غير مكتمل"
            )

            return None
        # =========================
        # ROUTE HIGH DISTRIBUTION BREAKOUT TO PENDING
        # =========================

        if (
            distribution_score >= 40
            and (
                "EARLY" in entry_stage
                or recent_move < 1.0
                or not vwap_reclaim
            )
        ):

            add_to_pending(
                symbol,
                cp,
                "اختراق مع تصريف مرتفع - يحتاج تأكيد قبل الدخول"
            )

            return None 
            
        if not advanced_entry:
            return None

        if sent_alerts.get(symbol):
            return None

        bot2_score = float(data.get("bot2_score", 0) or 0)
        bot2_grade = data.get("bot2_grade", "")

        bot2_bonus = 0
        if bot2_grade == "A++":
            bot2_bonus = 15
        elif bot2_grade == "A+":
            bot2_bonus = 10
        elif bot2_grade == "A":
            bot2_bonus = 7
        elif "Bot 2" in data.get("source", ""):
            bot2_bonus = 5

        technical_score = 0
        technical_score += min(instant_rvol * 8, 20)
        price_response_score = recent_move * 12

        technical_score += min(price_response_score, 30)

        # =========================
        # REAL PRICE EXPLOSION RESPONSE
        # =========================

        if recent_move >= 2.0:

            technical_score += min(recent_move * 10, 25)

        elif recent_move >= 1.2:

            technical_score += min(recent_move * 7, 14)

        elif recent_move >= 0.7:

            technical_score += min(recent_move * 4, 8)

        else:

            technical_score -= 12

        if cp > vwap:
            technical_score += 10
        if cp > ema9:
            technical_score += 10
        if ema9 >= ema20 * 0.995:
            technical_score += 8
        if real_breakout:
            technical_score += 15
        if volume_acceleration:
            technical_score += 8
        if strong_candle:
            technical_score += 8
        if vwap_reclaim:
            technical_score += 6
        if ema_reclaim:
            technical_score += 6
        if 52 <= rsi <= 68:
            technical_score += 10
        elif 50 <= rsi < 52:
            technical_score += 5

        if fake_breakout_risk:
            technical_score -= 15

        distribution_penalty = min(distribution_score, 30)
        technical_score -= distribution_penalty

        final_score = technical_score + bot2_bonus
        grade = grade_from_score(final_score)
        strong_explosion_candidate = (
            instant_rvol >= 3.5
            and recent_move >= 1.2
            and move_5m >= 0.60
            and move_3m >= 0.30
            and acceleration_ok
            and strong_candle
            and cp >= day_high * 0.985
            and distribution_score < 25
        )

        if grade not in ["A", "A+", "A++"]:
            return None

        entry = cp

        # =========================
        # SMART TARGETS
        # =========================

        if entry < 2:

            t1 = entry + 0.04
            t2 = entry + 0.08
            t3 = entry + 0.12

        elif entry < 5:

            t1 = entry + 0.07
            t2 = entry + 0.12
            t3 = entry + 0.18

        else:

            t1 = entry * 1.02
            t2 = entry * 1.04
            t3 = entry * 1.06

        sl = entry * 0.985



        if not can_send_trade_alerts():
            print(
                f"🔕 Bot 3 alert muted by schedule: {symbol} | {grade}",
                flush=True
            )

            if symbol in watchlist:
                watchlist[symbol]["alerted"] = True

            return None

        dist_reasons_text = (
            ", ".join(distribution_reasons[:3])
            if distribution_reasons else "None"
        )
        if runner_escape_mode:
            signal_type = "🔥 RUNNER ESCAPE - انفجار سريع"

        elif scenario_explosion_setup:
            signal_type = "🎯 SCENARIO ALERT - سيناريو انفجار محتمل"

        elif early_momentum_mode:
            signal_type = "🟡 EARLY MOMENTUM - دخول مبكر"

        else:
            signal_type = "✅ دخول مؤكد"
        timing_text = estimate_target_timing(
        signal_type,
        instant_rvol,
        recent_move,
        move_3m,
        move_5m,
        acceleration_ok,
        scenario_explosion_setup,
        runner_escape_mode,
        early_momentum_mode
        ) 
        
        if data.get("source") == "LIVE_MOVERS":
            source_text = "📡 رصد حي مباشر"

        elif "Bot 2" in data.get("source", ""):
            source_text = "🟢 إشارة من Bot 2"

        else:
            source_text = "🛟 فحص احتياطي"

        setup_strength = classify_setup_strength({
            "source": "LIVE_MOVERS" if data.get("source") == "LIVE_MOVERS" else data.get("source", ""),
            "final_score": final_score,
            "instant_rvol": instant_rvol,
            "move_3m": move_3m,
            "close_position": close_position,
            "distribution_score": distribution_score,
            "real_breakout": real_breakout,
            "volume_acceleration": volume_acceleration
        })
        source_group = data.get("source_group", "UNKNOWN")
        historical_time_text = ""

        if TIME_MACHINE_MODE:
            try:
                historical_time_text = (
                    f"🕰️ وقت التنبيه التاريخي: "
                    f"{TIME_MACHINE_END_NY.strftime('%Y-%m-%d %H:%M NY')}\n\n"
                )
            except Exception:
                historical_time_text = (
                    "🕰️ وقت التنبيه التاريخي: غير متاح\n\n"
                )
                
        msg = (
            f"🧠🔥 *Bot 3 - قرار دخول نهائي*\n\n"
            f"{historical_time_text}"
            f"🎫 السهم: `{symbol}`\n"
            f"💰 السعر: {entry:.2f}\n"
            f"🏆 التصنيف: {grade}\n\n"
            f"💎 قوة الفرصة: {setup_strength}\n\n"
            f"{signal_type}\n\n"
            f"{timing_text}\n\n"
            f"{'🔥 Strong Explosion Candidate - مرشح انفجار قوي جداً\\n\\n' if strong_explosion_candidate else ''}"
            f"📍 مرحلة الدخول: {entry_stage}\n"
            f"📡 المصدر:\n"
            f"📡 المصدر: {source_group}\n"
            f"{source_text}\n\n"
            f"📊 السكور:\n"
            f"Final Score: {final_score:.1f}\n"
            f"Technical Score: {technical_score:.1f}\n"
            f"Bot2 Bonus: {bot2_bonus}\n"
            f"خصم التصريف: {distribution_penalty}\n\n"
            f"📊 القوة:\n"
            f"RSI: {rsi:.1f}\n"
            f"RVOL: {instant_rvol:.2f}x\n"
            f"حركة 10د: {recent_move:.2f}%\n"
            f"حركة 5د: {move_5m:.2f}%\n"
            f"حركة 3د: {move_3m:.2f}%\n"
            f"Acceleration OK: {acceleration_ok}\n\n"
            f"🧪 تأكيد الدخول:\n"
            f"Real Breakout: {real_breakout}\n"
            f"Volume Acceleration: {volume_acceleration}\n"
            f"Strong Candle: {strong_candle}\n"
            f"VWAP Reclaim: {vwap_reclaim}\n"
            f"EMA Reclaim: {ema_reclaim}\n"
            f"Ready To Alert: {ready_to_alert}\n"
            f"Ignition Confirmed: {ignition_confirmed}\n"
            f"تصريف مخفي: {'نعم' if hidden_distribution else 'لا'}\n"
            f"درجة التصريف: {distribution_score}\n"
            f"ملاحظات التصريف: {dist_reasons_text}\n\n"
            f"🛡️ فلتر التصريف: تم تجاوزه ✅\n\n"
            f"🚀 دخول الآن: {entry:.2f}\n"
            f"🎯 هدف 1: {t1:.2f}\n"
            f"🚀 هدف 2: {t2:.2f}\n"
            f"🔥 هدف 3: {t3:.2f}\n"
            f"🛑 وقف الخسارة: {sl:.2f}\n\n"
            f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
        )

        send_telegram_msg(
            msg,
            TELEGRAM_BOT3_CHAT_ID
        )
        momentum_watchlist[symbol] = {
            "symbol": symbol,
            "entry": entry,
            "last_price": entry,
            "best_price": entry,
            "checks": 0,
            "started_at": time.time(),
            "last_update": time.time(),
            "last_status": "POST_ALERT"
        }

        sent_alerts[symbol] = {
            "time": time.time(),
            "grade": grade
        }

        active_trades[symbol] = {
        "entry": entry,
        "entry_time": time.time(),
        "signal_type": "SCENARIO_ALERT" if scenario_explosion_setup else signal_type,
            "t1": t1,
            "t2": t2,
            "t3": t3,
            "sl": sl,
            "grade": grade,
            "time": time.time(),
            "slow_alerted": False,
            "stop_alerted": False,
            "target1_alerted": False,
            "target2_alerted": False,
            "target3_alerted": False
        }
        if strong_explosion_candidate:

            explosion_tracking[symbol] = {
                "entry": entry,
                "start_time": time.time(),
                "last_update": time.time(),
                "last_status": "STARTED"
            }

            print(
                f"🔥 Explosion tracking started: {symbol}",
                flush=True
            )

        if symbol in watchlist:
            watchlist[symbol]["alerted"] = True

        print(f"🧠 BOT 3 ENTRY SENT: {symbol} | {grade}", flush=True)

    except Exception as e:
        print(f"Check entry error {symbol}: {e}", flush=True)


def get_trade_age_minutes(trade):
    trade_time = trade.get("time", time.time())

    try:
        return (time.time() - float(trade_time)) / 60
    except Exception:
        return 0

def monitor_momentum_watchlist():

    global momentum_watchlist

    for symbol, data in list(momentum_watchlist.items()):

        try:
            df = get_alpaca_bars(symbol, minutes=30)

            if df.empty or len(df) < 10:
                continue

            cp = get_latest_price(symbol, df)

            vwap = float(
                (df["Close"] * df["Volume"]).sum()
                / df["Volume"].sum()
            )

            df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
            ema9 = float(df["EMA9"].iloc[-1])

            instant_rvol = (
                df["Volume"].tail(3).mean()
                / max(df["Volume"].mean(), 1)
            )

            last_close = float(df["Close"].iloc[-1])
            last_high = float(df["High"].iloc[-1])
            last_low = float(df["Low"].iloc[-1])

            candle_range = last_high - last_low

            if candle_range <= 0:
                continue

            close_position = (last_close - last_low) / candle_range
            upper_wick_pct = (last_high - last_close) / candle_range

            higher_low = (
                df["Low"].iloc[-1] > df["Low"].iloc[-3]
            )

            confirmed = (
                cp > vwap
                and cp > ema9
                and instant_rvol >= 2.0
                and close_position >= 0.60
                and upper_wick_pct <= 0.40
                and higher_low
            )

            weak = (
                cp < vwap
                or cp < ema9
                or instant_rvol < 1.3
                or upper_wick_pct >= 0.55
            )

            data["checks"] = int(data.get("checks", 0)) + 1
            data["last_price"] = float(cp)
            data["best_price"] = max(float(data.get("best_price", cp)), float(cp))
            data["last_update"] = time.time()

            if confirmed and data["checks"] >= 2:
                add_to_watchlist(
                    symbol,
                    "Momentum Confirmed بعد مراقبة 3-10 دقائق",
                    cp
                )

                momentum_watchlist.pop(symbol, None)

                print(
                    f"✅ Momentum confirmed: {symbol}",
                    flush=True
                )

                continue

            if weak or data["checks"] >= 6:
                momentum_watchlist.pop(symbol, None)

                print(
                    f"❌ Momentum watch removed: {symbol}",
                    flush=True
                )

                continue

            momentum_watchlist[symbol] = data

        except Exception as e:
            print(
                f"Momentum watch error {symbol}: {e}",
                flush=True
            )
def monitor_active_trades():
    global active_trades

    for symbol, trade in list(active_trades.items()):
        try:
            df = get_alpaca_bars(symbol, minutes=30)

            if df.empty or len(df) < 10:
                continue

            cp = get_latest_price(symbol, df)

            entry = float(trade["entry"])
            sl = float(trade["sl"])
            t1 = float(trade["t1"])
            t2 = float(trade["t2"])
            t3 = float(trade.get("t3", entry * 1.07))

            gain_pct = ((cp - entry) / entry) * 100
            age_minutes = get_trade_age_minutes(trade)
            move_3m = 0

            try:
                price_3min_ago = float(df["Close"].iloc[-3])

                if price_3min_ago > 0:
                    move_3m = (
                        (cp - price_3min_ago)
                        / price_3min_ago
                    ) * 100

            except Exception:
                move_3m = 0
            
            # =========================
            # REMOVE OLD TRADES AFTER 3 DAYS
            # =========================

            if age_minutes >= 4320:
                print(
                    f"🧹 Removed old active trade after 3 days: {symbol}",
                    flush=True
                )

                active_trades.pop(symbol, None)
                continue
                
            vwap = float((df["Close"] * df["Volume"]).sum() / df["Volume"].sum())

            df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
            ema9 = float(df["EMA9"].iloc[-1])

            rsi = calculate_rsi(df["Close"])
            instant_rvol = df["Volume"].tail(3).mean() / max(df["Volume"].mean(), 1)

            last_close = float(df["Close"].iloc[-1])
            last_high = float(df["High"].iloc[-1])
            last_low = float(df["Low"].iloc[-1])

            candle_range = last_high - last_low
            close_position = ((last_close - last_low) / candle_range) if candle_range > 0 else 0.5
            upper_wick_pct = ((last_high - last_close) / candle_range) if candle_range > 0 else 0.5
            
            # =================================
            # Scenario Explosion Failed
            # =================================

            if trade.get("signal_type") == "SCENARIO_ALERT":

                entry_time = trade.get(
                    "entry_time",
                    trade.get("time", time.time())
                )

                minutes_alive = (
                    time.time() - float(entry_time)
                ) / 60

                failed_scenario = (
                    minutes_alive >= 10
                    and gain_pct < 1.0
                    and (
                        cp < vwap
                        or cp < ema9
                        or move_3m < 0.30
                    )
                )

                if (
                    failed_scenario
                    and not trade.get("scenario_failed_alert")
                ):

                    if can_send_trade_alerts():
                        msg = (
                            f"⚠️ *Bot 3 - فشل سيناريو الانفجار*\n\n"
                            f"🎫 السهم: `{symbol}`\n"
                            f"💰 السعر الحالي: {cp:.2f}\n"
                            f"🚀 الدخول: {entry:.2f}\n"
                            f"📊 الربح الحالي: {gain_pct:.2f}%\n"
                            f"⏱️ مدة المتابعة: {minutes_alive:.0f} دقيقة\n\n"
                            f"❌ السهم لم يؤكد الانفجار بعد 10 دقائق\n"
                            f"⚠️ يفضل تشديد الوقف أو الخروج حسب الشارت"
                        )

                        send_telegram_msg(
                            msg,
                            TELEGRAM_BOT3_CHAT_ID
                        )

                    trade["scenario_failed_alert"] = True

            strong_momentum_after_target = (
                cp > vwap
                and cp > ema9
                and instant_rvol >= 1.8
                and 50 <= rsi <= 78
                and close_position >= 0.55
                and upper_wick_pct <= 0.45
            )

            # =========================
            # SMART TRADE FOLLOW-UP
            # =========================
            if age_minutes >= 30 and gain_pct < 0.5 and not trade.get("slow_alerted", False):

                weak_trade = (
                    cp < vwap
                    or cp < ema9
                    or instant_rvol < 1.2
                    or close_position < 0.45
                    or upper_wick_pct > 0.50
                )

                healthy_but_slow = (
                    cp > vwap
                    and cp > ema9
                    and instant_rvol >= 1.5
                    and close_position >= 0.55
                )

                if can_send_trade_alerts():

                    # =========================
                    # HEALTHY BUT SLOW
                    # =========================
                    if healthy_but_slow:

                        msg = (
                            f"🟡 *Bot 3 - السهم هادئ لكن صحي*\n\n"
                            f"🎫 السهم: `{symbol}`\n"
                            f"💰 السعر الحالي: {cp:.2f}\n"
                            f"🚀 الدخول: {entry:.2f}\n"
                            f"📊 الحركة الحالية: {gain_pct:.2f}%\n\n"
                            f"✅ السعر فوق VWAP\n"
                            f"✅ السعر فوق EMA9\n"
                            f"✅ السيولة ما زالت إيجابية\n\n"
                            f"🧠 السهم لم ينطلق بقوة بعد،"
                            f" لكن الزخم لا يزال جيدًا."
                        )

                    # =========================
                    # WEAK TRADE
                    # =========================
                    elif weak_trade:

                        msg = (
                            f"⚠️ *Bot 3 - ضعف واضح بعد الدخول*\n\n"
                            f"🎫 السهم: `{symbol}`\n"
                            f"💰 السعر الحالي: {cp:.2f}\n"
                            f"🚀 الدخول: {entry:.2f}\n"
                            f"📊 الحركة الحالية: {gain_pct:.2f}%\n\n"
                            f"❌ ضعف في الزخم أو السيولة\n"
                            f"❌ السهم بدأ يفقد القوة\n\n"
                            f"يفضل:\n"
                            f"• تشديد الوقف\n"
                            f"أو\n"
                            f"• الخروج الجزئي/الكامل حسب الشارت"
                        )

                    # =========================
                    # NORMAL SLOW MESSAGE
                    # =========================
                    else:

                        msg = (
                            f"⚠️ *Bot 3 - متابعة الصفقة*\n\n"
                            f"🎫 السهم: `{symbol}`\n"
                            f"💰 السعر الحالي: {cp:.2f}\n"
                            f"🚀 الدخول: {entry:.2f}\n"
                            f"📊 الحركة الحالية: {gain_pct:.2f}%\n\n"
                            f"السهم لم يتحرك بقوة حتى الآن."
                        )

                    send_telegram_msg(
                        msg,
                        TELEGRAM_BOT3_CHAT_ID
                    )

                trade["slow_alerted"] = True

            if cp >= t1 and not trade.get("target1_alerted", False):
                new_sl = max(entry, cp * 0.985)
                trade["sl"] = round(new_sl, 4)

                if can_send_trade_alerts():
                    msg = (
                        f"🎯 *Bot 3 - وصل هدف 1*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"🎯 هدف 1: {t1:.2f}\n"
                        f"📈 الربح الحالي: {gain_pct:.2f}%\n\n"
                        f"✅ الوقف المقترح الآن: {new_sl:.2f}\n"
                        f"📌 الهدف التالي: {t2:.2f}"
                    )
                    send_telegram_msg(
                        msg,
                        TELEGRAM_BOT3_CHAT_ID
                    )

                trade["target1_alerted"] = True

            if cp >= t2 and not trade.get("target2_alerted", False):
                new_sl = max(t1, cp * 0.985)
                trade["sl"] = round(new_sl, 4)

                if strong_momentum_after_target:
                    action_text = "🔥 الزخم والسيولة ما زالت جيدة، ممكن الاستمرار مع رفع الوقف."
                else:
                    action_text = "⚠️ الزخم هدأ، يفضل جني جزء من الربح أو تشديد الوقف."

                if can_send_trade_alerts():
                    msg = (
                        f"🚀 *Bot 3 - وصل هدف 2*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"🎯 هدف 2: {t2:.2f}\n"
                        f"📈 الربح الحالي: {gain_pct:.2f}%\n\n"
                        f"✅ الوقف المقترح الآن: {new_sl:.2f}\n"
                        f"🔥 هدف 3: {t3:.2f}\n\n"
                        f"{action_text}"
                    )
                    send_telegram_msg(
                        msg,
                        TELEGRAM_BOT3_CHAT_ID
                    )

                trade["target2_alerted"] = True

            if cp >= t3 and not trade.get("target3_alerted", False):
                new_sl = max(t2, cp * 0.985)
                trade["sl"] = round(new_sl, 4)

                if strong_momentum_after_target:
                    action_text = (
                        "🔥 السهم ما زال قويًا بعد هدف 3\n"
                        "✅ السيولة والزخم مستمران\n"
                        f"📌 لمن يريد الاستمرار: الوقف المقترح {new_sl:.2f}\n"
                        "⚠️ سيتم حذف الصفقة من مراقبة البوت بعد هذا التنبيه"
                    )

                elif (
                    cp > vwap
                    and cp > ema9
                    and instant_rvol >= 1.3
                    and close_position >= 0.50
                ):
                    action_text = (
                        "🟡 السهم هادئ بعد هدف 3\n"
                        "✅ لا يوجد ضعف قوي حتى الآن\n"
                        f"📌 الوقف المقترح لحماية الربح: {new_sl:.2f}\n"
                        "⚠️ سيتم حذف الصفقة من مراقبة البوت بعد هذا التنبيه"
                    )

                else:
                    action_text = (
                        "⚠️ الزخم بدأ يضعف بعد هدف 3\n"
                        "❌ السيولة أو حركة السعر لم تعد قوية\n"
                        "✅ يفضل جني الربح أو الخروج\n"
                        "⚠️ سيتم حذف الصفقة من مراقبة البوت بعد هذا التنبيه"
                    )

                if can_send_trade_alerts():
                    msg = (
                        f"🔥 *Bot 3 - وصل هدف 3 / نهاية المتابعة*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"🔥 هدف 3: {t3:.2f}\n"
                        f"📈 الربح الحالي: {gain_pct:.2f}%\n\n"
                        f"{action_text}"
                    )

                    send_telegram_msg(
                        msg,
                        TELEGRAM_BOT3_CHAT_ID
                    )

                active_trades.pop(symbol, None)
                explosion_tracking.pop(symbol, None)

                print(
                    f"✅ Bot 3 trade completed and removed after Target 3: {symbol}",
                    flush=True
                )

                continue
                
            active_trades[symbol] = trade

        except Exception as e:
            print(f"Monitor trade error {symbol}: {e}", flush=True)
            continue

def monitor_explosion_tracking():

    global explosion_tracking

    for symbol, data in list(explosion_tracking.items()):

        try:
            df = get_alpaca_bars(symbol, minutes=30)

            if df.empty or len(df) < 10:
                continue

            cp = get_latest_price(symbol, df)
            entry = float(data["entry"])

            gain_pct = ((cp - entry) / entry) * 100

            vwap = float(
                (df["Close"] * df["Volume"]).sum()
                / df["Volume"].sum()
            )

            df["EMA9"] = (
                df["Close"]
                .ewm(span=9, adjust=False)
                .mean()
            )

            ema9 = float(df["EMA9"].iloc[-1])
            rsi = calculate_rsi(df["Close"])

            instant_rvol = (
                df["Volume"].tail(3).mean()
                / max(df["Volume"].mean(), 1)
            )

            last_close = float(df["Close"].iloc[-1])
            last_high = float(df["High"].iloc[-1])
            last_low = float(df["Low"].iloc[-1])

            candle_range = last_high - last_low

            if candle_range <= 0:
                continue

            close_position = (last_close - last_low) / candle_range

            strong_continuation = (
                cp > vwap
                and cp > ema9
                and instant_rvol >= 2.0
                and close_position >= 0.60
                and rsi <= 82
            )

            weak_behavior = (
                cp < vwap
                or instant_rvol < 1.3
                or close_position < 0.45
            )

            now_ts = time.time()

            if (now_ts - data.get("last_update", 0)) < 180:
                continue

            if strong_continuation:

                msg = (
                    f"🚀 *Explosion Update*\n\n"
                    f"🎫 `{symbol}`\n"
                    f"📈 الربح الحالي: {gain_pct:.2f}%\n\n"
                    f"🔥 الزخم ما زال قوي\n"
                    f"✅ فوق VWAP\n"
                    f"✅ السيولة مستمرة\n"
                    f"✅ احتمال استمرار الصعود قائم"
                )

                send_telegram_msg(
                    msg,
                    TELEGRAM_BOT3_CHAT_ID
                )

                data["last_status"] = "STRONG"

            elif weak_behavior:

                if data.get("last_status") != "WEAK":

                    msg = (
                        f"⚠️ *Explosion Weakness*\n\n"
                        f"🎫 `{symbol}`\n"
                        f"📈 الربح الحالي: {gain_pct:.2f}%\n\n"
                        f"❌ ضعف واضح بالزخم\n"
                        f"❌ السيولة تبرد\n"
                        f"⚠️ راقب الخروج أو تشديد الوقف"
                    )

                    send_telegram_msg(
                        msg,
                        TELEGRAM_BOT3_CHAT_ID
                    )

                    data["last_status"] = "WEAK"

            data["last_update"] = now_ts

            explosion_tracking[symbol] = data

            if weak_behavior and gain_pct < -3:

                explosion_tracking.pop(symbol, None)

                print(
                    f"🧹 Explosion tracking removed: {symbol}",
                    flush=True
                )

        except Exception as e:

            print(
                f"Explosion tracking error {symbol}: {e}",
                flush=True
            )

def load_active_trades_from_redis():
    global active_trades

    saved = load_json_from_redis(
        BOT3_ACTIVE_TRADES_REDIS_KEY,
        default={}
    )

    if isinstance(saved, dict):
        active_trades = saved
        print(f"✅ Restored active trades from Redis: {len(active_trades)}", flush=True)
    else:
        active_trades = {}
        print("⚠️ No valid Redis active trades found", flush=True)

threading.Thread(
    target=run_web_server,
    daemon=True
).start()

load_active_trades_from_redis()

print("🧪 BOT 3 TIME MACHINE TEST STARTED", flush=True)

print(
    f"🕰️ Testing historical window: {TEST_DATE} "
    f"{TEST_START_HOUR_NY}:{TEST_START_MINUTE_NY:02d} NY "
    f"for {TEST_DURATION_MINUTES} minutes",
    flush=True
)

send_telegram_msg(
    f"🧪 Time Machine Test\n"
    f"📅 {TEST_DATE}\n"
    f"⏰ {TEST_START_HOUR_NY}:{TEST_START_MINUTE_NY:02d} NY\n"
    f"⏳ {TEST_DURATION_MINUTES} minutes",
    TELEGRAM_BOT3_CHAT_ID
)
while True:
    try:
        if not is_trading_time():
            print("⏸️ خارج وقت التشغيل - Bot 3 ينتظر", flush=True)
            time.sleep(300)
            continue

        update_watchlist_from_bot2()
        self_scan_top_400()
        clean_old_watchlist()
        monitor_momentum_watchlist()
        clean_old_pending_watchlist()
        save_pending_candidates_if_changed()
        print(f"📊 Bot 3 Watchlist size: {len(watchlist)}", flush=True)

        sorted_watchlist = sorted(
            list(watchlist.items()),
            key=lambda x: x[1].get("priority_score", 0),
            reverse=True
        )

        symbols_to_check = [
            symbol
            for symbol, data in sorted_watchlist
            if not data.get("alerted", False)
        ]

        bars_map = {}

        for i in range(0, len(symbols_to_check), BULK_BARS_CHUNK_SIZE):
            chunk = symbols_to_check[i:i + BULK_BARS_CHUNK_SIZE]
            bars_map.update(
                get_alpaca_bars_bulk(
                    chunk,
                    minutes=120
                )
            )

        print(
            f"📦 Bulk bars loaded: {len(bars_map)}/{len(symbols_to_check)}",
            flush=True
        )

        for symbol, data in sorted_watchlist:
            if not data.get("alerted", False):
                check_ready_entry(
                    symbol,
                    data,
                    bars_map.get(symbol)
                )
                time.sleep(0.05)

        monitor_active_trades()
        monitor_explosion_tracking()

        current_active_trades = json.dumps(
            active_trades,
            sort_keys=True
        )

        if current_active_trades != last_saved_active_trades:

            save_json_to_redis(
                BOT3_ACTIVE_TRADES_REDIS_KEY,
                active_trades
            )

            last_saved_active_trades = current_active_trades

        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("Main loop error:", e, flush=True)
        time.sleep(10)
