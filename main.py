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

MASTER_LIST_FILE = "master_list.json"


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
# GIST قراءة عامة
# ======================
def read_gist_file(filename, default_value):
    if not GIST_ID or not GITHUB_TOKEN:
        return default_value

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()

        file_data = data.get("files", {}).get(filename)

        if not file_data:
            return default_value

        content = file_data.get("content", "")

        try:
            return json.loads(content)
        except Exception:
            return default_value

    except Exception as e:
        print(f"Gist read error ({filename}):", e, flush=True)
        return default_value


# ======================
# قراءة Master List
# ======================
def load_master_list():
    data = read_gist_file(MASTER_LIST_FILE, [])

    symbols = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                symbol = item
            elif isinstance(item, dict):
                symbol = item.get("symbol")
            else:
                continue

            if (
                symbol
                and isinstance(symbol, str)
                and "." not in symbol
                and "^" not in symbol
                and "-" not in symbol
            ):
                symbols.append(symbol.upper().strip())

    symbols = list(dict.fromkeys(symbols))

    return symbols[:RADAR_BATCH_SIZE]


# ======================
# GIST حفظ إشارات Bot 1
# ======================
def save_signal_to_gist(symbol, price, signal_type):
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}

        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()

        file_data = data.get("files", {}).get("signals.json")

        if file_data:
            content = file_data.get("content", "[]")
        else:
            content = "[]"

        try:
            signals = json.loads(content)
        except Exception:
            signals = []

        now_ts = time.time()

        signals = [
            s for s in signals
            if now_ts - float(s.get("time", 0)) < 1200
        ]

        signals.append({
            "symbol": symbol,
            "price": float(price),
            "source": "main_bot",
            "signal_type": signal_type,
            "time": now_ts
        })

        requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    "signals.json": {
                        "content": json.dumps(signals, ensure_ascii=False)
                    }
                }
            },
            timeout=10
        )

        print(f"Saved: {symbol}", flush=True)

    except Exception as e:
        print("Gist error:", e, flush=True)


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
            end=end.isoformat() + "Z",
            adjustment="raw"
        ).df

        if df is None or df.empty:
            return pd.DataFrame()

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

        df = df[needed].dropna()

        return df.tail(120)

    except Exception as e:
        print(f"Alpaca bars error {symbol}: {e}", flush=True)
        return pd.DataFrame()


# ======================
# قائمة الأسهم من Master List
# ======================
def get_symbols():
    symbols = load_master_list()

    if not symbols:
        print("⚠️ Master List empty or not found", flush=True)
        return []

    return symbols


# ======================
# ترتيب حسب النشاط
# ======================
def rank_symbols(symbols):
    ranked = []

    for s in symbols:
        try:
            df = get_bars(s)

            if df.empty or len(df) < 20:
                continue

            cp = df["Close"].iloc[-1]
            old = df["Close"].iloc[-10]

            if cp <= 0 or old <= 0:
                continue

            if not (PRICE_MIN <= cp <= PRICE_MAX):
                continue

            change = abs((cp - old) / old * 100)
            vol = df["Volume"].sum()

            score = change + (vol / 1_000_000)

            ranked.append((s, score))

            time.sleep(0.005)

        except Exception as e:
            print(f"Rank error {s}: {e}", flush=True)
            continue

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
# تنظيف التنبيهات القديمة
# ======================
def clean_confirmed_alerts():
    now = datetime.now()

    expired = []

    for symbol, expire_time in confirmed_alerts.items():
        if now >= expire_time:
            expired.append(symbol)

    for symbol in expired:
        confirmed_alerts.pop(symbol, None)


# ======================
# التحليل
# ======================
def scan():
    global confirmed_alerts

    clean_confirmed_alerts()

    symbols = get_symbols()
    symbols = rank_symbols(symbols)

    print(f"Scanning {len(symbols)} symbols", flush=True)

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

        except Exception as e:
            print(f"Scan error {s}: {e}", flush=True)
            continue


# ======================
# MAIN LOOP
# ======================
print("🚀 BOT 1 STARTED", flush=True)

while True:
    try:
        if not is_trading_time():
            time.sleep(300)
            continue

        scan()
        time.sleep(15)

    except Exception as e:
        print("Error:", e, flush=True)
        time.sleep(10) 
