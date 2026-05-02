import os
import time
import json
import requests
import pandas as pd
import yfinance as yf
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


def read_gist_signals():
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
        content = data["files"]["signals.json"]["content"]

        try:
            signals = json.loads(content)
        except Exception:
            signals = []

        now_ts = time.time()

        return [
            s for s in signals
            if now_ts - float(s.get("time", 0)) < 1800
        ]

    except Exception as e:
        print("Gist read error:", e, flush=True)
        return []


def get_base_list():
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    headers = {"User-Agent": "Mozilla/5.0"}

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
        symbols = []

        for scr_id in [
            "most_actives",
            "day_gainers",
            "small_cap_gainers",
            "undervalued_growth_stocks",
            "aggressive_small_caps",
            "most_shorted_stocks",
            "high_beta_stocks",
            "growth_technology_stocks"
        ]:
            res = requests.get(
                url,
                params={"scrIds": scr_id, "count": 200},
                headers=headers,
                timeout=10
            ).json()

            data = res.get("finance", {}).get("result")

            if not data:
                continue

            quotes = data[0].get("quotes", [])

            for q in quotes:
                symbol = q.get("symbol")
                price = q.get("regularMarketPrice")

                if (
                    symbol
                    and isinstance(symbol, str)
                    and "." not in symbol
                    and "^" not in symbol
                    and "-" not in symbol
                    and symbol not in black_list
                    and price is not None
                    and PRICE_MIN <= float(price) <= PRICE_MAX
                ):
                    symbols.append(symbol)

        return list(set(symbols))

    except Exception as e:
        print("Base list error:", e, flush=True)
        return []


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

    if symbol not in watchlist:
        watchlist[symbol] = {
            "source": source,
            "first_price": float(price) if price else 0,
            "created_at": now,
            "alerted": False
        }

        print(f"🧠 Added watchlist: {symbol} | source: {source}", flush=True)


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


def update_watchlist_from_radar():
    symbols = get_base_list()

    for symbol in symbols[:120]:
        try:
            df = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)

            if df.empty or len(df) < 25 or df["Volume"].mean() == 0:
                continue

            cp = float(df["Close"].iloc[-1])
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
        df = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)

        if df.empty or len(df) < 30 or df["Volume"].mean() == 0:
            return

        try:
            trade = api.get_latest_trade(symbol)
            cp = float(trade.price)
        except Exception:
            cp = float(df["Close"].iloc[-1])

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

        last_open = float(df["Open"].iloc[-1])
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

        entry = cp
        t1 = entry * 1.02
        t2 = entry * 1.04
        sl = entry * 0.985

        msg = (
            f"🧠🔥 *بوت القرار الذكي - دخول جاهز الآن*\n\n"
            f"🎫 السهم: `{symbol}`\n"
            f"💰 السعر: {entry:.2f}\n\n"
            f"🎯 الحالة: دخول جاهز (تمت المتابعة والتأكيد)\n\n"
            f"📡 المصدر:\n"
            f"{data.get('source', 'رادار مبكر')} + متابعة ذكية\n\n"
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
            df = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)

            if df.empty or len(df) < 5:
                continue

            try:
                latest_trade = api.get_latest_trade(symbol)
                cp = float(latest_trade.price)
            except Exception:
                cp = float(df["Close"].iloc[-1])

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
        update_watchlist_from_gist()
        update_watchlist_from_radar()
        clean_old_watchlist()

        print(f"📊 Watchlist size: {len(watchlist)}", flush=True)

        for symbol, data in list(watchlist.items()):
            if not data.get("alerted", False):
                check_ready_entry(symbol, data)
                time.sleep(0.05)

        monitor_active_trades()

        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("Main loop error:", e, flush=True)
        time.sleep(10)
