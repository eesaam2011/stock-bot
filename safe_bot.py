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

confirmed_alerts = {}
shared_alerts = {}
saudi_tz = pytz.timezone("Asia/Riyadh")
RUN_RADAR = True


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
        print("Gist keys missing", flush=True)
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

        signals = [
            s for s in signals
            if now_ts - float(s.get("time", 0)) < 1200
        ]

        return signals

    except Exception as e:
        print("Gist read error:", e, flush=True)
        return []


def save_signal_to_gist(symbol, price, signal_type):
    if not GIST_ID or not GITHUB_TOKEN:
        print("Gist keys missing", flush=True)
        return

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

        signals = [
            s for s in signals
            if now_ts - float(s.get("time", 0)) < 1200
        ]

        signals.append({
            "symbol": symbol,
            "price": round(float(price), 4),
            "type": signal_type,
            "source": "safe_bot",
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

        print(f"Gist saved SAFE: {symbol}", flush=True)

    except Exception as e:
        print("Gist save error:", e, flush=True)


def check_shared_signal(symbol):
    signals = read_gist_signals()

    for s in signals:
        if (
            s.get("symbol") == symbol
            and s.get("source") == "main_bot"
        ):
            return True, s

    return False, None


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

        # رحلات بحرية / كروز
        "NCLH", "CCL", "RCL"
    ]

    exclude_keywords = [
        "bank", "financial", "credit", "lending", "capital", "finance",
        "insurance", "assurance",
        "casino", "bet", "gambling", "lottery",
        "alcohol", "beer", "wine", "spirits", "brew",
        "tobacco", "cigarette", "smoke",
        "cannabis", "marijuana", "weed", "thc", "cbd"
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
                params={"scrIds": scr_id, "count": 250},
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
                    and price is not None
                    and 0.5 <= float(price) <= 25
                ):
                    symbols.append(symbol)

        clean_symbols = []

        for s in list(set(symbols)):
            if (
                isinstance(s, str)
                and "." not in s
                and "^" not in s
                and "-" not in s
                and s not in black_list
                and not any(keyword in s.lower() for keyword in exclude_keywords)
            ):
                clean_symbols.append(s)

        return clean_symbols

    except Exception as e:
        print("Symbol list error:", e, flush=True)
        return []


def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = -delta.where(delta < 0, 0).rolling(window=period).mean()

    if loss.iloc[-1] == 0:
        return 100

    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


def run_momentum_scanner():
    global confirmed_alerts, shared_alerts, RUN_RADAR

    print("🔎 Fetching symbols for SAFE bot...", flush=True)

    symbols = get_base_list()
    total_symbols = len(symbols)

    print(f"✅ SAFE symbols loaded: {total_symbols}", flush=True)

    if total_symbols == 0:
        print("No symbols found", flush=True)
        time.sleep(30)
        return

    for index, symbol in enumerate(symbols):
        if not RUN_RADAR:
            break

        try:
            now = datetime.now(saudi_tz)

            confirmed_alerts = {
                s: t for s, t in confirmed_alerts.items()
                if now < t["expiry"]
            }

            shared_alerts = {
                s: t for s, t in shared_alerts.items()
                if now < t["expiry"]
            }

            print(
                f"{now.strftime('%H:%M:%S')} | "
                f"{index + 1}/{total_symbols} | "
                f"SAFE scanning {symbol} | alerts: {len(confirmed_alerts)}",
                flush=True
            )

            ticker = yf.Ticker(symbol)
            df = ticker.history(period="1d", interval="1m", prepost=True)

            if (
                df.empty
                or len(df) < 25
                or df["Volume"].sum() == 0
                or df["Volume"].mean() == 0
            ):
                continue

            try:
                trade = api.get_latest_trade(symbol)
                cp = float(trade.price)
            except Exception:
                cp = float(df["Close"].iloc[-1])

            if cp <= 0:
                continue

            day_high = df["High"].max()
            price_2min_ago = df["Close"].iloc[-3]
            price_10min_ago = df["Close"].iloc[-10]

            if day_high == 0 or price_2min_ago == 0 or price_10min_ago == 0:
                continue

            recent_vol = df["Volume"].tail(5).sum()

            if not (0.5 <= cp <= 25):
                continue

            if recent_vol < 50000:
                continue

            stretch = ((cp - price_2min_ago) / price_2min_ago) * 100

            if stretch > 1.8:
                continue

            recent_highs = df["High"].tail(10)
            touches = (recent_highs >= day_high * 0.995).sum()

            if touches >= 3:
                continue

            vwap = (df["Close"] * df["Volume"]).sum() / df["Volume"].sum()
            avg_5min = df["Close"].tail(5).mean()

            rsi = calculate_rsi(df["Close"])

            if pd.isna(rsi) or rsi <= 55 or rsi > 85:
                continue

            recent_move = ((cp - price_10min_ago) / price_10min_ago) * 100
            instant_rvol = df["Volume"].tail(3).mean() / df["Volume"].mean()

            if day_high > 0 and cp >= day_high * 0.995:
                continue

            if touches >= 2:
                continue

            if rsi >= 70:
                continue

            if recent_move >= 3:
                continue

            df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
            df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

            ema9 = df["EMA9"].iloc[-1]
            ema20 = df["EMA20"].iloc[-1]

            if not (cp > ema9 and cp > ema20 and ema9 > ema20):
                continue

            last_open = df["Open"].iloc[-1]
            last_close = df["Close"].iloc[-1]
            last_high = df["High"].iloc[-1]
            last_low = df["Low"].iloc[-1]

            last_body = abs(last_close - last_open)
            avg_body = abs(df["Close"] - df["Open"]).tail(10).mean()

            if last_high == last_low:
                continue

            strong_last_candle = (
                last_close > last_open
                and last_body >= avg_body * 0.8
                and last_close >= last_low + ((last_high - last_low) * 0.70)
            )

            if not strong_last_candle:
                continue

            vol_3 = df["Volume"].tail(3).mean()
            vol_10 = df["Volume"].tail(10).mean()

            if vol_10 == 0:
                continue

            volume_confirmed = vol_3 > vol_10 * 1.20

            if not volume_confirmed:
                continue

            confirm_vwap = cp > vwap * 1.005
            no_short_breakdown = cp > avg_5min
            is_near_high = cp / day_high >= 0.985

            if not confirm_vwap:
                continue

            if not no_short_breakdown:
                continue

            is_momentum = (
                recent_move >= 0.7
                and instant_rvol > 3.0
                and is_near_high
            )

            is_accumulation = (
                abs(recent_move) < 0.3
                and instant_rvol > 4.5
                and cp > vwap
                and cp > ema9
                and cp > ema20
            )

            confirmed_entry = (
                confirm_vwap
                and strong_last_candle
                and volume_confirmed
                and no_short_breakdown
                and touches < 3
                and stretch <= 1.8
            )

            if confirmed_entry and (is_momentum or is_accumulation) and symbol not in confirmed_alerts:
                status = "دخول مؤكد جدًا - تجميع لحظي 🎯" if is_accumulation else "دخول مؤكد جدًا - انفجار ⚡"

                t1 = cp * 1.025
                t2 = cp * 1.05
                sl = cp * 0.985

                is_shared, main_signal = check_shared_signal(symbol)

                if is_shared and symbol not in shared_alerts:
                    main_price = float(main_signal.get("price", 0))
                    price_diff = ((cp - main_price) / main_price * 100) if main_price > 0 else 0

                    shared_msg = (
                        f"🔥🔥 *تأكيد مزدوج قوي جدًا*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الآن: ${cp:.2f}\n"
                        f"📌 ظهر في البوت الأول + بوت الدخول الأقوى\n"
                        f"⭐ الأولوية: عالية جدًا\n\n"
                        f"📍 سعر إشارة البوت الأول: ${main_price:.2f}\n"
                        f"📈 الفرق من أول إشارة: {price_diff:.2f}%\n\n"
                        f"💪 RSI: {rsi:.1f}\n"
                        f"⚡ RVOL: {instant_rvol:.2f}x\n"
                        f"📈 حركة 10د: {recent_move:.2f}%\n"
                        f"🛡️ تمدد 2د: {stretch:.2f}%\n\n"
                        f"🔝 قمة اليوم: ${day_high:.2f}\n"
                        f"🚀 دخول اذا اخترق: ${day_high:.2f}\n"
                        f"🎯 هدف 1: ${t1:.2f}\n"
                        f"🚀 هدف 2: ${t2:.2f}\n"
                        f"🛑 وقف الخسارة: ${sl:.2f}\n\n"
                        f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
                    )

                    send_telegram_msg(shared_msg)
                    shared_alerts[symbol] = {
                        "expiry": now + timedelta(minutes=15)
                    }

                msg = (
                    f"✅ *بوت الدخول الأقوى - إشارة مؤكدة جدًا*\n\n"
                    f"🎫 السهم: `{symbol}`\n"
                    f"💰 السعر: ${cp:.2f}\n"
                    f"💡 الحالة: {status}\n\n"
                    f"📊 *التأكيدات:*\n"
                    f"💪 RSI: {rsi:.1f}\n"
                    f"⚡ RVOL لحظي: {instant_rvol:.2f}x\n"
                    f"📈 حركة 10د: {recent_move:.2f}%\n"
                    f"🛡️ تمدد 2د: {stretch:.2f}%\n"
                    f"🔝 قمة اليوم: ${day_high:.2f}\n"
                    f"🚀 دخول اذا اخترق: ${day_high:.2f}\n"
                    f"🔝 لمس القمة: {touches}/3\n"
                    f"📌 EMA: السعر فوق EMA9 و EMA20 و EMA9 فوق EMA20 ✅\n"
                    f"📌 VWAP: السعر فوق VWAP ✅\n"
                    f"🟢 آخر شمعة قوية ✅\n\n"
                    f"🎯 هدف 1: ${t1:.2f}\n"
                    f"🚀 هدف 2: ${t2:.2f}\n"
                    f"🛑 وقف الخسارة: ${sl:.2f}\n\n"
                    f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
                )

                send_telegram_msg(msg)
                save_signal_to_gist(symbol, cp, status)

                confirmed_alerts[symbol] = {
                    "expiry": now + timedelta(minutes=15)
                }

            time.sleep(0.05)

        except Exception as e:
            print(f"Error with {symbol}: {e}", flush=True)
            continue


print("🚀 SAFE BOT STARTED", flush=True)
send_telegram_msg("🚀 تم تشغيل بوت الدخول الأقوى على Render")

while RUN_RADAR:
    run_momentum_scanner()
    time.sleep(10)
