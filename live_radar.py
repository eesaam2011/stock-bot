import os
import time
import json
import requests
import threading
import asyncio
from collections import defaultdict, deque
import pandas as pd
import alpaca_trade_api as tradeapi
from flask import Flask
from datetime import datetime, timedelta, timezone
import pytz

try:
    from alpaca.data.live import StockDataStream
    from alpaca.data.enums import DataFeed
    ALPACA_PY_AVAILABLE = True
except Exception:
    ALPACA_PY_AVAILABLE = False


API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN_LIVE")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

LIVE_MOVERS_REDIS_KEY = "live_movers"

api = tradeapi.REST(
    API_KEY,
    SECRET_KEY,
    BASE_URL,
    api_version="v2"
)

saudi_tz = pytz.timezone("Asia/Riyadh")

app = Flask(__name__)

LIVE_MOVERS_FILE = "live_movers.json"
last_saved_live_movers_signature = ""
last_live_movers_save_time = 0

PRICE_MIN = 0.4
PRICE_MAX = 25

# WebSocket / Live Engine timing
EVENT_PROCESS_INTERVAL = 5          # تحليل مصغر كل 5 ثواني
LIVE_SAVE_INTERVAL = 30             # تحديث live_movers.json كل 30 ثانية
WEBSOCKET_STALE_SECONDS = 90        # إذا انقطعت بيانات WebSocket نستخدم الاحتياط

# Polling fallback فقط عند الحاجة
POLLING_FALLBACK_INTERVAL = 60
CHUNK_SIZE = 200
BARS_MINUTES = 30

MIN_DOLLAR_VOLUME_10M = 50000
MIN_MOVE_3M = 0.25
MIN_INSTANT_RVOL = 1.5

# لا يوجد حد إجباري لعدد الأسهم المحفوظة
# كل سهم يجتاز الفلاتر يدخل live_movers.json ثم يرتب حسب hot_score

SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS",
    "DKNG", "PENN", "WYNN", "LVS",
    "BUD", "STZ", "DEO",
    "PM", "MO",
    "CGC", "TLRY", "ACB",
    "NCLH", "CCL", "RCL",
    "AMC", "GPRE", "SKLZ", "PGY", "JELD", "TWO", "PGEN", "GENI", "TRC",
}

BAD_KEYWORDS = [
    "bank", "financial", "insurance", "credit", "lending",
    "casino", "gambling", "betting", "sportsbook",
    "alcohol", "beer", "wine", "tobacco", "cannabis",
    "marijuana", "hemp", "cruise", "adult",
    "etf", "fund", "trust", "warrant", "unit"
]

# WebSocket state
allowed_symbols = set()
asset_names = {}
symbol_bars = defaultdict(lambda: deque(maxlen=40))
current_minute_bar = {}
last_trade_time = {}
last_websocket_event_ts = 0
latest_movers = []
state_lock = threading.Lock()


@app.route("/")
def home():
    return "Live Movers WebSocket Scanner Running"


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def save_live_movers_to_redis(movers):
    try:
        if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
            print("⚠️ Upstash Redis env vars missing", flush=True)
            return False

        url = f"{UPSTASH_REDIS_REST_URL}/set/{LIVE_MOVERS_REDIS_KEY}"

        headers = {
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"
        }

        payload = json.dumps(movers)

        r = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10
        )

        if r.status_code in [200, 201]:
            print(f"✅ Saved live movers to Redis: {len(movers)}", flush=True)
            return True

        print(f"❌ Redis save failed: {r.status_code} {r.text}", flush=True)
        return False

    except Exception as e:
        print(f"❌ Redis save exception: {e}", flush=True)
        return False
        
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

        res = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    filename: {
                        "content": json.dumps(data, ensure_ascii=False)
                    }
                }
            },
            timeout=15
        )

        if res.status_code not in [200, 201]:
            print(f"❌ Gist save failed {filename}: {res.text[:300]}", flush=True)
            return

        print(f"✅ Saved {filename}: {len(data)} movers", flush=True)

    except Exception as e:
        print(f"❌ Gist save error {filename}: {e}", flush=True)


def clean_symbol(symbol):
    if not symbol or not isinstance(symbol, str):
        return None

    symbol = symbol.upper().strip()

    if symbol in SYMBOL_BLACKLIST:
        return None

    if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
        return None

    if len(symbol) > 5:
        return None

    if not symbol.isalpha():
        return None

    return symbol


def is_blacklisted_asset(asset):
    symbol = clean_symbol(asset.symbol)
    if not symbol:
        return True

    name = str(getattr(asset, "name", "") or "").lower()

    for kw in BAD_KEYWORDS:
        if kw in name:
            return True

    return False


def load_alpaca_universe():
    try:
        assets = api.list_assets(status="active")
        symbols = []
        names = {}

        for asset in assets:
            if not getattr(asset, "tradable", False):
                continue

            if is_blacklisted_asset(asset):
                continue

            symbol = clean_symbol(asset.symbol)
            if symbol:
                symbols.append(symbol)
                names[symbol] = str(getattr(asset, "name", "") or "")

        symbols = list(dict.fromkeys(symbols))

        print(f"📦 Loaded clean Alpaca universe: {len(symbols)}", flush=True)
        return symbols, names

    except Exception as e:
        print(f"❌ Alpaca assets error: {e}", flush=True)
        return [], {}


def get_market_session_label():
    now = datetime.now(saudi_tz)
    minutes = now.hour * 60 + now.minute

    if 16 * 60 <= minutes < 23 * 60 + 30:
        return "REGULAR_OR_PREMARKET"

    if minutes >= 23 * 60 + 30 or minutes < 3 * 60:
        return "AFTER_HOURS"

    return "OFF_HOURS"


def get_field(obj, *names, default=None):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj.get(name)

        if hasattr(obj, name):
            return getattr(obj, name)

    return default


def minute_bucket(ts):
    try:
        if ts is None:
            return int(time.time() // 60)

        if isinstance(ts, str):
            dt = pd.to_datetime(ts, utc=True)
            return int(dt.timestamp() // 60)

        if hasattr(ts, "timestamp"):
            return int(ts.timestamp() // 60)

        return int(time.time() // 60)

    except Exception:
        return int(time.time() // 60)


def update_symbol_bar_from_trade(symbol, price, size, ts):
    global last_websocket_event_ts

    if not symbol or price <= 0 or size <= 0:
        return

    if allowed_symbols and symbol not in allowed_symbols:
        return

    if not (PRICE_MIN <= price <= PRICE_MAX):
        return

    bucket = minute_bucket(ts)
    now_ts = time.time()

    with state_lock:
        last_websocket_event_ts = now_ts
        last_trade_time[symbol] = now_ts

        current = current_minute_bar.get(symbol)

        if current is None:
            current_minute_bar[symbol] = {
                "minute": bucket,
                "Open": price,
                "High": price,
                "Low": price,
                "Close": price,
                "Volume": float(size),
            }
            return

        if current["minute"] == bucket:
            current["High"] = max(current["High"], price)
            current["Low"] = min(current["Low"], price)
            current["Close"] = price
            current["Volume"] += float(size)
            return

        symbol_bars[symbol].append({
            "Open": current["Open"],
            "High": current["High"],
            "Low": current["Low"],
            "Close": current["Close"],
            "Volume": current["Volume"],
        })

        current_minute_bar[symbol] = {
            "minute": bucket,
            "Open": price,
            "High": price,
            "Low": price,
            "Close": price,
            "Volume": float(size),
        }


def build_df_from_state(symbol):
    with state_lock:
        bars = list(symbol_bars.get(symbol, []))
        current = current_minute_bar.get(symbol)

        if current:
            bars.append({
                "Open": current["Open"],
                "High": current["High"],
                "Low": current["Low"],
                "Close": current["Close"],
                "Volume": current["Volume"],
            })

    if len(bars) < 12:
        return None

    return pd.DataFrame(bars)


def analyze_symbol_bars(symbol, df, source="WEBSOCKET_LIVE_MOVERS"):
    try:
        if df is None or df.empty or len(df) < 12:
            return None

        if "symbol" in df.columns:
            df = df[df["symbol"] == symbol]

        if df.empty or len(df) < 12:
            return None

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
                return None

        df = df[needed].dropna()

        if df.empty or len(df) < 12 or df["Volume"].mean() <= 0:
            return None

        cp = float(df["Close"].iloc[-1])

        if not (PRICE_MIN <= cp <= PRICE_MAX):
            return None

        price_1m = float(df["Close"].iloc[-2])
        price_3m = float(df["Close"].iloc[-4])
        price_5m = float(df["Close"].iloc[-6])
        price_10m = float(df["Close"].iloc[-11])

        move_1m = ((cp - price_1m) / price_1m) * 100 if price_1m > 0 else 0
        move_3m = ((cp - price_3m) / price_3m) * 100 if price_3m > 0 else 0
        move_5m = ((cp - price_5m) / price_5m) * 100 if price_5m > 0 else 0
        move_10m = ((cp - price_10m) / price_10m) * 100 if price_10m > 0 else 0

        last_3_volume = float(df["Volume"].tail(3).mean())
        avg_volume = float(df["Volume"].mean())
        instant_rvol = last_3_volume / max(avg_volume, 1)

        volume_10m = float(df["Volume"].tail(10).sum())
        dollar_volume_10m = cp * volume_10m

        day_high = float(df["High"].max())
        near_high = cp >= day_high * 0.975 if day_high > 0 else False

        last_open = float(df["Open"].iloc[-1])
        last_close = float(df["Close"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])

        candle_range = last_high - last_low
        if candle_range <= 0:
            return None

        close_position = (last_close - last_low) / candle_range
        upper_wick_pct = (last_high - last_close) / candle_range
        body_ratio = abs(last_close - last_open) / candle_range

        volume_spike = last_3_volume >= avg_volume * 2.0

        hot_score = 0
        hot_score += min(move_3m * 12, 35)
        hot_score += min(move_5m * 8, 25)
        hot_score += min(instant_rvol * 6, 25)

        if volume_spike:
            hot_score += 10

        if near_high:
            hot_score += 8

        if close_position >= 0.70:
            hot_score += 6

        if upper_wick_pct <= 0.30:
            hot_score += 5

        if body_ratio >= 0.35:
            hot_score += 5

        hot_mover = (
            dollar_volume_10m >= MIN_DOLLAR_VOLUME_10M
            and instant_rvol >= MIN_INSTANT_RVOL
            and (
                move_3m >= MIN_MOVE_3M
                or move_5m >= 0.40
                or move_10m >= 0.70
            )
            and cp > 0
            and close_position >= 0.50
            and upper_wick_pct <= 0.50
        )

        if not hot_mover:
            return None

        return {
            "symbol": symbol,
            "price": round(cp, 4),
            "move_1m": round(move_1m, 2),
            "move_3m": round(move_3m, 2),
            "move_5m": round(move_5m, 2),
            "move_10m": round(move_10m, 2),
            "instant_rvol": round(float(instant_rvol), 2),
            "dollar_volume_10m": round(float(dollar_volume_10m), 2),
            "volume_spike": bool(volume_spike),
            "near_high": bool(near_high),
            "close_position": round(float(close_position), 2),
            "upper_wick_pct": round(float(upper_wick_pct), 2),
            "body_ratio": round(float(body_ratio), 2),
            "hot_score": round(float(hot_score), 2),
            "source": source,
            "session": get_market_session_label(),
            "time": time.time(),
            "created_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")
        }

    except Exception as e:
        print(f"Analyze bars error {symbol}: {e}", flush=True)
        return None


def sort_movers(movers):
    return sorted(
        movers,
        key=lambda x: (
            x.get("hot_score", 0),
            x.get("move_3m", 0),
            x.get("instant_rvol", 0),
            x.get("dollar_volume_10m", 0)
        ),
        reverse=True
    )


def build_websocket_movers():
    movers = []

    with state_lock:
        symbols = list(current_minute_bar.keys())

    for symbol in symbols:
        df = build_df_from_state(symbol)
        result = analyze_symbol_bars(symbol, df, source="WEBSOCKET_LIVE_MOVERS")

        if result:
            movers.append(result)

    return sort_movers(movers)


async def websocket_trade_handler(trade):
    try:
        symbol = clean_symbol(get_field(trade, "symbol", "S"))
        if not symbol:
            return

        price = float(get_field(trade, "price", "p", default=0) or 0)
        size = float(get_field(trade, "size", "s", default=0) or 0)
        ts = get_field(trade, "timestamp", "t", default=None)

        update_symbol_bar_from_trade(symbol, price, size, ts)

    except Exception as e:
        print(f"WebSocket trade handler error: {e}", flush=True)


def get_data_feed():
    feed = os.getenv("ALPACA_DATA_FEED", "sip").lower().strip()

    if feed == "iex":
        return DataFeed.IEX

    return DataFeed.SIP


def run_websocket_stream():
    global last_websocket_event_ts

    if not ALPACA_PY_AVAILABLE:
        print("❌ alpaca-py not installed. WebSocket disabled. Polling fallback will run.", flush=True)
        return

    while True:
        try:
            print("🔌 Starting Alpaca WebSocket stream...", flush=True)

            stream = StockDataStream(
                API_KEY,
                SECRET_KEY,
                feed=get_data_feed()
            )

            stream.subscribe_trades(websocket_trade_handler, "*")
            print("✅ Subscribed to WebSocket trades: *", flush=True)

            last_websocket_event_ts = time.time()
            stream.run()

        except Exception as e:
            print(f"❌ WebSocket stream error: {e}", flush=True)
            time.sleep(60)


def websocket_processor_loop():
    global latest_movers

    while True:
        try:
            movers = build_websocket_movers()

            if movers:
                latest_movers = movers
                save_live_movers_to_redis(movers)
                print(f"⚡ WebSocket movers updated: {len(movers)}", flush=True)
            else:
                print("⚠️ WebSocket active but no movers passed filters", flush=True)

            time.sleep(LIVE_SAVE_INTERVAL)

        except Exception as e:
            print(f"WebSocket processor error: {e}", flush=True)
            time.sleep(10)


def scan_live_movers_polling_fallback():
    symbols, _ = load_alpaca_universe()

    if not symbols:
        print("⚠️ No Alpaca symbols loaded for fallback", flush=True)
        return []

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=BARS_MINUTES + 10)

    movers = []

    for i in range(0, len(symbols), CHUNK_SIZE):
        chunk = symbols[i:i + CHUNK_SIZE]

        try:
            bars = api.get_bars(
                chunk,
                tradeapi.TimeFrame.Minute,
                start=start.isoformat(),
                end=end.isoformat(),
                adjustment="raw"
            ).df

            if bars is None or bars.empty:
                continue

            for symbol in chunk:
                result = analyze_symbol_bars(
                    symbol,
                    bars,
                    source="POLLING_LIVE_MOVERS_BACKUP"
                )

                if result:
                    movers.append(result)

            print(
                f"🔎 Backup polling scan {min(i + CHUNK_SIZE, len(symbols))}/{len(symbols)} | movers: {len(movers)}",
                flush=True
            )

            time.sleep(0.2)

        except Exception as e:
            print(f"Chunk fallback scan error {i}: {e}", flush=True)
            time.sleep(1)
            continue

    return sort_movers(movers)


def fallback_supervisor_loop():
    while True:
        try:
            now_ts = time.time()
            websocket_age = now_ts - last_websocket_event_ts if last_websocket_event_ts else 999999

            if websocket_age > WEBSOCKET_STALE_SECONDS:
                print(
                    f"🛟 WebSocket stale for {round(websocket_age)}s. Running polling Live Movers backup...",
                    flush=True
                )

                movers = scan_live_movers_polling_fallback()

                if movers:
                    save_live_movers_to_redis(movers)
                    print(f"✅ Backup polling saved movers: {len(movers)}", flush=True)
                else:
                    print("⚠️ Backup polling found no movers", flush=True)

            time.sleep(POLLING_FALLBACK_INTERVAL)

        except Exception as e:
            print(f"Fallback supervisor error: {e}", flush=True)
            time.sleep(30)


def warmup_from_polling_once():
    print("🔥 Warmup: running one polling scan to seed live_movers.json...", flush=True)

    movers = scan_live_movers_polling_fallback()

    if movers:
        save_live_movers_to_redis(movers)

    print(
        f"✅ Warmup saved movers: {len(movers)}",
        flush=True
    )
    
    else:
        print("⚠️ Warmup found no movers", flush=True)    
        
threading.Thread(target=run_web_server, daemon=True).start()

print("🚀 LIVE MOVERS WEBSOCKET SCANNER STARTED", flush=True)

symbols, names = load_alpaca_universe()
allowed_symbols = set(symbols)
asset_names = names

print(f"✅ Allowed clean symbols for WebSocket filter: {len(allowed_symbols)}", flush=True)

threading.Thread(target=warmup_from_polling_once, daemon=True).start()
threading.Thread(target=run_websocket_stream, daemon=True).start()
threading.Thread(target=websocket_processor_loop, daemon=True).start()
threading.Thread(target=fallback_supervisor_loop, daemon=True).start()

while True:
    try:
        time.sleep(60)
        print(
            f"💓 Alive | allowed_symbols={len(allowed_symbols)} | tracked={len(current_minute_bar)} | latest_movers={len(latest_movers)}",
            flush=True
        )

    except Exception as e:
        print(f"Main keepalive error: {e}", flush=True)
        time.sleep(30)
