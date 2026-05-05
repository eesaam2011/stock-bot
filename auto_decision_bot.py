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

PRICE_MIN = 0.5
PRICE_MAX = 25
WATCH_MINUTES = 45
SCAN_INTERVAL = 20
NEWS_FILE = "news_signals.json"

RADAR_BATCH_SIZE = 650
MASTER_LIST_FILE = "master_list.json"


def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram keys missing", flush=True)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
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


def is_trading_time():
    now = datetime.now(saudi_tz)
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()

    if weekday in [5, 6]:
        return False

    if weekday == 0:
        if hour < 8 or (hour == 8 and minute < 30):
            return False

    return True


def read_gist_file(filename):
    if not GIST_ID or not GITHUB_TOKEN:
        return []

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
            return []

        content = file_data.get("content", "[]")

        try:
            return json.loads(content)
        except Exception:
            return []

    except Exception as e:
        print(f"Gist read error ({filename}):", e, flush=True)
        return []


def load_master_list():
    data = read_gist_file(MASTER_LIST_FILE)

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


def read_gist_signals():
    signals = read_gist_file("signals.json")
    now_ts = time.time()

    return [
        s for s in signals
        if now_ts - float(s.get("time", 0)) < 1800
    ]


def get_stock_news(symbol):
    news = read_gist_file(NEWS_FILE)
    now_ts = time.time()

    best_news = None
    best_score = -999

    for n in news:
        if n.get("symbol") != symbol:
            continue

        age = now_ts - float(n.get("time", 0))

        if age > 21600:
            continue

        grade = n.get("news_grade")
        score = float(n.get("news_score", 0))

        if grade == "NEGATIVE":
            return {
                "has_news": False,
                "has_strong_news": False,
                "has_negative_news": True,
                "headline": n.get("headline", ""),
                "label": n.get("news_label", "🔴 خبر سلبي"),
                "score": score
            }

        if grade == "STRONG" and score >= 7 and score > best_score:
            best_news = n
            best_score = score

    if best_news:
        return {
            "has_news": True,
            "has_strong_news": True,
            "has_negative_news": False,
            "headline": best_news.get("headline", ""),
            "label": best_news.get("news_label", "🔥 خبر إيجابي قوي"),
            "score": best_score
        }

    return {
        "has_news": False,
        "has_strong_news": False,
        "has_negative_news": False,
        "headline": "",
        "label": "",
        "score": 0
    }


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

        df = df[needed].dropna()
        df = df.tail(minutes)

        return df

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


def get_base_list():
    symbols = load_master_list()

    if not symbols:
        print("⚠️ Master List empty or not found", flush=True)
        return []

    return symbols


def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=period).mean()

    if loss.iloc[-1] == 0:
        return 100

    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


def add_to_watchlist(symbol, source, price=0):
    now = datetime.now(saudi_tz)

    source_score = 1

    if "البوت الثاني" in source:
        source_score = 2
    elif "البوت الأول" in source:
        source_score = 1
    elif "رادار مبكر ذاتي" in source:
        source_score = 1

    if symbol not in watchlist:
        watchlist[symbol] = {
            "source": source,
            "sources": [source],
            "priority_score": source_score,
            "first_price": float(price) if price else 0,
            "created_at": now,
            "alerted": False
        }

        print(f"🧠 Added watchlist: {symbol} | source: {source}", flush=True)

    else:
        if source not in watchlist[symbol].get("sources", []):
            watchlist[symbol]["sources"].append(source)
            watchlist[symbol]["priority_score"] = (
                watchlist[symbol].get("priority_score", 0) + source_score
            )

        watchlist[symbol]["source"] = " + ".join(
            watchlist[symbol].get("sources", [source])
        )

        print(
            f"🧠 Updated watchlist: {symbol} | sources: {watchlist[symbol]['source']} | priority: {watchlist[symbol]['priority_score']}",
            flush=True
        )


def update_watchlist_from_gist():
    signals = read_gist_signals()

    for s in signals:
        symbol = s.get("symbol")
        price = s.get("price", 0)
        source = s.get("source")

        if not symbol:
            continue

        if source == "main_bot":
            add_to_watchlist(symbol, "رادار مبكر (البوت الأول)", price)

        elif source == "safe_bot":
            add_to_watchlist(symbol, "تأكيد قوي (البوت الثاني)", price)


def rank_symbols_by_activity(symbols, max_symbols=650):
    ranked = []

    for symbol in symbols:
        try:
            df = get_alpaca_bars(symbol, minutes=30)

            if df.empty or len(df) < 10 or df["Volume"].sum() == 0:
                continue

            cp = float(df["Close"].iloc[-1])
            old_price = float(df["Close"].iloc[-10])

            if cp <= 0 or old_price <= 0:
                continue

            if not (PRICE_MIN <= cp <= PRICE_MAX):
                continue

            change_pct = ((cp - old_price) / old_price) * 100
            volume = float(df["Volume"].sum())

            activity_score = abs(change_pct) + (volume / 1_000_000)

            ranked.append({
                "symbol": symbol,
                "activity_score": activity_score
            })

            time.sleep(0.01)

        except Exception as e:
            print(f"Rank error {symbol}: {e}", flush=True)
            continue

    ranked = sorted(
        ranked,
        key=lambda x: x["activity_score"],
        reverse=True
    )

    return [x["symbol"] for x in ranked[:max_symbols]]


def update_watchlist_from_radar():
    symbols = get_base_list()

    if not symbols:
        return

    ranked_symbols = rank_symbols_by_activity(
        symbols,
        max_symbols=RADAR_BATCH_SIZE
    )

    print(f"🔥 Top active symbols selected: {len(ranked_symbols)}", flush=True)

    for symbol in ranked_symbols:
        try:
            df = get_alpaca_bars(symbol, minutes=120)

            if df.empty or len(df) < 25 or df["Volume"].mean() == 0:
                continue

            cp = get_latest_price(symbol, df)
            day_high = float(df["High"].max())
            vwap = float((df["Close"] * df["Volume"]).sum() / df["Volume"].sum())

            rsi = calculate_rsi(df["Close"])
            instant_rvol = df["Volume"].tail(3).mean() / df["Volume"].mean()
            recent_move = ((cp - df["Close"].iloc[-10]) / df["Close"].iloc[-10]) * 100

            df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
            ema9 = float(df["EMA9"].iloc[-1])

            early_setup = (
                PRICE_MIN <= cp <= PRICE_MAX
                and 1.5 <= instant_rvol <= 5.0
                and 45 <= rsi <= 66
                and cp > vwap
                and cp > ema9
                and cp >= day_high * 0.965
                and 0.15 <= recent_move < 2.5
            )

            if early_setup:
                add_to_watchlist(symbol, "رادار مبكر ذاتي", cp)

            time.sleep(0.03)

        except Exception as e:
            print(f"Radar error {symbol}: {e}", flush=True)
            continue


def clean_old_watchlist():
    now = datetime.now(saudi_tz)

    expired = []

    for symbol, data in watchlist.items():
        if now - data["created_at"] > timedelta(minutes=WATCH_MINUTES):
            expired.append(symbol)

    for symbol in expired:
        watchlist.pop(symbol, None)


def check_ready_entry(symbol, data):
    try:
        df = get_alpaca_bars(symbol, minutes=120)

        if df.empty or len(df) < 30 or df["Volume"].mean() == 0:
            return

        cp = get_latest_price(symbol, df)

        day_high = float(df["High"].max())
        price_10min_ago = float(df["Close"].iloc[-10])

        if cp <= 0 or day_high <= 0 or price_10min_ago <= 0:
            return

        vwap = float((df["Close"] * df["Volume"]).sum() / df["Volume"].sum())

        df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
        ema9 = float(df["EMA9"].iloc[-1])

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
            return

        upper_wick_pct = (last_high - last_close) / candle_range
        close_position = (last_close - last_low) / candle_range

        real_breakout = (
            last_close > prev_high
            and prev_close > prev_high * 0.998
            and instant_rvol >= 2.5
        )

        overextended = (
            rsi > 75
            or recent_move > 3.2
            or touches >= 3
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

        distribution_risk = (
            fake_breakout_risk
            or repeated_rejection
        )

        advanced_entry = (
            cp > vwap
            and cp > ema9
            and touches < 3
            and not overextended
            and not (distribution_risk and not real_breakout)
            and (real_breakout or early_entry)
        )

        if not advanced_entry:
            return

        if sent_alerts.get(symbol):
            return

        news_info = get_stock_news(symbol)

        has_strong_news = news_info["has_strong_news"]
        has_negative_news = news_info["has_negative_news"]

        entry = cp
        t1 = entry * 1.02
        t2 = entry * 1.04
        sl = entry * 0.985

        source_text = data.get("source", "رادار مبكر")

        if "البوت الثاني" in source_text and has_strong_news:
            signal_grade = "A++ 🔥🔥"
            grade_note = "أقوى نوع: تأكيد من البوت الثاني + خبر قوي"
        elif "البوت الثاني" in source_text:
            signal_grade = "A+ 🔥"
            grade_note = "أقوى نوع: تأكيد من البوت الثاني + متابعة ذكية"
        elif "البوت الأول" in source_text and has_strong_news:
            signal_grade = "A+ 📰🔥"
            grade_note = "رادار مبكر + خبر قوي"
        elif "البوت الأول" in source_text:
            signal_grade = "A ✅"
            grade_note = "جيد جدًا: رادار مبكر + تأكيد دخول"
        elif has_strong_news:
            signal_grade = "A 📰"
            grade_note = "إشارة ذاتية مدعومة بخبر قوي"
        else:
            signal_grade = "B ⚠️"
            grade_note = "إشارة ذاتية: جيدة لكن تحتاج حذر أكثر"

        if has_negative_news:
            signal_grade = "C ⚠️"
            grade_note = "تحذير: يوجد خبر سلبي حديث، يفضل الحذر الشديد أو التجاهل"

        grade_clean = signal_grade.split()[0]

        if grade_clean == "A" and "البوت الأول" in source_text:
            if instant_rvol < 2.5 or not real_breakout:
                return

        if grade_clean not in ["A", "A+", "A++"]:
            return

        news_text = ""
        if has_strong_news:
            news_text = (
                f"📰 *خبر داعم:* {news_info['label']}\n"
                f"⭐ News Score: {news_info['score']:.0f}\n"
                f"🧠 العنوان: {news_info['headline']}\n\n"
            )
        elif has_negative_news:
            news_text = (
                f"🚨 *تحذير خبر سلبي:* {news_info['label']}\n"
                f"⭐ News Score: {news_info['score']:.0f}\n"
                f"🧠 العنوان: {news_info['headline']}\n\n"
            )
        else:
            news_text = "📰 الأخبار: لا يوجد خبر قوي حديث\n\n"

        msg = (
            f"🧠🔥 *بوت القرار الذكي - دخول جاهز الآن*\n\n"
            f"🎫 السهم: `{symbol}`\n"
            f"💰 السعر: {entry:.2f}\n\n"
            f"🎯 الحالة: دخول جاهز (تمت المتابعة والتأكيد)\n\n"
            f"📡 المصدر:\n"
            f"{source_text} + متابعة ذكية\n\n"
            f"{news_text}"
            f"🏆 التصنيف: {signal_grade}\n"
            f"🧠 ملاحظة: {grade_note}\n\n"
            f"📊 القوة:\n"
            f"RSI: {rsi:.1f}\n"
            f"RVOL: {instant_rvol:.2f}x\n"
            f"حركة 10د: {recent_move:.2f}%\n\n"
            f"🛡️ فلتر التصريف: تم تجاوزه ✅\n\n"
            f"🚀 دخول الآن: {entry:.2f}\n"
            f"🎯 هدف 1: {t1:.2f}\n"
            f"🚀 هدف ثاني: {t2:.2f}\n"
            f"🛑 وقف الخسارة: {sl:.2f}\n\n"
            f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
        )

        send_telegram_msg(msg)

        sent_alerts[symbol] = {
            "time": datetime.now(saudi_tz)
        }

        active_trades[symbol] = {
            "entry": entry,
            "t1": t1,
            "t2": t2,
            "sl": sl,
            "time": datetime.now(saudi_tz),
            "slow_alerted": False,
            "run_alerted": False,
            "stop_alerted": False
        }

        watchlist[symbol]["alerted"] = True

        print(f"🧠 READY ENTRY SENT: {symbol}", flush=True)

    except Exception as e:
        print(f"Check entry error {symbol}: {e}", flush=True)


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
                msg = (
                    f"🛑 *خروج - كسر وقف الخسارة*\n\n"
                    f"🎫 السهم: `{symbol}`\n"
                    f"💰 السعر الحالي: {cp:.2f}\n"
                    f"🚀 الدخول: {entry:.2f}\n"
                    f"🛑 الوقف: {sl:.2f}"
                )

                send_telegram_msg(msg)
                trade["stop_alerted"] = True
                active_trades.pop(symbol, None)
                continue

            if age_minutes >= 5 and gain_pct < 0.5 and not trade.get("slow_alerted", False):
                msg = (
                    f"⚠️ *تنبيه متابعة الصفقة*\n\n"
                    f"🎫 السهم: `{symbol}`\n"
                    f"💰 السعر الحالي: {cp:.2f}\n"
                    f"🚀 الدخول: {entry:.2f}\n"
                    f"📊 الحركة بعد الدخول: {gain_pct:.2f}%\n\n"
                    f"⚠️ السهم لم يتحرك بقوة بعد الدخول.\n"
                    f"الأفضل تشديد الوقف أو الخروج الجزئي."
                )

                send_telegram_msg(msg)
                trade["slow_alerted"] = True

            if gain_pct >= 2 and not trade.get("run_alerted", False):
                msg = (
                    f"🚀 *السهم انطلق بعد الدخول*\n\n"
                    f"🎫 السهم: `{symbol}`\n"
                    f"💰 السعر الحالي: {cp:.2f}\n"
                    f"🚀 الدخول: {entry:.2f}\n"
                    f"📈 الربح الحالي: {gain_pct:.2f}%\n\n"
                    f"🎯 هدف 1: {t1:.2f}\n"
                    f"🚀 هدف ثاني: {t2:.2f}\n"
                    f"✅ يفضل رفع الوقف لحماية الربح."
                )

                send_telegram_msg(msg)
                trade["run_alerted"] = True

        except Exception as e:
            print(f"Monitor trade error {symbol}: {e}", flush=True)
            continue


print("🧠 AUTO DECISION BOT STARTED", flush=True)
send_telegram_msg("🧠 تم تشغيل بوت القرار الذكي")

while True:
    try:
        if not is_trading_time():
            print("⏸️ خارج وقت التشغيل - البوت ينتظر", flush=True)
            time.sleep(300)
            continue

        update_watchlist_from_gist()
        update_watchlist_from_radar()
        clean_old_watchlist()

        print(f"📊 Watchlist size: {len(watchlist)}", flush=True)

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

        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("Main loop error:", e, flush=True)
        time.sleep(10)
