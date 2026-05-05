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
PRICE_MAX = 15

INVESTMENT_SYMBOL_LIMIT = 800
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
        "last_weekly_universe": "",
        "last_daily_refresh": "",
        "last_monday_alert": "",
        "weekly_universe": [],
        "active_picks": []
    }

    state = read_gist_file(STATE_FILE, default_state)

    for key in default_state:
        if key not in state:
            state[key] = default_state[key]

    return state


def save_state(state):
    save_gist_file(STATE_FILE, state)


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

        score = float(n.get("news_score", 0))

        if score > best_score:
            best = n
            best_score = score

    if best:
        return best.get("news_grade"), best_score, best.get("headline", "")

    return "NONE", 0, ""


def is_clean_symbol(symbol):
    if not symbol:
        return False

    if not isinstance(symbol, str):
        return False

    if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
        return False

    if len(symbol) > 5:
        return False

    if not symbol.isalpha():
        return False

    return True


def get_all_alpaca_symbols():
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

            if not is_clean_symbol(symbol):
                continue

            asset_class = getattr(asset, "asset_class", "")
            tradable = getattr(asset, "tradable", False)

            if (
                tradable
                and asset_class == "us_equity"
                and symbol not in black_list
            ):
                symbols.append(symbol)

        return list(set(symbols))

    except Exception as e:
        print("Alpaca asset list error:", e, flush=True)
        return []


def score_investment_universe(symbol):
    try:
        df = get_daily_bars(symbol, days=220)

        if df.empty or len(df) < 80:
            return None

        price = float(df["Close"].iloc[-1])

        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None

        avg_vol_30 = float(df["Volume"].tail(30).mean())
        avg_vol_10 = float(df["Volume"].tail(10).mean())
        avg_vol_5 = float(df["Volume"].tail(5).mean())

        if avg_vol_30 < 300000:
            return None

        df["SMA20"] = df["Close"].rolling(20).mean()
        df["SMA50"] = df["Close"].rolling(50).mean()

        sma20 = float(df["SMA20"].iloc[-1])
        sma50 = float(df["SMA50"].iloc[-1])

        rsi = calculate_rsi(df["Close"])

        high30 = float(df["High"].tail(30).max())
        low30 = float(df["Low"].tail(30).min())

        move5 = ((price - float(df["Close"].iloc[-6])) / float(df["Close"].iloc[-6])) * 100
        move20 = ((price - float(df["Close"].iloc[-21])) / float(df["Close"].iloc[-21])) * 100

        vol_ratio = avg_vol_5 / max(float(df["Volume"].tail(20).mean()), 1)

        news_grade, news_score, headline = get_news(symbol)

        if news_grade == "NEGATIVE":
            return None

        score = 0

        if price > sma20:
            score += 12

        if price > sma50:
            score += 15

        if sma20 >= sma50 * 0.97:
            score += 8

        if vol_ratio >= 1:
            score += 10

        if avg_vol_10 >= avg_vol_30 * 0.9:
            score += 8

        if 42 <= rsi <= 68:
            score += 12

        if price >= high30 * 0.85:
            score += 12

        if -8 <= move5 <= 18:
            score += 8

        if -15 <= move20 <= 45:
            score += 6

        if news_grade == "STRONG":
            score += 15

        elif news_grade == "MEDIUM":
            score += 8

        dollar_volume = price * avg_vol_30

        if dollar_volume >= 5_000_000:
            score += 8
        elif dollar_volume >= 2_000_000:
            score += 5

        return {
            "symbol": symbol,
            "price": price,
            "score": score,
            "avg_vol_30": avg_vol_30,
            "dollar_volume": dollar_volume,
            "rsi": rsi,
            "move5": move5,
            "move20": move20,
            "headline": headline
        }

    except Exception as e:
        print(f"Universe score error {symbol}: {e}", flush=True)
        return None


def build_weekly_universe():
    print("🔎 Building smart weekly investment universe...", flush=True)

    symbols = get_all_alpaca_symbols()
    scored = []

    for i, symbol in enumerate(symbols, start=1):
        try:
            item = score_investment_universe(symbol)

            if item:
                scored.append(item)

            if i % 100 == 0:
                print(f"Checked {i}/{len(symbols)} | accepted: {len(scored)}", flush=True)

            time.sleep(0.03)

        except Exception as e:
            print(f"Weekly universe error {symbol}: {e}", flush=True)
            continue

    scored = sorted(
        scored,
        key=lambda x: (
            x["score"],
            x["dollar_volume"],
            x["avg_vol_30"]
        ),
        reverse=True
    )

    universe = [x["symbol"] for x in scored[:INVESTMENT_SYMBOL_LIMIT]]

    print(f"✅ Weekly universe ready: {len(universe)} symbols", flush=True)

    return universe


def refresh_weekly_universe_light(current_universe):
    print("🔄 Daily light refresh for investment universe...", flush=True)

    refreshed = []
    weak_symbols = []

    for symbol in current_universe:
        item = score_investment_universe(symbol)

        if item and item["score"] >= 55:
            refreshed.append({
                "symbol": symbol,
                "score": item["score"]
            })
        else:
            weak_symbols.append(symbol)

        time.sleep(0.03)

    missing = INVESTMENT_SYMBOL_LIMIT - len(refreshed)

    if missing <= 0:
        refreshed = sorted(refreshed, key=lambda x: x["score"], reverse=True)
        return [x["symbol"] for x in refreshed[:INVESTMENT_SYMBOL_LIMIT]]

    all_symbols = get_all_alpaca_symbols()
    existing = {x["symbol"] for x in refreshed}

    replacements = []

    for symbol in all_symbols:
        if symbol in existing:
            continue

        item = score_investment_universe(symbol)

        if item and item["score"] >= 60:
            replacements.append(item)

        if len(replacements) >= missing * 2:
            break

        time.sleep(0.03)

    replacements = sorted(
        replacements,
        key=lambda x: (
            x["score"],
            x["dollar_volume"],
            x["avg_vol_30"]
        ),
        reverse=True
    )

    for item in replacements[:missing]:
        refreshed.append({
            "symbol": item["symbol"],
            "score": item["score"]
        })

    refreshed = sorted(refreshed, key=lambda x: x["score"], reverse=True)

    print(
        f"✅ Daily refresh done | kept: {len(current_universe) - len(weak_symbols)} | removed: {len(weak_symbols)} | final: {len(refreshed)}",
        flush=True
    )

    return [x["symbol"] for x in refreshed[:INVESTMENT_SYMBOL_LIMIT]]


def ensure_weekly_universe():
    state = load_state()
    now = datetime.now(saudi_tz)
    today = now.strftime("%Y-%m-%d")

    need_weekly_rebuild = False

    if not state.get("weekly_universe"):
        need_weekly_rebuild = True

    if now.weekday() in [3, 4] and state.get("last_weekly_universe") != today:
        need_weekly_rebuild = True

    if need_weekly_rebuild:
        universe = build_weekly_universe()
        state["weekly_universe"] = universe
        state["last_weekly_universe"] = today
        state["last_daily_refresh"] = today
        save_state(state)
        return universe

    if state.get("last_daily_refresh") != today:
        universe = refresh_weekly_universe_light(state.get("weekly_universe", []))
        state["weekly_universe"] = universe
        state["last_daily_refresh"] = today
        save_state(state)
        return universe

    return state.get("weekly_universe", [])


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

        price = float(df["Close"].iloc[-1])

        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None

        avg_vol = float(df["Volume"].tail(30).mean())

        if avg_vol < 300000:
            return None

        df["SMA20"] = df["Close"].rolling(20).mean()
        df["SMA50"] = df["Close"].rolling(50).mean()

        sma20 = float(df["SMA20"].iloc[-1])
        sma50 = float(df["SMA50"].iloc[-1])

        rsi = calculate_rsi(df["Close"])

        high30 = float(df["High"].tail(30).max())
        low30 = float(df["Low"].tail(30).min())

        move5 = ((price - float(df["Close"].iloc[-6])) / float(df["Close"].iloc[-6])) * 100
        vol_ratio = float(df["Volume"].tail(5).mean()) / max(float(df["Volume"].tail(20).mean()), 1)

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

        dollar_volume = price * avg_vol

        if dollar_volume >= 5_000_000:
            score += 8
            reasons.append("سيولة دولارية قوية")

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
    symbols = ensure_weekly_universe()

    results = []

    for i, symbol in enumerate(symbols, start=1):
        r = analyze_stock(symbol)

        if r:
            results.append(r)

        if i % 100 == 0:
            print(f"Monday analysis {i}/{len(symbols)} | results: {len(results)}", flush=True)

        time.sleep(0.03)

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

            price = float(df["Close"].iloc[-1])
            entry = float(p["entry"])

            gain = ((price - entry) / entry) * 100

            if gain > 5 and not p["alerts"].get("start"):
                send_telegram_msg(f"🚀 {symbol} بدأ يتحرك")
                p["alerts"]["start"] = True

            if gain > 10 and not p["alerts"].get("raise"):
                new_stop = entry * 1.02
                send_telegram_msg(f"🔒 ارفع وقف {symbol} إلى {new_stop:.2f}")
                p["alerts"]["raise"] = True

            if price <= float(p["stop"]) and not p["alerts"].get("stop"):
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

        ensure_weekly_universe()

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
