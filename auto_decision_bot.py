import os
import time
import json
import requests
import pandas as pd
import alpaca_trade_api as tradeapi
from datetime import datetime, timedelta, timezone
import pytz

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)
saudi_tz = pytz.timezone("Asia/Riyadh")

watchlist = {}
sent_alerts = {}
active_trades = {}
last_saved_active_trades = ""
pending_watchlist = {}
last_saved_pending_candidates = ""

PRICE_MIN = 0.4
PRICE_MAX = 25

WATCH_MINUTES = 45
SCAN_INTERVAL = 30
PENDING_MAX_AGE_MINUTES = 90

MASTER_LIST_FILE = "master_list.json"
NEWS_FILE = "news_signals.json"
BOT2_FINAL_FILE = "bot2_final_results.json"
BOT3_ACTIVE_TRADES_FILE = "bot3_active_trades.json"
BOT3_EARLY_CANDIDATES_FILE = "bot3_early_candidates.json"

SELF_SCAN_COUNT = 400


def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram keys missing", flush=True)
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
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

    if 22 * 60 + 40 <= current_minutes <= 23 * 60 + 15:
        return False

    if 2 * 60 <= current_minutes <= 11 * 60:
        return False

    return True


def is_trading_time():
    now = datetime.now(saudi_tz)
    weekday = now.weekday()

    if weekday in [5, 6]:
        return False

    if weekday == 0:
        if now.hour < 8 or (now.hour == 8 and now.minute < 30):
            return False

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


def get_latest_price(symbol, df=None):
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


def load_news_map():
    news = read_gist_file(NEWS_FILE, default=[])
    now_ts = time.time()
    news_map = {}

    if not isinstance(news, list):
        return news_map

    for n in news:
        symbol = n.get("symbol")
        if not symbol:
            continue

        try:
            age = now_ts - float(n.get("time", 0))
        except Exception:
            age = 999999

        if age > 21600:
            continue

        symbol = symbol.upper()
        score = float(n.get("news_score", 0) or 0)

        old = news_map.get(symbol)
        if old is None or score > old.get("news_score", 0):
            news_map[symbol] = {
                "news_score": score,
                "news_grade": n.get("news_grade", ""),
                "news_label": n.get("news_label", ""),
                "headline": n.get("headline", "")
            }

    return news_map


def get_news_bonus(news):
    news_score = float(news.get("news_score", 0) or 0)
    news_grade = news.get("news_grade", "")

    if news_grade == "NEGATIVE":
        return -25

    if news_score >= 18:
        return 15
    elif news_score >= 14:
        return 8
    elif news_score >= 10:
        return 4

    return 0


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
    symbols = load_master_list()[:SELF_SCAN_COUNT]

    if not symbols:
        print("⚠️ Master List empty", flush=True)
        return

    print(f"🔎 Bot 3 self scan: {len(symbols)} symbols", flush=True)

    for i, symbol in enumerate(symbols, start=1):
        try:
            df = get_alpaca_bars(symbol, minutes=120)

            if df.empty or len(df) < 30 or df["Volume"].mean() == 0:
                continue

            cp = get_latest_price(symbol, df)

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

            if self_setup:
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

def add_to_pending(symbol, price, reason=""):

    now = datetime.now(saudi_tz)

    pending_watchlist[symbol] = {
        "symbol": symbol,
        "price": float(price),
        "reason": reason,
        "created_at": now.isoformat()
    }

    print(f"🟡 Added pending candidate: {symbol}", flush=True)

def clean_old_pending_watchlist():

    expired = []
    now = datetime.now(saudi_tz)

    for symbol, data in pending_watchlist.items():

        try:
            created_at = datetime.fromisoformat(
                data["created_at"]
            )

            age_minutes = (
                (now - created_at).total_seconds() / 60
            )

            if age_minutes >= PENDING_MAX_AGE_MINUTES:
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
            "price": data.get("price", 0),
            "reason": data.get("reason", ""),
            "created_at": data.get("created_at", "")
        })

    current_json = json.dumps(
        simplified,
        sort_keys=True
    )

    if current_json != last_saved_pending_candidates:

        save_gist_file(
            BOT3_EARLY_CANDIDATES_FILE,
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


def check_ready_entry(symbol, data):
    try:
        df = get_alpaca_bars(symbol, minutes=120)

        if df.empty or len(df) < 30 or df["Volume"].mean() == 0:
            return None

        cp = get_latest_price(symbol, df)

        day_high = float(df["High"].max())
        price_10min_ago = float(df["Close"].iloc[-10])

        if cp <= 0 or day_high <= 0 or price_10min_ago <= 0:
            return None

        vwap = float((df["Close"] * df["Volume"]).sum() / df["Volume"].sum())

        df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

        ema9 = float(df["EMA9"].iloc[-1])
        ema20 = float(df["EMA20"].iloc[-1])

        rsi = calculate_rsi(df["Close"])
        instant_rvol = df["Volume"].tail(3).mean() / df["Volume"].mean()
        recent_move = ((cp - price_10min_ago) / price_10min_ago) * 100

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
        ready_to_alert = (
            real_breakout
            or (
                instant_rvol >= 2.5
                and recent_move >= 0.65
                and 50 <= rsi <= 70
                and cp > vwap
                and cp > ema9
                and ema9 >= ema20 * 0.995
                and volume_acceleration
                and strong_candle
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
                not strong_candle
                or not vwap_reclaim
                or not ema_reclaim
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

        news_map = load_news_map()
        news = news_map.get(symbol, {})
        news_bonus = get_news_bonus(news)

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
        technical_score += min(instant_rvol * 12, 30)
        technical_score += min(max(recent_move, 0) * 8, 20)

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

        final_score = technical_score + bot2_bonus + news_bonus
        grade = grade_from_score(final_score)

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

        news_text = "📰 الأخبار: لا يوجد خبر قوي حديث\n\n"

        if news_bonus > 0:
            news_text = (
                f"📰 *خبر داعم:* {news.get('news_label', '')}\n"
                f"⭐ News Score: {news.get('news_score', 0):.0f}\n"
                f"🧠 العنوان: {news.get('headline', '')}\n\n"
            )
        elif news_bonus < 0:
            news_text = (
                f"🚨 *خبر سلبي:* {news.get('news_label', '')}\n"
                f"⭐ News Score: {news.get('news_score', 0):.0f}\n"
                f"🧠 العنوان: {news.get('headline', '')}\n\n"
            )

        source_text = data.get("source", "Bot 3")

        if not can_send_trade_alerts():
            print(f"🔕 Bot 3 alert muted by schedule: {symbol} | {grade}", flush=True)
            watchlist[symbol]["alerted"] = True
            return None

        dist_reasons_text = (
            ", ".join(distribution_reasons[:3])
            if distribution_reasons else "None"
        )

        msg = (
            f"🧠🔥 *Bot 3 - قرار دخول نهائي*\n\n"
            f"🎫 السهم: `{symbol}`\n"
            f"💰 السعر: {entry:.2f}\n"
            f"🏆 التصنيف: {grade}\n\n"
            f"📍 مرحلة الدخول: {entry_stage}\n"
            f"📡 المصدر:\n"
            f"{source_text}\n\n"
            f"{news_text}"
            f"📊 السكور:\n"
            f"Final Score: {final_score:.1f}\n"
            f"Technical Score: {technical_score:.1f}\n"
            f"Bot2 Bonus: {bot2_bonus}\n"
            f"News Bonus: {news_bonus}\n"
            f"خصم التصريف: {distribution_penalty}\n\n"
            f"📊 القوة:\n"
            f"RSI: {rsi:.1f}\n"
            f"RVOL: {instant_rvol:.2f}x\n"
            f"حركة 10د: {recent_move:.2f}%\n\n"
            f"🧪 تأكيد الدخول:\n"
            f"Real Breakout: {real_breakout}\n"
            f"Volume Acceleration: {volume_acceleration}\n"
            f"Strong Candle: {strong_candle}\n"
            f"VWAP Reclaim: {vwap_reclaim}\n"
            f"EMA Reclaim: {ema_reclaim}\n"
            f"Ready To Alert: {ready_to_alert}\n"
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

        send_telegram_msg(msg)

        sent_alerts[symbol] = {
            "time": time.time(),
            "grade": grade
        }

        active_trades[symbol] = {
            "entry": entry,
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

            strong_momentum_after_target = (
                cp > vwap
                and cp > ema9
                and instant_rvol >= 1.8
                and 50 <= rsi <= 78
                and close_position >= 0.55
                and upper_wick_pct <= 0.45
            )

            if cp <= sl and not trade.get("stop_alerted", False):
                if can_send_trade_alerts():
                    msg = (
                        f"🛑 *Bot 3 - خروج وقف الخسارة*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"🛑 الوقف: {sl:.2f}"
                    )
                    send_telegram_msg(msg)

                trade["stop_alerted"] = True
                active_trades.pop(symbol, None)
                continue

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

                    send_telegram_msg(msg)

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
                    send_telegram_msg(msg)

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
                    send_telegram_msg(msg)

                trade["target2_alerted"] = True

            if cp >= t3 and not trade.get("target3_alerted", False):
                new_sl = max(t2, cp * 0.985)
                trade["sl"] = round(new_sl, 4)

                if strong_momentum_after_target:
                    action_text = "🔥 السيولة ما زالت داخلة، ممكن الاستمرار بجزء بسيط مع وقف متحرك."
                else:
                    action_text = "✅ وصل هدف 3، يفضل جني أغلب الربح أو رفع الوقف بقوة."

                if can_send_trade_alerts():
                    msg = (
                        f"🔥 *Bot 3 - وصل هدف 3*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"🔥 هدف 3: {t3:.2f}\n"
                        f"📈 الربح الحالي: {gain_pct:.2f}%\n\n"
                        f"✅ الوقف المقترح الآن: {new_sl:.2f}\n\n"
                        f"{action_text}"
                    )
                    send_telegram_msg(msg)

                trade["target3_alerted"] = True

            active_trades[symbol] = trade

        except Exception as e:
            print(f"Monitor trade error {symbol}: {e}", flush=True)
            continue


def load_active_trades_from_gist():
    global active_trades

    saved = read_gist_file(
        BOT3_ACTIVE_TRADES_FILE,
        default={}
    )

    if isinstance(saved, dict):
        active_trades = saved
        print(f"✅ Restored active trades: {len(active_trades)}", flush=True)
    else:
        active_trades = {}
        print("⚠️ No valid active trades found", flush=True)


load_active_trades_from_gist()

print("🧠 BOT 3 DECISION BOT STARTED", flush=True)
send_telegram_msg("🧠 تم تشغيل Bot 3 - القرار النهائي")

while True:
    try:
        if not is_trading_time():
            print("⏸️ خارج وقت التشغيل - Bot 3 ينتظر", flush=True)
            time.sleep(300)
            continue

        update_watchlist_from_bot2()
        self_scan_top_400()
        clean_old_watchlist()
        clean_old_pending_watchlist()
        save_pending_candidates_if_changed()
        print(f"📊 Bot 3 Watchlist size: {len(watchlist)}", flush=True)

        sorted_watchlist = sorted(
            list(watchlist.items()),
            key=lambda x: x[1].get("priority_score", 0),
            reverse=True
        )

        for symbol, data in sorted_watchlist:
            if not data.get("alerted", False):
                check_ready_entry(symbol, data)
                time.sleep(0.05)

        monitor_active_trades()

        current_active_trades = json.dumps(
            active_trades,
            sort_keys=True
        )

        if current_active_trades != last_saved_active_trades:

            save_gist_file(
                BOT3_ACTIVE_TRADES_FILE,
                active_trades
            )

            last_saved_active_trades = current_active_trades

        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("Main loop error:", e, flush=True)
        time.sleep(10)
