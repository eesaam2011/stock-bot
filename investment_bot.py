import os
import time
import json
import requests
import threading
import pandas as pd
import alpaca_trade_api as tradeapi
from flask import Flask
from datetime import datetime, timedelta
import pytz

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)

saudi_tz = pytz.timezone("Asia/Riyadh")

PRICE_MIN = 0.5
PRICE_MAX = 10

INVESTMENT_SYMBOL_LIMIT = 800

TOP_CANDIDATES_THURSDAY = 10
FINAL_MIN_SCORE = 65
FINAL_MAX_PICKS = 5
FINAL_MIN_PICKS = 3

SCAN_INTERVAL = 900

STATE_FILE = "investment_state.json"
NEWS_FILE = "news_signals.json"

MONDAY_ALERT_HOUR = 17
MONDAY_ALERT_MINUTE = 45

app = Flask(__name__)

@app.route("/")
def home():
    return "Investment Bot Running"


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram keys missing", flush=True)
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e, flush=True)


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
        print(f"Read gist error {filename}:", e, flush=True)
        return default_value


def save_gist_file(filename, content_obj):
    if not GIST_ID or not GITHUB_TOKEN:
        print("Gist keys missing", flush=True)
        return

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    filename: {
                        "content": json.dumps(content_obj, ensure_ascii=False)
                    }
                }
            },
            timeout=10
        )

        print(f"Gist saved: {filename}", flush=True)

    except Exception as e:
        print(f"Save gist error {filename}:", e, flush=True)


def load_state():
    default_state = {
        "last_thursday_scan": "",
        "last_monday_alert": "",
        "candidates": [],
        "active_picks": []
    }

    state = read_gist_file(STATE_FILE, default_state)

    for key in default_state:
        if key not in state:
            state[key] = default_state[key]

    return state


def save_state(state):
    save_gist_file(STATE_FILE, state)


def get_base_list():
    black_list = [
        "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC",
        "MET", "PRU", "ALL", "AIG", "CB",
        "DKNG", "PENN", "WYNN", "LVS",
        "BUD", "TAP", "STZ", "DEO",
        "PM", "MO",
        "CGC", "TLRY", "ACB",
        "NCLH", "CCL", "RCL"
    ]

    try:
        assets = api.list_assets(status="active")
        symbols = []

        for asset in assets:
            symbol = getattr(asset, "symbol", None)

            if not symbol:
                continue

            if (
                getattr(asset, "tradable", False)
                and getattr(asset, "asset_class", "") == "us_equity"
                and isinstance(symbol, str)
                and "." not in symbol
                and "^" not in symbol
                and "-" not in symbol
                and symbol not in black_list
            ):
                symbols.append(symbol)

        return list(set(symbols))[:INVESTMENT_SYMBOL_LIMIT]

    except Exception as e:
        print("Alpaca base list error:", e, flush=True)
        return []


def get_daily_bars(symbol, days=220):
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=days)

        bars = api.get_bars(
            symbol,
            tradeapi.TimeFrame.Day,
            start=start.isoformat() + "Z",
            end=end.isoformat() + "Z",
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

        return df[needed].dropna()

    except Exception as e:
        print(f"Alpaca daily bars error {symbol}: {e}", flush=True)
        return pd.DataFrame()


def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()

    if loss.iloc[-1] == 0:
        return 100

    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


def get_news(symbol):
    news_list = read_gist_file(NEWS_FILE, [])
    now = time.time()

    best = None
    best_score = 0

    for n in news_list:
        if n.get("symbol") != symbol:
            continue

        age = now - float(n.get("time", 0))

        if age > 7 * 86400:
            continue

        if n.get("news_grade") == "NEGATIVE":
            return "NEGATIVE", 0, n.get("headline", "")

        score = n.get("news_score", 0)

        if score > best_score:
            best = n
            best_score = score

    if best:
        return best.get("news_grade"), best_score, best.get("headline", "")

    return "NONE", 0, ""


def get_target_timing():
    now = datetime.now(saudi_tz)

    target1_date = now + timedelta(days=7)
    target2_date = now + timedelta(days=30)

    return {
        "target1_date": target1_date.strftime("%Y-%m-%d"),
        "target2_date": target2_date.strftime("%Y-%m-%d"),
        "target1_duration": "تقريبًا خلال 7 أيام",
        "target2_duration": "تقريبًا خلال 30 يوم"
    }


def analyze_stock(symbol):
    try:
        df = get_daily_bars(symbol, days=220)

        if df.empty or len(df) < 60:
            return None

        price = df["Close"].iloc[-1]

        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None

        avg_vol = df["Volume"].tail(30).mean()
        if avg_vol < 300000:
            return None

        df["SMA20"] = df["Close"].rolling(20).mean()
        df["SMA50"] = df["Close"].rolling(50).mean()

        sma20 = df["SMA20"].iloc[-1]
        sma50 = df["SMA50"].iloc[-1]

        rsi = calculate_rsi(df["Close"])

        high30 = df["High"].tail(30).max()
        low30 = df["Low"].tail(30).min()

        move5 = ((price - df["Close"].iloc[-6]) / df["Close"].iloc[-6]) * 100

        vol_ratio = df["Volume"].tail(5).mean() / df["Volume"].tail(20).mean()

        news_grade, news_score, headline = get_news(symbol)

        if news_grade == "NEGATIVE":
            return None

        score = 0
        reasons = []

        if price > sma20:
            score += 10
            reasons.append("فوق SMA20")

        if price > sma50:
            score += 10
            reasons.append("فوق SMA50")

        if vol_ratio > 1:
            score += 10
            reasons.append("سيولة جيدة")

        if 45 <= rsi <= 65:
            score += 10
            reasons.append("RSI مناسب")

        if price >= high30 * 0.88:
            score += 15
            reasons.append("قريب اختراق")

        if abs(move5) < 20:
            score += 10
            reasons.append("غير منفجر")

        if news_grade == "STRONG":
            score += 20
            reasons.append("خبر قوي")

        elif news_grade == "MEDIUM":
            score += 10
            reasons.append("خبر متوسط")

        if score < FINAL_MIN_SCORE:
            return None

        timing = get_target_timing()

        entry = price
        stop = min(price * 0.9, low30 * 0.98)
        t1 = price * 1.15
        t2 = price * 1.35

        return {
            "symbol": symbol,
            "entry": float(entry),
            "stop": float(stop),
            "t1": float(t1),
            "t2": float(t2),
            "target1_date": timing["target1_date"],
            "target2_date": timing["target2_date"],
            "target1_duration": timing["target1_duration"],
            "target2_duration": timing["target2_duration"],
            "score": score,
            "headline": headline,
            "reasons": reasons,
            "alerts": {}
        }

    except Exception as e:
        print(f"Analyze error {symbol}: {e}", flush=True)
        return None


def send_monday():
    symbols = get_base_list()

    results = []

    for s in symbols:
        r = analyze_stock(s)
        if r:
            results.append(r)
        time.sleep(0.05)

    results = sorted(results, key=lambda x: x["score"], reverse=True)[:FINAL_MAX_PICKS]

    if not results:
        send_telegram_msg("📉 لا توجد فرص هذا الأسبوع")
        return

    msg = "📈 أفضل أسهم الأسبوع\n\n"

    for r in results:
        msg += (
            f"{r['symbol']}\n"
            f"دخول: {r['entry']:.2f}\n"
            f"وقف: {r['stop']:.2f}\n"
            f"هدف1: {r['t1']:.2f}\n"
            f"تاريخ هدف1 المتوقع: {r['target1_date']}\n"
            f"مدة هدف1: {r['target1_duration']}\n"
            f"هدف2: {r['t2']:.2f}\n"
            f"تاريخ هدف2 المتوقع: {r['target2_date']}\n"
            f"مدة هدف2: {r['target2_duration']}\n"
            f"نسبة: {r['score']}%\n"
        )

        if r["headline"]:
            msg += f"📰 {r['headline']}\n"

        msg += "\n"

    send_telegram_msg(msg)

    state = load_state()
    state["active_picks"] = results
    state["last_monday_alert"] = datetime.now(saudi_tz).strftime("%Y-%m-%d")
    save_state(state)


def monitor():
    state = load_state()
    picks = state.get("active_picks", [])

    for p in picks:
        try:
            symbol = p["symbol"]
            df = get_daily_bars(symbol, days=45)

            if df.empty:
                continue

            price = df["Close"].iloc[-1]
            entry = p["entry"]

            gain = ((price - entry) / entry) * 100

            if gain > 5 and not p["alerts"].get("start"):
                send_telegram_msg(f"🚀 {symbol} بدأ يتحرك")
                p["alerts"]["start"] = True

            if gain > 10 and not p["alerts"].get("raise"):
                new_stop = entry * 1.02
                send_telegram_msg(f"🔒 ارفع وقف {symbol} إلى {new_stop:.2f}")
                p["alerts"]["raise"] = True

            if price <= p["stop"] and not p["alerts"].get("stop"):
                send_telegram_msg(f"🛑 خروج {symbol}")
                p["alerts"]["stop"] = True

        except Exception:
            continue

    state["active_picks"] = picks
    save_state(state)


threading.Thread(target=run_web_server, daemon=True).start()

print("📈 Investment Bot Started", flush=True)

while True:
    now = datetime.now(saudi_tz)

    try:
        state = load_state()
        today = now.strftime("%Y-%m-%d")

        if (
            now.weekday() == 0
            and now.hour == MONDAY_ALERT_HOUR
            and now.minute >= MONDAY_ALERT_MINUTE
            and state.get("last_monday_alert") != today
        ):
            send_monday()

        monitor()

        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("Main error:", e, flush=True)
        time.sleep(30)
