import os
import time
import requests
import pandas as pd
import yfinance as yf
import alpaca_trade_api as tradeapi
from datetime import datetime, timedelta
import pytz

# ==========================================
# الإعدادات من Render Environment Variables
# ==========================================
API_KEY = os.getenv("PKLT4AJPDSKTKUXSQDW3CMOY6L")
SECRET_KEY = os.getenv("725DY5KSZ6TFqdc1PqPxKd3ZovAyQkTwtqq8W2qfqWdX")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN = os.getenv("8425162317:AAH9l8S_rTx5gktNoG-mY0vEumYlmZdfWFg")
TELEGRAM_CHAT_ID = os.getenv("1125387322")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)

confirmed_alerts = {}
saudi_tz = pytz.timezone("Asia/Riyadh")
RUN_RADAR = True


def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram keys missing")
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
        print("Telegram error:", e)


def get_base_list():
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    headers = {"User-Agent": "Mozilla/5.0"}

    black_list = [
        "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC",
        "MET", "PRU", "ALL", "AIG", "CB",
        "DKNG", "PENN", "WYNN", "LVS",
        "BUD", "TAP", "STZ", "DEO",
        "PM", "MO",
        "CGC", "TLRY", "ACB"
    ]

    try:
        symbols = []

        for scr_id in ["most_actives", "day_gainers"]:
            res = requests.get(
                url,
                params={"scrIds": scr_id, "count": 250},
                headers=headers,
                timeout=10
            ).json()

            quotes = res["finance"]["result"][0]["quotes"]
            symbols += [q["symbol"] for q in quotes if "symbol" in q]

        clean_symbols = []

        for s in list(set(symbols)):
            if (
                isinstance(s, str)
                and "." not in s
                and "^" not in s
                and "-" not in s
                and s not in black_list
            ):
                clean_symbols.append(s)

        return clean_symbols

    except Exception as e:
        print("Symbol list error:", e)
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
    global confirmed_alerts, RUN_RADAR

    symbols = get_base_list()
    total_symbols = len(symbols)

    if total_symbols == 0:
        print("No symbols found")
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

            print(
                f"{now.strftime('%H:%M:%S')} | "
                f"{index + 1}/{total_symbols} | "
                f"Scanning {symbol} | alerts: {len(confirmed_alerts)}"
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

            if not (0.5 <= cp <= 25):
                continue

            recent_vol = df["Volume"].tail(5).sum()
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

            rsi = calculate_rsi(df["Close"])
            if pd.isna(rsi) or rsi <= 55 or rsi > 85:
                continue

            recent_move = ((cp - price_10min_ago) / price_10min_ago) * 100
            instant_rvol = df["Volume"].tail(3).mean() / df["Volume"].mean()

            # المتوسطات القريبة — ليست شرط إجباري
            df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
            df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

            ema9 = df["EMA9"].iloc[-1]
            ema20 = df["EMA20"].iloc[-1]

            trend_score = 1 if cp > ema9 and cp > ema20 else 0

            is_momentum = (
                recent_move >= 0.7
                and instant_rvol > 3.0
                and cp / day_high >= 0.985
            )

            is_accumulation = (
                abs(recent_move) < 0.3
                and instant_rvol > 4.5
                and cp > vwap
            )

            # هنا لم نجعل EMA شرطًا قاسيًا
            if (is_momentum or is_accumulation) and symbol not in confirmed_alerts:
                status = "تجميع لحظي 🎯" if is_accumulation else "انفجار ⚡"

                t1 = cp * 1.03
                t2 = cp * 1.06
                sl = cp * 0.98

                trend_text = "فوق EMA9 و EMA20 ✅" if trend_score == 1 else "لم يؤكد المتوسطات بعد ⚠️"

                msg = (
                    f"💎 *إشارة دخول {status}*\n\n"
                    f"🎫 السهم: `{symbol}`\n"
                    f"💰 السعر: ${cp:.2f}\n\n"
                    f"📊 *القوة:*\n"
                    f"💪 RSI: {rsi:.1f}\n"
                    f"⚡ RVOL لحظي: {instant_rvol:.2f}x\n"
                    f"📈 حركة 10 دقائق: {recent_move:.2f}%\n"
                    f"🛡️ التمدد: {stretch:.2f}%\n"
                    f"📌 الاتجاه: {trend_text}\n\n"
                    f"🎯 هدف 1: ${t1:.2f}\n"
                    f"🚀 هدف 2: ${t2:.2f}\n"
                    f"🛑 وقف الخسارة: ${sl:.2f}\n\n"
                    f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
                )

                send_telegram_msg(msg)

                confirmed_alerts[symbol] = {
                    "expiry": now + timedelta(minutes=15)
                }

            time.sleep(0.05)

        except Exception as e:
            print(f"Error with {symbol}: {e}")
            continue


send_telegram_msg("🚀 تم تشغيل رادار الأسهم على Render")

while RUN_RADAR:
    run_momentum_scanner()
    time.sleep(10)
