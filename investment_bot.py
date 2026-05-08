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

PRICE_MIN = 1.0
PRICE_MAX = 15.0

WEEKEND_TOP_N = 500
NEWS_TOP_N = 50
FINAL_MAX_PICKS = 5
FINAL_MIN_PICKS = 3

SCAN_INTERVAL = 900
MONITOR_INTERVAL = 4 * 60 * 60

THURSDAY_ALERT_HOUR = 16
THURSDAY_ALERT_MINUTE = 0

STATE_FILE = "investment_state.json"
NEWS_FILE = "news_signals.json"

INVESTMENT_500_FILE = "investment_500_candidates.json"
INVESTMENT_NEWS_50_FILE = "investment_news_candidates_50.json"
INVESTMENT_FINAL_FILE = "investment_final_results.json"
INVESTMENT_ACTIVE_FILE = "investment_active_trades.json"

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

        res = requests.get(url, headers=headers, timeout=15)
        data = res.json()

        file_data = data.get("files", {}).get(filename)
        if not file_data:
            return default_value

        content = file_data.get("content", "")
        if not content:
            return default_value

        return json.loads(content)

    except Exception as e:
        print(f"Read gist error {filename}: {e}", flush=True)
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

        res = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    filename: {
                        "content": json.dumps(content_obj, ensure_ascii=False)
                    }
                }
            },
            timeout=15
        )

        if res.status_code not in [200, 201]:
            print(f"Save failed {filename}: {res.text[:300]}", flush=True)
            return

        print(f"Gist saved: {filename}", flush=True)

    except Exception as e:
        print(f"Save gist error {filename}: {e}", flush=True)


def load_state():
    default_state = {
        "cycle_week": "",
        "last_weekend_build": "",
        "last_deep_review": "",
        "last_news_50": "",
        "last_thursday_alert": "",
        "last_monitor": "",
        "weekend_500": [],
        "reviewed_results": [],
        "news_50": [],
        "final_picks": [],
        "active_picks": []
    }

    state = read_gist_file(STATE_FILE, default_state)

    for k, v in default_state.items():
        if k not in state:
            state[k] = v

    return state


def save_state(state):
    save_gist_file(STATE_FILE, state)


BLACKLIST_SYMBOLS = [
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC",
    "MET", "PRU", "ALL", "AIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS",
    "BUD", "TAP", "STZ", "DEO",
    "PM", "MO",
    "CGC", "TLRY", "ACB",
    "NCLH", "CCL", "RCL",
    "AMC", "GPRE", "SKLZ", "PGY", "JELD", "TWO",
]

BLACKLIST_KEYWORDS = [
    "bank", "finance", "capital", "credit", "lending",
    "casino", "gambling", "bet", "betting", "sportsbook",
    "alcohol", "beer", "wine", "spirits", "distillery",
    "tobacco", "cigarette", "smoke",
    "cannabis", "marijuana", "weed", "thc", "cbd",
    "cruise", "cruises", "shipping",
    "adult", "xxx", "porn",
    "cinema", "theater", "movie", "film"
]

BAD_NAME_KEYWORDS = [
    "etf", "fund", "trust", "warrant", "unit", "rights",
    "preferred", "depositary", "notes", "bond",
    "spdr", "ishares", "proshares", "invesco", "vanguard"
]


def is_clean_symbol(symbol):
    if not symbol or not isinstance(symbol, str):
        return False
    if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
        return False
    if len(symbol) > 5:
        return False
    if not symbol.isalpha():
        return False
    return True


def is_blacklisted(symbol, name=""):
    text = f"{symbol} {name}".lower()

    if symbol.upper() in BLACKLIST_SYMBOLS:
        return True

    for word in BLACKLIST_KEYWORDS:
        if word in text:
            return True

    for word in BAD_NAME_KEYWORDS:
        if word in text:
            return True

    return False


def get_nasdaq_symbols_from_alpaca():
    try:
        assets = api.list_assets(status="active")
        symbols = []

        for i, asset in enumerate(assets, start=1):

            symbol = getattr(asset, "symbol", "")
            name = getattr(asset, "name", "")
            exchange = getattr(asset, "exchange", "")
            asset_class = getattr(asset, "asset_class", "")
            tradable = getattr(asset, "tradable", False)

                
            if not is_clean_symbol(symbol):
                continue

            if is_blacklisted(symbol, name):
                continue

            symbols.append(symbol.upper())

        symbols = list(dict.fromkeys(symbols))
        print(f"✅ Nasdaq clean symbols: {len(symbols)}", flush=True)
        return symbols

    except Exception as e:
        print("Alpaca asset list error:", e, flush=True)
        return []


def get_daily_bars(symbol, days=240):
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
    if len(close) < period + 1:
        return 50

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
        if str(n.get("symbol", "")).upper() != symbol.upper():
            continue

        try:
            age = now - float(n.get("time", 0))
        except Exception:
            age = 999999

        if age > 7 * 86400:
            continue

        if n.get("news_grade") == "NEGATIVE":
            return "NEGATIVE", float(n.get("news_score", 0) or 0), n.get("headline", "")

        score = float(n.get("news_score", 0) or 0)

        if score > best_score:
            best = n
            best_score = score

    if best:
        return best.get("news_grade", "NONE"), best_score, best.get("headline", "")

    return "NONE", 0, ""


def investment_score(symbol, use_news=False, deep=False):
    try:
        df = get_daily_bars(symbol, days=240)

        if df.empty or len(df) < 80:
            return None

        close = df["Close"]
        volume = df["Volume"]

        price = float(close.iloc[-1])

        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None

        avg_vol_30 = float(volume.tail(30).mean())
        avg_vol_10 = float(volume.tail(10).mean())
        avg_vol_5 = float(volume.tail(5).mean())

        dollar_volume = price * avg_vol_30

        if avg_vol_30 < 250_000:
            return None

        if dollar_volume < 500_000:
            return None

        df["SMA20"] = close.rolling(20).mean()
        df["SMA50"] = close.rolling(50).mean()

        sma20 = float(df["SMA20"].iloc[-1])
        sma50 = float(df["SMA50"].iloc[-1])

        rsi = calculate_rsi(close)

        high30 = float(df["High"].tail(30).max())
        low30 = float(df["Low"].tail(30).min())
        high60 = float(df["High"].tail(60).max())

        recent_range_15 = ((close.tail(15).max() - close.tail(15).min()) / price) * 100
        distance_sma20 = ((price - sma20) / sma20) * 100 if sma20 > 0 else 99

        move5 = ((price - float(close.iloc[-6])) / float(close.iloc[-6])) * 100
        move10 = ((price - float(close.iloc[-11])) / float(close.iloc[-11])) * 100
        move20 = ((price - float(close.iloc[-21])) / float(close.iloc[-21])) * 100

        vol_ratio_5_20 = avg_vol_5 / max(float(volume.tail(20).mean()), 1)
        vol_ratio_10_30 = avg_vol_10 / max(avg_vol_30, 1)

        higher_lows = (
            float(close.iloc[-1]) > float(close.iloc[-5]) > float(close.iloc[-10])
        )

        trend_ok = (
            price > sma20 * 0.98
            and sma20 >= sma50 * 0.97
        )

        not_extended = (
            distance_sma20 <= 8
            and move10 <= 15
            and move20 <= 25
        )

        accumulation = recent_range_15 <= 14

        near_breakout = (
            price >= high30 * 0.88
            or price >= high60 * 0.82
        )

        volume_improving = (
            vol_ratio_5_20 >= 0.9
            or vol_ratio_10_30 >= 1.0
        )

        if not trend_ok:
            return None

        if not not_extended:
            return None

        if not accumulation:
            return None

        if not near_breakout:
            return None

        if deep:
            if not volume_improving:
                return None
            if not (43 <= rsi <= 66):
                return None
        else:
            if not (40 <= rsi <= 68):
                return None

        news_grade, news_score, headline = get_news(symbol) if use_news else ("NONE", 0, "")

        if news_grade == "NEGATIVE":
            return None

        score = 0
        reasons = []

        if price > sma20:
            score += 12
            reasons.append("فوق SMA20")

        if price > sma50:
            score += 12
            reasons.append("فوق SMA50")

        if sma20 >= sma50 * 0.99:
            score += 8
            reasons.append("SMA20 قريب/فوق SMA50")

        if 45 <= rsi <= 62:
            score += 12
            reasons.append("RSI صحي")

        if recent_range_15 <= 10:
            score += 14
            reasons.append("تجميع هادئ")
        elif recent_range_15 <= 14:
            score += 8
            reasons.append("نطاق مقبول")

        if volume_improving:
            score += 12
            reasons.append("تحسن سيولة تدريجي")

        if price >= high30 * 0.92:
            score += 14
            reasons.append("قريب من اختراق 30 يوم")
        elif price >= high30 * 0.88:
            score += 8
            reasons.append("قريب من مقاومة")

        if higher_lows:
            score += 8
            reasons.append("قيعان صاعدة")

        if -5 <= move5 <= 8:
            score += 8
            reasons.append("غير متضخم قصيرًا")

        if -10 <= move20 <= 18:
            score += 8
            reasons.append("غير متأخر شهريًا")

        if dollar_volume >= 5_000_000:
            score += 8
            reasons.append("سيولة دولارية قوية")
        elif dollar_volume >= 1_500_000:
            score += 5
            reasons.append("سيولة مناسبة")

        news_bonus = 0

        if use_news:
            if news_grade == "STRONG" and news_score >= 18:
                news_bonus = 18
                reasons.append("خبر قوي جدًا")
            elif news_grade == "STRONG":
                news_bonus = 12
                reasons.append("خبر قوي")
            elif news_grade == "MEDIUM":
                news_bonus = 6
                reasons.append("خبر متوسط")

        score += news_bonus

        if deep:
            score += 5

        stop = min(price * 0.90, low30 * 0.98)
        t1 = price * 1.15
        t2 = price * 1.35

        return {
            "symbol": symbol,
            "price": round(price, 4),
            "entry": round(price, 4),
            "stop": round(stop, 4),
            "t1": round(t1, 4),
            "t2": round(t2, 4),
            "score": round(score, 2),
            "rsi": round(rsi, 2),
            "avg_vol_30": round(avg_vol_30, 2),
            "dollar_volume": round(dollar_volume, 2),
            "move5": round(move5, 2),
            "move10": round(move10, 2),
            "move20": round(move20, 2),
            "distance_sma20": round(distance_sma20, 2),
            "range15": round(recent_range_15, 2),
            "near_breakout": near_breakout,
            "volume_improving": volume_improving,
            "news_grade": news_grade,
            "news_score": news_score,
            "headline": headline,
            "news_bonus": news_bonus,
            "reasons": reasons,
            "alerts": {},
            "time": time.time(),
            "created_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")
        }

    except Exception as e:
        print(f"Investment score error {symbol}: {e}", flush=True)
        return None


def build_weekend_500():
    print("🔎 Investment Bot - Building weekend 500 from Nasdaq...", flush=True)

    symbols = get_nasdaq_symbols_from_alpaca()
    print(f"DEBUG symbols loaded: {len(symbols)}", flush=True)

    if not symbols:
        print("❌ No symbols loaded from Alpaca. Check exchange filter or API keys.", flush=True)
        return []
        
    scored = []

    for i, symbol in enumerate(symbols, start=1):
        item = investment_score(symbol, use_news=False, deep=False)

        if item:
            scored.append(item)

        if i % 100 == 0:
            print(f"Weekend checked {i}/{len(symbols)} | accepted: {len(scored)}", flush=True)

        time.sleep(0.03)

    scored = sorted(
        scored,
        key=lambda x: (x["score"], x["dollar_volume"], x["avg_vol_30"]),
        reverse=True
    )

    top500 = scored[:WEEKEND_TOP_N]

    save_gist_file(INVESTMENT_500_FILE, top500)

    state = load_state()
    state["weekend_500"] = top500
    state["last_weekend_build"] = datetime.now(saudi_tz).strftime("%Y-%m-%d")
    state["reviewed_results"] = []
    state["news_50"] = []
    state["final_picks"] = []
    save_state(state)

    print(f"✅ Investment Bot - Weekend 500 ready: {len(top500)}", flush=True)
    return top500


def deep_review_500():
    state = load_state()
    candidates = state.get("weekend_500", [])

    if not candidates:
        candidates = read_gist_file(INVESTMENT_500_FILE, [])

    symbols = [x["symbol"] for x in candidates if x.get("symbol")]

    print(f"🔍 Investment Bot - Deep review 500: {len(symbols)}", flush=True)

    reviewed = []

    for i, symbol in enumerate(symbols, start=1):
        item = investment_score(symbol, use_news=False, deep=True)

        if item:
            reviewed.append(item)

        if i % 50 == 0:
            print(f"Deep reviewed {i}/{len(symbols)} | accepted: {len(reviewed)}", flush=True)

        time.sleep(0.04)

    reviewed = sorted(
        reviewed,
        key=lambda x: (x["score"], x["dollar_volume"], x["avg_vol_30"]),
        reverse=True
    )

    state["reviewed_results"] = reviewed
    state["last_deep_review"] = datetime.now(saudi_tz).strftime("%Y-%m-%d")
    save_state(state)

    print(f"✅ Investment Bot - Deep review done: {len(reviewed)}", flush=True)
    return reviewed


def prepare_news_50():
    state = load_state()
    reviewed = state.get("reviewed_results", [])

    if not reviewed:
        reviewed = deep_review_500()

    news_50 = sorted(
        reviewed,
        key=lambda x: (x["score"], x["dollar_volume"]),
        reverse=True
    )[:NEWS_TOP_N]

    payload = [
        {
            "symbol": x["symbol"],
            "score": x["score"],
            "price": x["price"],
            "reason": "Investment Bot Wednesday Top 50",
            "time": time.time()
        }
        for x in news_50
    ]

    save_gist_file(INVESTMENT_NEWS_50_FILE, payload)

    state["news_50"] = news_50
    state["last_news_50"] = datetime.now(saudi_tz).strftime("%Y-%m-%d")
    save_state(state)

    print(f"✅ Investment Bot - News 50 prepared: {len(news_50)}", flush=True)
    return news_50


def get_target_timing():
    now = datetime.now(saudi_tz)
    return {
        "target1_date": (now + timedelta(days=14)).strftime("%Y-%m-%d"),
        "target2_date": (now + timedelta(days=30)).strftime("%Y-%m-%d"),
        "target1_duration": "تقريبًا خلال أسبوعين",
        "target2_duration": "تقريبًا خلال شهر"
    }


def build_final_picks():
    state = load_state()
    news_50 = state.get("news_50", [])

    if not news_50:
        news_50 = read_gist_file(INVESTMENT_NEWS_50_FILE, [])

    symbols = [x["symbol"] for x in news_50 if x.get("symbol")]

    print(f"🏁 Investment Bot - Building final picks from news 50: {len(symbols)}", flush=True)

    final = []

    for symbol in symbols:
        item = investment_score(symbol, use_news=True, deep=True)

        if item:
            final.append(item)

        time.sleep(0.04)

    final = sorted(
        final,
        key=lambda x: (x["score"], x["news_bonus"], x["dollar_volume"]),
        reverse=True
    )

    picks = final[:FINAL_MAX_PICKS]

    timing = get_target_timing()

    for p in picks:
        p["target1_date"] = timing["target1_date"]
        p["target2_date"] = timing["target2_date"]
        p["target1_duration"] = timing["target1_duration"]
        p["target2_duration"] = timing["target2_duration"]
        p["alerts"] = {}

    save_gist_file(INVESTMENT_FINAL_FILE, picks)

    state["final_picks"] = picks
    state["active_picks"] = picks
    save_state(state)

    return picks


def send_thursday_alert():
    picks = build_final_picks()

    if len(picks) < FINAL_MIN_PICKS:
        send_telegram_msg(
            "📈 *Investment Bot - البوت الاستثماري*\n\n"
            "📉 لا توجد فرص استثمارية كافية هذا الأسبوع."
        )
        return

    msg = "📈 *Investment Bot - البوت الاستثماري*\n"
    msg += "🏆 *أفضل فرص استثمارية لمدة 2 إلى 4 أسابيع*\n\n"

    for r in picks:
        msg += (
            f"🎫 `{r['symbol']}`\n"
            f"💰 دخول: {r['entry']:.2f}\n"
            f"🛑 وقف: {r['stop']:.2f}\n"
            f"🎯 هدف 1: {r['t1']:.2f}\n"
            f"📅 هدف 1: {r['target1_date']} - {r['target1_duration']}\n"
            f"🚀 هدف 2: {r['t2']:.2f}\n"
            f"📅 هدف 2: {r['target2_date']} - {r['target2_duration']}\n"
            f"⭐ الدرجة: {r['score']:.1f}\n"
            f"📊 RSI: {r['rsi']:.1f}\n"
            f"📦 Range15: {r['range15']:.1f}%\n"
            f"🧠 الأسباب: {', '.join(r['reasons'][:5])}\n"
        )

        if r.get("headline"):
            msg += f"📰 {r['headline']}\n"

        msg += f"🔗 https://www.tradingview.com/chart/?symbol={r['symbol']}\n\n"

    send_telegram_msg(msg)

    state = load_state()
    state["last_thursday_alert"] = datetime.now(saudi_tz).strftime("%Y-%m-%d")
    state["active_picks"] = picks
    save_state(state)
    save_gist_file(INVESTMENT_ACTIVE_FILE, picks)


def monitor_active_picks():
    state = load_state()
    picks = state.get("active_picks", [])

    if not picks:
        return

    updated = []

    for p in picks:
        try:
            symbol = p["symbol"]
            df = get_daily_bars(symbol, days=90)

            if df.empty or len(df) < 30:
                updated.append(p)
                continue

            price = float(df["Close"].iloc[-1])
            entry = float(p["entry"])
            stop = float(p["stop"])
            t1 = float(p["t1"])
            t2 = float(p["t2"])

            gain = ((price - entry) / entry) * 100

            df["SMA20"] = df["Close"].rolling(20).mean()
            sma20 = float(df["SMA20"].iloc[-1])
            rsi = calculate_rsi(df["Close"])

            news_grade, news_score, headline = get_news(symbol)

            alerts = p.get("alerts", {})

            if news_grade == "NEGATIVE" and not alerts.get("negative_news"):
                send_telegram_msg(
                    f"📈 *Investment Bot - البوت الاستثماري*\n"
                    f"🚨 *خبر سلبي على صفقة استثمارية*\n\n"
                    f"🎫 `{symbol}`\n"
                    f"💰 السعر الحالي: {price:.2f}\n"
                    f"📰 {headline}\n\n"
                    f"يفضل مراجعة الصفقة فورًا."
                )
                alerts["negative_news"] = True

            if price <= stop and not alerts.get("stop"):
                send_telegram_msg(
                    f"📈 *Investment Bot - البوت الاستثماري*\n"
                    f"🛑 *كسر وقف الخسارة*\n\n"
                    f"🎫 `{symbol}`\n"
                    f"💰 السعر الحالي: {price:.2f}\n"
                    f"🚀 الدخول: {entry:.2f}\n"
                    f"🛑 الوقف: {stop:.2f}"
                )
                alerts["stop"] = True

            if gain >= 5 and not alerts.get("start"):
                send_telegram_msg(
                    f"📈 *Investment Bot - البوت الاستثماري*\n"
                    f"🚀 *بدأ التحرك*\n\n"
                    f"🎫 `{symbol}`\n"
                    f"💰 السعر الحالي: {price:.2f}\n"
                    f"📈 الربح الحالي: {gain:.2f}%"
                )
                alerts["start"] = True

            if gain >= 10 and not alerts.get("raise_stop"):
                new_stop = max(entry * 1.02, price * 0.92)

                send_telegram_msg(
                    f"📈 *Investment Bot - البوت الاستثماري*\n"
                    f"🔒 *رفع وقف مقترح*\n\n"
                    f"🎫 `{symbol}`\n"
                    f"💰 السعر الحالي: {price:.2f}\n"
                    f"📈 الربح الحالي: {gain:.2f}%\n\n"
                    f"✅ الوقف المقترح الآن: {new_stop:.2f}"
                )

                p["stop"] = round(new_stop, 4)
                alerts["raise_stop"] = True

            if price >= t1 and not alerts.get("target1"):
                new_stop = max(entry * 1.05, price * 0.90)

                send_telegram_msg(
                    f"📈 *Investment Bot - البوت الاستثماري*\n"
                    f"🎯 *وصل هدف 1*\n\n"
                    f"🎫 `{symbol}`\n"
                    f"💰 السعر الحالي: {price:.2f}\n"
                    f"🎯 هدف 1: {t1:.2f}\n\n"
                    f"✅ الوقف المقترح بعد الهدف: {new_stop:.2f}"
                )

                p["stop"] = round(new_stop, 4)
                alerts["target1"] = True

            if price >= t2 and not alerts.get("target2"):
                send_telegram_msg(
                    f"📈 *Investment Bot - البوت الاستثماري*\n"
                    f"🚀 *وصل هدف 2*\n\n"
                    f"🎫 `{symbol}`\n"
                    f"💰 السعر الحالي: {price:.2f}\n"
                    f"🚀 هدف 2: {t2:.2f}\n\n"
                    f"يفضل جني جزء كبير من الربح."
                )
                alerts["target2"] = True

            weakness = (
                price < sma20 * 0.97
                or rsi < 40
            )

            if weakness and not alerts.get("weakness"):
                send_telegram_msg(
                    f"📈 *Investment Bot - البوت الاستثماري*\n"
                    f"⚠️ *ضعف فني*\n\n"
                    f"🎫 `{symbol}`\n"
                    f"💰 السعر الحالي: {price:.2f}\n"
                    f"📊 RSI: {rsi:.1f}\n"
                    f"📉 SMA20: {sma20:.2f}\n\n"
                    f"يفضل المراقبة أو تخفيف الكمية."
                )
                alerts["weakness"] = True

            p["current_price"] = round(price, 4)
            p["current_gain_pct"] = round(gain, 2)
            p["alerts"] = alerts
            p["last_monitor"] = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")

            updated.append(p)

        except Exception as e:
            print(f"Monitor error {p.get('symbol')}: {e}", flush=True)
            updated.append(p)

    state["active_picks"] = updated
    state["last_monitor"] = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    save_gist_file(INVESTMENT_ACTIVE_FILE, updated)


def load_active_picks_from_gist():
    state = load_state()

    saved_active = read_gist_file(
        INVESTMENT_ACTIVE_FILE,
        []
    )

    if isinstance(saved_active, list) and len(saved_active) > 0:
        state["active_picks"] = saved_active
        save_state(state)

        print(
            f"✅ Restored investment active picks: {len(saved_active)}",
            flush=True
        )

    else:
        print(
            "⚠️ No investment active picks found",
            flush=True
        )


def run_scheduler():
    now = datetime.now(saudi_tz)
    today = now.strftime("%Y-%m-%d")
    state = load_state()

    if now.weekday() in [4, 5, 6]:
        if state.get("last_weekend_build") != today:
            build_weekend_500()

    if now.weekday() in [0, 1]:
        if state.get("last_deep_review") != today:
            deep_review_500()

    if now.weekday() == 2 and now.hour >= 8:
        if state.get("last_news_50") != today:
            prepare_news_50()

    if (
        now.weekday() == 3
        and now.hour == THURSDAY_ALERT_HOUR
        and now.minute >= THURSDAY_ALERT_MINUTE
        and state.get("last_thursday_alert") != today
    ):
        send_thursday_alert()

    last_monitor = state.get("last_monitor", "")
    should_monitor = True

    if last_monitor:
        try:
            last_dt = saudi_tz.localize(datetime.strptime(last_monitor, "%Y-%m-%d %H:%M:%S"))
            should_monitor = (now - last_dt).total_seconds() >= MONITOR_INTERVAL
        except Exception:
            should_monitor = True

    if should_monitor:
        monitor_active_picks()


threading.Thread(target=run_web_server, daemon=True).start()

load_active_picks_from_gist()

print("📈 Investment Bot Started", flush=True)
send_telegram_msg("📈 *Investment Bot - البوت الاستثماري*\n\nتم تشغيل النظام الأسبوعي الجديد.")

while True:
    try:
        run_scheduler()
        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("Main error:", e, flush=True)
        time.sleep(30)
