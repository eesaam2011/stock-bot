import os
import time
import json
import requests
import threading
import pandas as pd
import alpaca_trade_api as tradeapi
from flask import Flask
from datetime import datetime, timedelta, timezone
import pytz

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)
saudi_tz = pytz.timezone("Asia/Riyadh")

app = Flask(__name__)

LIVE_MOVERS_FILE = "live_movers.json"

SCAN_INTERVAL = 60
PRICE_MIN = 0.4
PRICE_MAX = 25

MAX_SYMBOLS_TO_SCAN = 5000
CHUNK_SIZE = 200
BARS_MINUTES = 30
TOP_SAVE_COUNT = 200

MIN_DOLLAR_VOLUME_10M = 150000
MIN_MOVE_3M = 0.40
MIN_INSTANT_RVOL = 2.0


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


@app.route("/")
def home():
    return "Live Movers Scanner Running"


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


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

        for asset in assets:
            if not getattr(asset, "tradable", False):
                continue

            if is_blacklisted_asset(asset):
                continue

            symbol = clean_symbol(asset.symbol)
            if symbol:
                symbols.append(symbol)

        symbols = list(dict.fromkeys(symbols))
        symbols = symbols[:MAX_SYMBOLS_TO_SCAN]

        print(f"📦 Loaded Alpaca live universe: {len(symbols)}", flush=True)
        return symbols

    except Exception as e:
        print(f"❌ Alpaca assets error: {e}", flush=True)
        return []


def get_market_session_label():
    now = datetime.now(saudi_tz)
    minutes = now.hour * 60 + now.minute

    if 16 * 60 <= minutes < 23 * 60 + 30:
        return "REGULAR_OR_PREMARKET"

    if minutes >= 23 * 60 + 30 or minutes < 3 * 60:
        return "AFTER_HOURS"

    return "OFF_HOURS"


def analyze_symbol_bars(symbol, df):
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
            and move_3m >= MIN_MOVE_3M
            and cp > 0
            and close_position >= 0.55
            and upper_wick_pct <= 0.45
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
            "source": "LIVE_MOVERS",
            "session": get_market_session_label(),
            "time": time.time(),
            "created_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")
        }

    except Exception as e:
        print(f"Analyze bars error {symbol}: {e}", flush=True)
        return None


def scan_live_movers():
    symbols = load_alpaca_universe()

    if not symbols:
        print("⚠️ No Alpaca symbols loaded", flush=True)
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
                result = analyze_symbol_bars(symbol, bars)

                if result:
                    movers.append(result)

            print(
                f"🔎 Live scan {min(i + CHUNK_SIZE, len(symbols))}/{len(symbols)} | movers: {len(movers)}",
                flush=True
            )

            time.sleep(0.2)

        except Exception as e:
            print(f"Chunk scan error {i}: {e}", flush=True)
            time.sleep(1)
            continue

    movers = sorted(
        movers,
        key=lambda x: (
            x.get("hot_score", 0),
            x.get("move_3m", 0),
            x.get("instant_rvol", 0),
            x.get("dollar_volume_10m", 0)
        ),
        reverse=True
    )

    return movers[:TOP_SAVE_COUNT]


def run_once():
    print("🚀 Live Movers Scanner running...", flush=True)

    movers = scan_live_movers()

    if movers:
        save_gist_file(LIVE_MOVERS_FILE, movers)
    else:
        print("⚠️ No live movers found this cycle", flush=True)

    print(f"✅ Live Movers cycle completed: {len(movers)}", flush=True)


threading.Thread(target=run_web_server, daemon=True).start()

print("🚀 LIVE MOVERS SCANNER STARTED", flush=True)


while True:
    try:
        run_once()
        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print(f"Main loop error: {e}", flush=True)
        time.sleep(30)
