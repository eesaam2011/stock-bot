import os
import time
import json
import requests
import pandas as pd
import alpaca_trade_api as tradeapi
from datetime import datetime, timedelta
import pytz

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)

saudi_tz = pytz.timezone("Asia/Riyadh")

confirmed_alerts = {}

PRICE_MIN = 0.5
PRICE_MAX = 25
RADAR_BATCH_SIZE = 600


# ======================
# وقت التشغيل
# ======================
def is_trading_time():
    now = datetime.now(saudi_tz)
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()

    if weekday in [5, 6]:
        return False

    if hour < 8 or (hour == 8 and minute < 30):
        return False

    return True


# ======================
# GIST
# ======================
def save_signal_to_gist(symbol, price, signal_type):
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}

        res = requests.get(url, headers=headers)
        content = res.json()["files"]["signals.json"]["content"]

        try:
            signals = json.loads(content)
        except:
            signals = []

        now_ts = time.time()

        signals = [s for s in signals if now_ts - float(s.get("time", 0)) < 1200]

        signals.append({
            "symbol": symbol,
            "price": float(price),
            "source": "main_bot",
            "time": now_ts
        })

        requests.patch(
            url,
            headers=headers,
            json={"files": {"signals.json": {"content": json.dumps(signals)}}}
        )

        print(f"Saved: {symbol}")

    except Exception as e:
        print("Gist error:", e)


# ======================
# Alpaca Bars
# ======================
def get_bars(symbol):
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=1)

        df = api.get_bars(
            symbol,
            tradeapi.TimeFrame.Minute,
            start=start.isoformat() + "Z",
            end=end.isoformat() + "Z"
        ).df

        if df.empty:
            return pd.DataFrame()

        df = df.rename(columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume"
        })

        return df.tail(120)

    except:
        return pd.DataFrame()


# ======================
# قائمة الأسهم
# ======================
def get_symbols():
    try:
        assets = api.list_assets(status="active")
        symbols = []

        for a in assets:
            if (
                a.tradable
                and a.asset_class == "us_equity"
                and "." not in a.symbol
            ):
                symbols.append(a.symbol)

        return list(set(symbols))

    except:
        return []


# ======================
# ترتيب حسب النشاط
# ======================
def rank_symbols(symbols):
    ranked = []

    for s in symbols:
        df = get_bars(s)
        if df.empty or len(df) < 20:
            continue

        cp = df["Close"].iloc[-1]
        old = df["Close"].iloc[-10]

        if not (PRICE_MIN <= cp <= PRICE_MAX):
            continue

        change = abs((cp - old) / old * 100)
        vol = df["Volume"].sum()

        score = change + (vol / 1_000_000)

        ranked.append((s, score))

        time.sleep(0.005)

    ranked.sort(key=lambda x: x[1], reverse=True)

    return [x[0] for x in ranked[:RADAR_BATCH_SIZE]]


# ======================
# RSI
# ======================
def calculate_rsi(close):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()

    if loss.iloc[-1] == 0:
        return 100

    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


# ======================
# التحليل
# ======================
def scan():
    global confirmed_alerts

    symbols = get_symbols()
    symbols = rank_symbols(symbols)

    print(f"Scanning {len(symbols)} symbols")

    for s in symbols:
        try:
            df = get_bars(s)

            if df.empty or len(df) < 30:
                continue

            cp = df["Close"].iloc[-1]
            day_high = df["High"].max()

            rsi = calculate_rsi(df["Close"])
            rvol = df["Volume"].tail(3).mean() / df["Volume"].mean()
            move = ((cp - df["Close"].iloc[-10]) / df["Close"].iloc[-10]) * 100

            df["EMA9"] = df["Close"].ewm(span=9).mean()
            df["EMA20"] = df["Close"].ewm(span=20).mean()

            ema9 = df["EMA9"].iloc[-1]
            ema20 = df["EMA20"].iloc[-1]

            if (
                cp > ema9
                and cp > ema20
                and rsi > 50
                and rvol > 2
                and cp >= day_high * 0.97
                and abs(move) < 3
                and s not in confirmed_alerts
            ):
                save_signal_to_gist(s, cp, "radar")

                confirmed_alerts[s] = datetime.now() + timedelta(minutes=15)

            time.sleep(0.03)

        except:
            continue


# ======================
# MAIN LOOP
# ======================
print("🚀 BOT 1 STARTED")

while True:
    try:
        if not is_trading_time():
            time.sleep(300)
            continue

        scan()
        time.sleep(15)

    except Exception as e:
        print("Error:", e)
        time.sleep(10)
