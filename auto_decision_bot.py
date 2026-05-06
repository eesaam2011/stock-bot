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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)
saudi_tz = pytz.timezone("Asia/Riyadh")

watchlist = {}
sent_alerts = {}
active_trades = {}

PRICE_MIN = 0.4
PRICE_MAX = 25

WATCH_MINUTES = 45
SCAN_INTERVAL = 20

MASTER_LIST_FILE = "master_list.json"
NEWS_FILE = "news_signals.json"
BOT2_FINAL_FILE = "bot2_final_results.json"

BOT3_RESULTS_FILE = "bot3_results.json"
BOT3_ACTIVE_TRADES_FILE = "bot3_active_trades.json"

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
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()

    if weekday in [5, 6]:
        return False

    current_minutes = hour * 60 + minute

    # ⛔ من 10:40 مساءً إلى 11:15 مساءً بتوقيت السعودية
    if 22 * 60 + 40 <= current_minutes <= 23 * 60 + 15:
        return False

    # ⛔ من 2:00 ليلًا إلى 10:00 صباحًا بتوقيت السعودية
    if 2 * 60 <= current_minutes <= 10 * 60:
        return False

    return True


def is_trading_time():
    now = datetime.now(saudi_tz)
    weekday = now.weekday()
    hour = now.hour
    minute = now.minute

    if weekday in [5, 6]:
        return False

    if weekday == 0:
        if hour < 8 or (hour == 8 and minute < 30):
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

    symbols = list(dict.fromkeys(symbols))
    return symbols


def get_alpaca_bars(symbol, minutes=120):
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=1)

        bars = api.get_bars(
            symbol,
            tradeapi.TimeFrame.Minute,
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

            day_high = float(df["High"].max())
            vwap = float((df["Close"] * df["Volume"]).sum() / df["Volume"].sum())

            rsi = calculate_rsi(df["Close"])
            instant_rvol = df["Volume"].tail(3).mean() / df["Volume"].mean()
            recent_move = ((cp - df["Close"].iloc[-10]) / df["Close"].iloc[-10]) * 100

            df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
            ema9 = float(df["EMA9"].iloc[-1])

            self_setup = (
                1.8 <= instant_rvol <= 6.0
                and 48 <= rsi <= 70
                and cp > vwap
                and cp > ema9
                and cp >= day_high * 0.965
                and 0.25 <= recent_move <= 2.8
            )

            if self_setup:
                add_to_watchlist(symbol, "فحص ذاتي Bot 3", cp)

            if i % 50 == 0:
                print(f"🔎 Bot 3 scanned {i}/{len(symbols)}", flush=True)

            time.sleep(0.03)

        except Exception as e:
            print(f"Self scan error {symbol}: {e}", flush=True)
            continue


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

        real_breakout = (
            last_close > prev_high
            and prev_close > prev_high * 0.998
            and instant_rvol >= 2.5
        )

        early_entry = (
            instant_rvol >= 2.2
            and 0.5 <= recent_move <= 2.2
            and 50 <= rsi <= 70
            and cp >= day_high * 0.975
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

        overextended = (
            rsi > 75
            or recent_move > 3.2
            or touches >= 3
        )

        advanced_entry = (
            cp > vwap
            and cp > ema9
            and ema9 >= ema20 * 0.995
            and touches < 3
            and not overextended
            and not (distribution_risk and not real_breakout)
            and (real_breakout or early_entry)
        )

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
        if cp >= day_high * 0.975:
            technical_score += 10
        if real_breakout:
            technical_score += 15
        if 52 <= rsi <= 68:
            technical_score += 10
        elif 50 <= rsi < 52:
            technical_score += 5

        if fake_breakout_risk:
            technical_score -= 15

        final_score = technical_score + bot2_bonus + news_bonus
        grade = grade_from_score(final_score)

        if grade not in ["A", "A+", "A++"]:
            return None

        entry = cp
        t1 = entry * 1.02
        t2 = entry * 1.04
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

        result = {
            "symbol": symbol,
            "price": round(entry, 4),
            "grade": grade,
            "final_score": round(final_score, 2),
            "technical_score": round(technical_score, 2),
            "bot2_bonus": bot2_bonus,
            "news_bonus": news_bonus,
            "bot2_grade": bot2_grade,
            "bot2_score": bot2_score,
            "source": source_text,
            "rsi": round(rsi, 2),
            "instant_rvol": round(float(instant_rvol), 2),
            "recent_move": round(float(recent_move), 2),
            "entry": round(entry, 4),
            "target_1": round(t1, 4),
            "target_2": round(t2, 4),
            "stop_loss": round(sl, 4),
            "news_score": news.get("news_score", 0),
            "news_grade": news.get("news_grade", ""),
            "news_headline": news.get("headline", ""),
            "time": time.time(),
            "created_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")
        }

        if not can_send_trade_alerts():
            print(f"🔕 Bot 3 alert muted by schedule: {symbol} | {grade}", flush=True)
            watchlist[symbol]["alerted"] = True
            return result

        msg = (
            f"🧠🔥 *Bot 3 - قرار دخول نهائي*\n\n"
            f"🎫 السهم: `{symbol}`\n"
            f"💰 السعر: {entry:.2f}\n"
            f"🏆 التصنيف: {grade}\n\n"
            f"📡 المصدر:\n"
            f"{source_text}\n\n"
            f"{news_text}"
            f"📊 السكور:\n"
            f"Final Score: {final_score:.1f}\n"
            f"Technical Score: {technical_score:.1f}\n"
            f"Bot2 Bonus: {bot2_bonus}\n"
            f"News Bonus: {news_bonus}\n\n"
            f"📊 القوة:\n"
            f"RSI: {rsi:.1f}\n"
            f"RVOL: {instant_rvol:.2f}x\n"
            f"حركة 10د: {recent_move:.2f}%\n\n"
            f"🛡️ فلتر التصريف: تم تجاوزه ✅\n\n"
            f"🚀 دخول الآن: {entry:.2f}\n"
            f"🎯 هدف 1: {t1:.2f}\n"
            f"🚀 هدف 2: {t2:.2f}\n"
            f"🛑 وقف الخسارة: {sl:.2f}\n\n"
            f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
        )

        send_telegram_msg(msg)

        sent_alerts[symbol] = {
            "time": datetime.now(saudi_tz),
            "grade": grade
        }

        active_trades[symbol] = {
            "entry": entry,
            "t1": t1,
            "t2": t2,
            "sl": sl,
            "grade": grade,
            "time": datetime.now(saudi_tz),
            "slow_alerted": False,
            "run_alerted": False,
            "stop_alerted": False
        }

        watchlist[symbol]["alerted"] = True

        print(f"🧠 BOT 3 ENTRY SENT: {symbol} | {grade}", flush=True)
        return result

    except Exception as e:
        print(f"Check entry error {symbol}: {e}", flush=True)
        return None


def monitor_active_trades():
    global active_trades

    now = datetime.now(saudi_tz)

    for symbol, trade in list(active_trades.items()):
        try:
            df = get_alpaca_bars(symbol, minutes=30)

            if df.empty or len(df) < 5:
                continue

            cp = get_latest_price(symbol, df)

            entry = trade["entry"]
            sl = trade["sl"]
            t1 = trade["t1"]
            t2 = trade["t2"]

            gain_pct = ((cp - entry) / entry) * 100
            age_minutes = (now - trade["time"]).total_seconds() / 60

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

            if age_minutes >= 30 and gain_pct < 0.5 and not trade.get("slow_alerted", False):
                if can_send_trade_alerts():
                    msg = (
                        f"⚠️ *Bot 3 - متابعة الصفقة*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"📊 الحركة بعد الدخول: {gain_pct:.2f}%\n\n"
                        f"⚠️ السهم لم يتحرك بقوة بعد الدخول.\n"
                        f"يفضل تشديد الوقف أو الخروج الجزئي."
                    )
                    send_telegram_msg(msg)

                trade["slow_alerted"] = True

            if gain_pct >= 2 and not trade.get("run_alerted", False):
                new_sl = max(entry, cp * 0.985)

                if can_send_trade_alerts():
                    msg = (
                        f"🚀 *Bot 3 - السهم انطلق بعد الدخول*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"📈 الربح الحالي: {gain_pct:.2f}%\n\n"
                        f"🎯 هدف 1: {t1:.2f}\n"
                        f"🚀 هدف 2: {t2:.2f}\n"
                        f"✅ الوقف المقترح الآن: {new_sl:.2f}"
                    )
                    send_telegram_msg(msg)

                trade["run_alerted"] = True

        except Exception as e:
            print(f"Monitor trade error {symbol}: {e}", flush=True)
            continue


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

        print(f"📊 Bot 3 Watchlist size: {len(watchlist)}", flush=True)

        sorted_watchlist = sorted(
            list(watchlist.items()),
            key=lambda x: x[1].get("priority_score", 0),
            reverse=True
        )

        bot3_results = []

        for symbol, data in sorted_watchlist:
            if not data.get("alerted", False):
                result = check_ready_entry(symbol, data)
                if result:
                    bot3_results.append(result)
                time.sleep(0.05)

        if bot3_results:
            old_results = read_gist_file(BOT3_RESULTS_FILE, default=[])
            if not isinstance(old_results, list):
                old_results = []

            combined = bot3_results + old_results

            deduped = {}
            for r in combined:
                sym = r.get("symbol")
                if not sym:
                    continue
                old = deduped.get(sym)
                if old is None or r.get("time", 0) > old.get("time", 0):
                    deduped[sym] = r

            final_saved = sorted(
                deduped.values(),
                key=lambda x: x.get("time", 0),
                reverse=True
            )[:200]

            save_gist_file(BOT3_RESULTS_FILE, final_saved)

        save_gist_file(BOT3_ACTIVE_TRADES_FILE, active_trades)

        monitor_active_trades()

        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("Main loop error:", e, flush=True)
        time.sleep(10)
