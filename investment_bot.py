import os
import time
import json
import requests
import threading
import pandas as pd
import alpaca_trade_api as tradeapi
from flask import Flask
from datetime import datetime
import pytz

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_INVESTMENT_CHAT_ID = os.getenv("TELEGRAM_INVESTMENT_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)

app = Flask(__name__)
saudi_tz = pytz.timezone("Asia/Riyadh")

STATE_FILE = "investment_state.json"
ACTIVE_TRADES_FILE = "investment_active_trades.json"

PRICE_MIN = 0.5
PRICE_MAX = 10.0
BATCH_SIZE = 100
SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC", "PNC", "COF", "DFS",
    "MET", "PRU", "ALL", "AIG", "AFL", "TRV", "HIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR", "RSI",
    "BUD", "TAP", "STZ", "DEO", "PM", "MO", "BTI",
    "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL",
    "AMC", "CNK", "IMAX",
}

BAD_NAME_KEYWORDS = [
    "etf",
    "fund",
    "trust",
    "warrant",
    "unit",
    "right",
    "preferred",
    "bond",
    "notes",
    "income",
    "index",
    "acquisition",
    "blank check",
    "spac",
    "holdings acquisition",
    "acquisition corp",
    "acquisition corporation",
    "bank",
    "bancorp",
    "credit",
    "lending",
    "loan",
    "mortgage",
    "insurance",
    "casino",
    "gambling",
    "betting",
    "sportsbook",
    "alcohol",
    "beer",
    "wine",
    "tobacco",
    "cannabis",
    "marijuana",
    "hemp",
    "cruise",
    "cinema",
    "movie",
    "theater",
]


def is_clean_symbol(symbol):
    symbol = str(symbol).upper().strip()

    if not symbol:
        return False

    if "." in symbol or "/" in symbol or "-" in symbol or "^" in symbol:
        return False

    if len(symbol) > 5:
        return False

    if not symbol.isalpha():
        return False

    if symbol.endswith(("W", "U", "R", "P", "Q", "Z")):
        return False

    if symbol in SYMBOL_BLACKLIST:
        return False

    return True
    
SCAN_INTERVAL = 300
INSTANT_SCAN_INTERVAL = 30 * 60
MONITOR_INTERVAL = 30 * 60
DAILY_ACCUMULATION_HOUR = 18

MIN_SCORE_FOR_ALERT = 82
MAX_INSTANT_ALERTS = 5
MAX_CANDIDATES_TO_COLLECT = 15

REPEAT_BLOCK_DAYS = 30
CLOSED_PICK_KEEP_DAYS = 3

@app.route("/")
def home():
    return "Investment Bot Running"


def now_saudi():
    return datetime.now(saudi_tz)


def today_str():
    return now_saudi().strftime("%Y-%m-%d")


def gist_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def load_from_gist(filename, default):
    if not GIST_ID or not GITHUB_TOKEN:
        return default

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        r = requests.get(url, headers=gist_headers(), timeout=20)

        if r.status_code != 200:
            print(f"Gist load failed: {r.status_code}", flush=True)
            return default

        files = r.json().get("files", {})

        if filename not in files:
            return default

        content = files[filename].get("content", "")

        if not content:
            return default

        return json.loads(content)

    except Exception as e:
        print(f"load_from_gist error: {e}", flush=True)
        return default


def save_to_gist(filename, data):
    if not GIST_ID or not GITHUB_TOKEN:
        return False

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        payload = {
            "files": {
                filename: {
                    "content": json.dumps(data, ensure_ascii=False, indent=2)
                }
            }
        }

        r = requests.patch(
            url,
            headers=gist_headers(),
            json=payload,
            timeout=20,
        )

        if r.status_code not in [200, 201]:
            print(f"Gist save failed: {r.status_code} {r.text[:200]}", flush=True)
            return False

        return True

    except Exception as e:
        print(f"save_to_gist error: {e}", flush=True)
        return False


def default_state():
    return {
        "last_monitor": None,
        "last_instant_alert": None,
        "last_daily_accumulation_alert": None,
        "active_picks": [],
        "sent_history": [],
    }


def load_state():
    state = load_from_gist(STATE_FILE, None)

    if isinstance(state, dict):
        return state

    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)

            if isinstance(state, dict):
                return state

    except Exception as e:
        print(f"load_state error: {e}", flush=True)

    return default_state()


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"save_state local error: {e}", flush=True)

    save_to_gist(STATE_FILE, state)


def send_telegram_message(message):
    try:
        chat_id = TELEGRAM_INVESTMENT_CHAT_ID or TELEGRAM_CHAT_ID

        if not TELEGRAM_TOKEN or not chat_id:
            print("Telegram token/chat id missing", flush=True)
            return False

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200

    except Exception as e:
        print(f"Telegram error: {e}", flush=True)
        return False


def get_nasdaq_symbols_from_alpaca():
    symbols = []

    try:
        assets = api.list_assets(status="active")

        raw_count = 0
        clean_symbol_count = 0
        blacklist_count = 0
        bad_name_count = 0

        for asset in assets:
            raw_count += 1

            symbol = getattr(asset, "symbol", "")
            name = getattr(asset, "name", "") or ""

            if not symbol:
                continue

            symbol = symbol.upper().strip()

            if not getattr(asset, "tradable", False):
                continue

            if not is_clean_symbol(symbol):
                continue

            clean_symbol_count += 1

            if symbol in SYMBOL_BLACKLIST:
                blacklist_count += 1
                continue

            lowered_name = name.lower()

            if any(k in lowered_name for k in BAD_NAME_KEYWORDS):
                bad_name_count += 1
                continue

            symbols.append(symbol)

        symbols = list(set(symbols))

        print(
            f"✅ Asset filter: raw={raw_count} | clean_symbols={clean_symbol_count} | "
            f"bad_name_rejected={bad_name_count} | final={len(symbols)}",
            flush=True,
        )

    except Exception as e:
        print(f"get_nasdaq_symbols_from_alpaca error: {e}", flush=True)

    return symbols
    
def get_latest_prices_for_symbols(symbols):
    prices = {}

    try:
        snapshots = api.get_snapshots(symbols)

        for symbol, snapshot in snapshots.items():
            price = None

            if snapshot.latest_trade:
                price = getattr(snapshot.latest_trade, "price", None)

            if price is None and snapshot.daily_bar:
                price = getattr(snapshot.daily_bar, "close", None)

            if price is not None:
                prices[symbol] = float(price)

    except Exception as e:
        print(f"get_latest_prices_for_symbols error: {e}", flush=True)

    return prices
def get_latest_prices_batch(symbols):
    prices = {}

    try:
        snapshots = api.get_snapshots(symbols)

        for symbol, snapshot in snapshots.items():
            price = None

            if snapshot.latest_trade:
                price = getattr(snapshot.latest_trade, "price", None)

            if price is None and snapshot.daily_bar:
                price = getattr(snapshot.daily_bar, "close", None)

            if price is not None:
                prices[symbol.upper()] = float(price)

    except Exception as e:
        print(f"get_latest_prices_batch error: {e}", flush=True)

    return prices

def filter_symbols_by_price_before_bars(symbols):
    filtered = []

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        prices = get_latest_prices_for_symbols(batch)

        for symbol in batch:
            price = prices.get(symbol)

            if price is None:
                continue

            if PRICE_MIN <= price <= PRICE_MAX:
                filtered.append(symbol)

        time.sleep(0.2)

    print(
        f"✅ Price pre-filter: {len(filtered)} / {len(symbols)} داخل السعر {PRICE_MIN}-{PRICE_MAX}",
        flush=True,
    )

    return filtered


def get_bars_batch(symbols, timeframe=tradeapi.TimeFrame.Day, limit=240):
    try:
        bars = api.get_bars(
            symbols,
            timeframe,
            limit=limit,
            adjustment="raw",
        ).df

        if bars is None or bars.empty:
            return {}

        bars_map = {}

        if isinstance(bars.index, pd.MultiIndex):
            for symbol in symbols:
                try:
                    df = bars.xs(symbol)
                    if df is not None and not df.empty:
                        bars_map[symbol] = df.copy()
                except Exception:
                    continue
        else:
            if "symbol" in bars.columns:
                for symbol, df in bars.groupby("symbol"):
                    bars_map[symbol] = df.copy()

        return bars_map

    except Exception as e:
        print(f"get_bars_batch error: {e}", flush=True)
        return {}


def calculate_rsi(series, period=14):
    try:
        delta = series.diff()

        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, 0.000001)
        rsi = 100 - (100 / (1 + rs))

        return float(rsi.iloc[-1])

    except Exception:
        return 50.0


def pct_change(new, old):
    try:
        if old == 0:
            return 0
        return ((new - old) / old) * 100
    except Exception:
        return 0


def investment_score(symbol, df=None):
    try:
        if df is None or df.empty:
            return None

        df = df.copy()

        if len(df) < 80:
            return None

        if "close" not in df.columns:
            return None

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        price = float(close.iloc[-1])

        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None

        df["SMA5"] = close.rolling(5).mean()
        df["SMA10"] = close.rolling(10).mean()
        df["SMA20"] = close.rolling(20).mean()
        df["SMA50"] = close.rolling(50).mean()

        sma5 = float(df["SMA5"].iloc[-1])
        sma10 = float(df["SMA10"].iloc[-1])
        sma20 = float(df["SMA20"].iloc[-1])
        sma50 = float(df["SMA50"].iloc[-1])

        rsi = calculate_rsi(close)

        avg_vol_20 = float(volume.tail(20).mean())
        avg_vol_60 = float(volume.tail(60).mean())
        recent_vol = float(volume.tail(5).mean())

        if avg_vol_20 < 150_000:
            return None

        dollar_volume = price * avg_vol_20

        if dollar_volume < 500_000:
            return None

        move_5d = pct_change(price, float(close.iloc[-6]))
        move_10d = pct_change(price, float(close.iloc[-11]))
        move_20d = pct_change(price, float(close.iloc[-21]))
        move_60d = pct_change(price, float(close.iloc[-61]))

        high_20 = float(high.tail(20).max())
        low_20 = float(low.tail(20).min())
        range_20 = pct_change(high_20, low_20)

        close_position = 0.5
        today_range = float(high.iloc[-1] - low.iloc[-1])
        if today_range > 0:
            close_position = float((price - low.iloc[-1]) / today_range)

        volume_ratio = recent_vol / avg_vol_20 if avg_vol_20 else 0
        accumulation_ratio = avg_vol_20 / avg_vol_60 if avg_vol_60 else 0

        trend_ok = (
            price > sma20
            and sma20 > sma50 * 0.97
            and sma5 >= sma10 * 0.98
        )

        slow_runner = (
            price > sma20
            and price > sma50
            and 3 <= move_20d <= 30
            and move_5d >= -2
            and 45 <= rsi <= 64
            and range_20 <= 45
        )

        accumulation_ok = (
            accumulation_ratio >= 1.05
            and volume_ratio >= 0.80
            and close_position >= 0.45
            and move_20d >= 0
        )

        not_late = (
            move_5d <= 18
            and move_10d <= 30
            and rsi <= 62
            and price <= sma20 * 1.25
        )

        technical_strength = (
            close_position >= 0.55
            and price >= high_20 * 0.75
            and move_60d > -35
        )

        score = 0
        reasons = []

        if trend_ok:
            score += 25
            reasons.append("ترند صاعد فوق المتوسطات")

        if slow_runner:
            score += 25
            reasons.append("Slow runner مناسب للاستثمار")

        if accumulation_ok:
            score += 20
            reasons.append("تجميع وسيولة هادئة")

        if not_late:
            score += 15
            reasons.append("ليس دخولاً متأخراً")

        if technical_strength:
            score += 15
            reasons.append("قوة فنية جيدة")

        if rsi > 74:
            score -= 20
            reasons.append("RSI مرتفع")

        if move_5d > 30:
            score -= 20
            reasons.append("ارتفاع سريع قد يكون متأخر")

        if price < sma20 * 0.92:
            score -= 25
            reasons.append("كسر متوسط 20 يوم")

        if score < MIN_SCORE_FOR_ALERT:
            return None

        stop_loss = round(min(sma20 * 0.96, price * 0.90), 4)

        target1 = round(price * 1.25, 4)
        target2 = round(price * 1.75, 4)
        target3 = round(price * 3.00, 4)
        target4 = round(price * 5.00, 4)

        return {
            "symbol": symbol,
            "price": round(price, 4),
            "entry": round(price, 4),
            "score": round(score, 2),
            "rsi": round(rsi, 2),
            "move_5d": round(move_5d, 2),
            "move_10d": round(move_10d, 2),
            "move_20d": round(move_20d, 2),
            "move_60d": round(move_60d, 2),
            "volume_ratio": round(volume_ratio, 2),
            "accumulation_ratio": round(accumulation_ratio, 2),
            "close_position": round(close_position, 2),
            "sma20": round(sma20, 4),
            "sma50": round(sma50, 4),
            "stop_loss": stop_loss,
            "target1": target1,
            "target2": target2,
            "target3": target3,
            "target4": target4,
            "reasons": reasons,
            "alerted_at": now_saudi().isoformat(),
            "status": "active",
            "hit_target1": False,
            "hit_target2": False,
            "hit_target3": False,
            "hit_target4": False,
            "weakness_alerted": False,
            "movement_alerted": False,
            "stop_raised_to": stop_loss,
        }

    except Exception as e:
        print(f"investment_score error {symbol}: {e}", flush=True)
        return None


def get_recent_sent_symbols(state):
    sent_history = state.get("sent_history", [])
    blocked = set()
    cleaned_history = []

    now = now_saudi()

    for item in sent_history:
        try:
            symbol = item.get("symbol")
            sent_at = item.get("sent_at")

            if not symbol or not sent_at:
                continue

            dt = datetime.fromisoformat(sent_at)

            if dt.tzinfo is None:
                dt = saudi_tz.localize(dt)

            age_days = (now - dt.astimezone(saudi_tz)).days

            if age_days < REPEAT_BLOCK_DAYS:
                blocked.add(symbol)
                cleaned_history.append(item)

        except Exception:
            continue

    state["sent_history"] = cleaned_history
    return blocked


def add_to_sent_history(state, picks):
    sent_history = state.get("sent_history", [])

    for p in picks:
        sent_history.append({
            "symbol": p.get("symbol"),
            "sent_at": now_saudi().isoformat(),
            "entry": p.get("entry"),
            "score": p.get("score"),
        })

    state["sent_history"] = sent_history


def add_breakout_candidates_to_active_picks(state, breakout_candidates):
    active_picks = state.get("active_picks", [])

    existing_symbols = {
        str(p.get("symbol", "")).upper()
        for p in active_picks
        if p.get("symbol")
    }

    for p in breakout_candidates:
        symbol = str(p.get("symbol", "")).upper()

        if not symbol:
            continue

        if symbol in existing_symbols:
            continue

        active_picks.append(p)
        existing_symbols.add(symbol)

    state["active_picks"] = active_picks


def send_instant_breakout_alert(breakout_candidates):
    if not breakout_candidates:
        return False

    msg = "🚀 <b>تنبيه استثماري فوري</b>\n"
    msg += "فرص استثمارية جديدة داخل نطاق السعر 0.5 إلى 10 دولار\n\n"

    for p in breakout_candidates:
        msg += f"📌 <b>{p['symbol']}</b>\n"
        msg += f"السعر: {p['price']}\n"
        msg += f"الدرجة: {p['score']}\n"
        msg += f"RSI: {p['rsi']}\n"
        msg += f"حركة 5 أيام: {p['move_5d']}%\n"
        msg += f"حركة 20 يوم: {p['move_20d']}%\n"
        msg += f"وقف الخسارة: {p['stop_loss']}\n"
        msg += f"هدف 1: {p['target1']}\n"
        msg += f"هدف 2: {p['target2']}\n"
        msg += f"هدف 3: {p['target3']}\n"
        msg += f"هدف 4: {p['target4']}\n"

        reasons = p.get("reasons", [])
        if reasons:
            msg += "الأسباب: " + "، ".join(reasons[:4]) + "\n"

        msg += "\n"

    msg += "⚠️ فلتر السعر يخص الفرص الجديدة فقط. بعد دخول السهم في المتابعة يستمر حتى لو تجاوز 10 دولار."

    return send_telegram_message(msg)


def send_daily_accumulation_alert(candidates):
    if not candidates:
        return False

    msg = "📊 <b>تقرير التجميع اليومي للبوت الاستثماري</b>\n\n"

    for p in candidates[:10]:
        msg += f"📌 <b>{p['symbol']}</b> | السعر: {p['price']} | الدرجة: {p['score']}\n"
        msg += f"20D: {p['move_20d']}% | RSI: {p['rsi']} | Vol Ratio: {p['volume_ratio']}\n\n"

    return send_telegram_message(msg)


def scan_for_instant_alerts(state):
    print("🔎 Investment scan started...", flush=True)

    symbols = get_nasdaq_symbols_from_alpaca()

    symbols = filter_symbols_by_price_before_bars(symbols)

    recent_sent = get_recent_sent_symbols(state)
    active_symbols = {
        str(p.get("symbol", "")).upper()
        for p in state.get("active_picks", [])
        if p.get("symbol")
    }

    breakout_candidates = []
    accumulation_candidates = []

    for i in range(0, len(symbols), BATCH_SIZE):
        batch_symbols = symbols[i:i + BATCH_SIZE]
        bars_map = get_bars_batch(batch_symbols, limit=240)

        for symbol, df in bars_map.items():
            symbol = symbol.upper()

            if symbol in recent_sent:
                continue

            if symbol in active_symbols:
                continue

            result = investment_score(symbol, df)

            if not result:
                continue

            accumulation_candidates.append(result)

            if result.get("score", 0) >= MIN_SCORE_FOR_ALERT:
                breakout_candidates.append(result)

        if (
            len(breakout_candidates) >= MAX_CANDIDATES_TO_COLLECT
            and i >= len(symbols) * 0.50
        ):
            break

        time.sleep(0.5)

    breakout_candidates = sorted(
        breakout_candidates,
        key=lambda x: x.get("score", 0),
        reverse=True,
    )[:MAX_INSTANT_ALERTS]

    if breakout_candidates:
        sent = send_instant_breakout_alert(breakout_candidates)

        if sent:
            add_to_sent_history(state, breakout_candidates)
            add_breakout_candidates_to_active_picks(state, breakout_candidates)

            state["last_instant_alert"] = now_saudi().isoformat()
            save_state(state)

    today = today_str()
    if state.get("last_daily_accumulation_alert") != today:
        if now_saudi().hour >= DAILY_ACCUMULATION_HOUR:
            accumulation_candidates = sorted(
                accumulation_candidates,
                key=lambda x: x.get("score", 0),
                reverse=True,
            )

            if accumulation_candidates:
                send_daily_accumulation_alert(accumulation_candidates)
                state["last_daily_accumulation_alert"] = today
                save_state(state)

    print(
        f"✅ Investment scan completed | alerts={len(breakout_candidates)}",
        flush=True,
    )
    state["last_instant_alert"] = now_saudi().isoformat()
    save_state(state)


def get_latest_price(symbol):
    try:
        snapshot = api.get_snapshot(symbol)

        if snapshot.latest_trade:
            return float(snapshot.latest_trade.price)

        if snapshot.daily_bar:
            return float(snapshot.daily_bar.close)

    except Exception as e:
        print(f"get_latest_price error {symbol}: {e}", flush=True)

    return None

def cleanup_closed_active_picks(state):
    active_picks = state.get("active_picks", [])
    cleaned_picks = []
    now = now_saudi()
    changed = False

    for p in active_picks:
        status = p.get("status", "active")

        if status == "active":
            cleaned_picks.append(p)
            continue

        closed_at = p.get("closed_at")

        if not closed_at:
            p["closed_at"] = now.isoformat()
            cleaned_picks.append(p)
            changed = True
            continue

        try:
            dt = datetime.fromisoformat(closed_at)

            if dt.tzinfo is None:
                dt = saudi_tz.localize(dt)

            age_days = (now - dt.astimezone(saudi_tz)).days

            if age_days < CLOSED_PICK_KEEP_DAYS:
                cleaned_picks.append(p)
            else:
                changed = True

        except Exception:
            cleaned_picks.append(p)

    if changed:
        state["active_picks"] = cleaned_picks
        save_state(state)

    return state.get("active_picks", [])
    
def monitor_active_picks(state):
    active_picks = cleanup_closed_active_picks(state)
    
    if not active_picks:
        return

    symbols = [
        str(p.get("symbol", "")).upper()
        for p in active_picks
        if p.get("symbol") and p.get("status") == "active"
    ]

    prices = get_latest_prices_batch(symbols)

    changed = False

    for p in active_picks:
        try:
            symbol = p.get("symbol")

            if not symbol:
                continue

            if p.get("status") != "active":
                continue

            price = prices.get(symbol.upper())

            if price is None:
                continue

            entry = float(p.get("entry", p.get("price", 0)))
            stop_loss = float(p.get("stop_loss", 0))
            target1 = float(p.get("target1", 0))
            target2 = float(p.get("target2", 0))
            target3 = float(p.get("target3", 0))
            target4 = float(p.get("target4", 0))

            gain_pct = pct_change(price, entry)

            p["last_price"] = round(price, 4)
            p["last_gain_pct"] = round(gain_pct, 2)
            p["last_checked_at"] = now_saudi().isoformat()

            if gain_pct >= 4 and not p.get("movement_alerted"):
                p["movement_alerted"] = True
                changed = True

                send_telegram_message(
                    f"🚀 <b>{symbol}</b> بدأ التحرك\n"
                    f"الدخول: {entry}\n"
                    f"السعر الحالي: {round(price, 4)}\n"
                    f"الربح الحالي: {round(gain_pct, 2)}%"
                )

            if price >= target1 and not p.get("hit_target1"):
                p["hit_target1"] = True
                p["stop_raised_to"] = max(entry, float(p.get("stop_raised_to", stop_loss)))
                changed = True

                send_telegram_message(
                    f"✅ <b>{symbol}</b> حقق الهدف 1\n"
                    f"السعر الحالي: {round(price, 4)}\n"
                    f"المقترح: رفع الوقف إلى الدخول {entry}"
                )

            if price >= target2 and not p.get("hit_target2"):
                p["hit_target2"] = True
                p["stop_raised_to"] = max(target1, float(p.get("stop_raised_to", stop_loss)))
                changed = True

                send_telegram_message(
                    f"🔥 <b>{symbol}</b> حقق الهدف 2\n"
                    f"السعر الحالي: {round(price, 4)}\n"
                    f"المقترح: رفع الوقف إلى هدف 1: {target1}"
                )

            if price >= target3 and not p.get("hit_target3"):
                p["hit_target3"] = True
                p["stop_raised_to"] = max(target2, float(p.get("stop_raised_to", stop_loss)))
                changed = True

                send_telegram_message(
                    f"💎 <b>{symbol}</b> حقق الهدف 3\n"
                    f"السعر الحالي: {round(price, 4)}\n"
                    f"المقترح: رفع الوقف إلى هدف 2: {target2}"
                )

            if price >= target4 and not p.get("hit_target4"):
                p["hit_target4"] = True
                p["status"] = "target4_done"
                p["closed_at"] = now_saudi().isoformat()
                changed = True

                send_telegram_message(
                    f"🏁 <b>{symbol}</b> حقق الهدف 4\n"
                    f"السعر الحالي: {round(price, 4)}\n"
                    f"النتيجة: اكتمل المسار الاستثماري بنجاح"
                )

            raised_stop = float(p.get("stop_raised_to", stop_loss))

            if price <= raised_stop:
                p["status"] = "stopped"
                p["closed_at"] = now_saudi().isoformat()
                changed = True

                send_telegram_message(
                    f"🛑 <b>{symbol}</b> وصل الوقف\n"
                    f"السعر الحالي: {round(price, 4)}\n"
                    f"الوقف: {round(raised_stop, 4)}"
                )

                continue

            weakness = (
                gain_pct <= -7
                or price <= entry * 0.93
            )

            if weakness and not p.get("weakness_alerted"):
                p["weakness_alerted"] = True
                changed = True

                send_telegram_message(
                    f"⚠️ <b>{symbol}</b> ضعف فني\n"
                    f"الدخول: {entry}\n"
                    f"السعر الحالي: {round(price, 4)}\n"
                    f"التغير: {round(gain_pct, 2)}%\n"
                    f"المقترح: راقب الوقف أو خفف حسب خطتك"
                )

        except Exception as e:
            print(f"monitor_active_picks error: {e}", flush=True)

    if changed:
        state["active_picks"] = active_picks
        state["last_monitor"] = now_saudi().isoformat()
        save_state(state)


def run_bot():
    state = load_state()

    while True:
        try:
            monitor_active_picks(state)

            last_scan = state.get("last_instant_alert")
            should_scan = True

            if last_scan:
                try:
                    dt = datetime.fromisoformat(last_scan)

                    if dt.tzinfo is None:
                        dt = saudi_tz.localize(dt)

                    seconds_since = (
                        now_saudi() - dt.astimezone(saudi_tz)
                    ).total_seconds()

                    if seconds_since < INSTANT_SCAN_INTERVAL:
                        should_scan = False

                except Exception:
                    should_scan = True

            if should_scan:
                scan_for_instant_alerts(state)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"run_bot error: {e}", flush=True)
            time.sleep(60)


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()

    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
