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
TELEGRAM_FAST_CHAT_ID = os.getenv("TELEGRAM_FAST_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN_BOT2")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
BOT2_ACTIVE_TRADES_REDIS_KEY = "bot2_active_trades"
BOT2_FINAL_RESULTS_REDIS_KEY = "bot2_final_results"

LIVE_MOVERS_REDIS_KEY = "live_movers"
api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)
saudi_tz = pytz.timezone("Asia/Riyadh")

# =========================
# SETTINGS
# =========================

PRICE_MIN = 0.4
PRICE_MAX = 25

SCAN_INTERVAL = 40
MASTER_LIST_FILE = "master_list.json"
LIVE_MOVERS_FILE = "live_movers.json"
BOT2_PRELIMINARY_FILE = "bot2_preliminary_results.json"
BOT2_FINAL_FILE = "bot2_final_results.json"
BOT2_ACTIVE_TRADES_FILE = "bot2_active_trades.json"

STRONG_COUNT = 400
RADAR_COUNT = 400

sent_alerts = {}
active_trades = {}

# =========================
# TELEGRAM
# =========================

def send_telegram_msg(message, chat_id):
    if not TELEGRAM_TOKEN or not chat_id:
        print("Telegram keys missing", flush=True)
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                # "parse_mode": "Markdown"
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
    
    if 4 * 60 <= current_minutes <= 10 * 60 + 45:
        return False

    return True


# =========================
# GIST
# =========================

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

        print(f"✅ Saved {len(data) if isinstance(data, list) else 'data'} to {filename}", flush=True)

    except Exception as e:
        print(f"❌ Save gist error ({filename}): {e}", flush=True)


def load_active_trades_from_gist():
    global active_trades

    saved = read_gist_file(
        BOT2_ACTIVE_TRADES_FILE,
        default={}
    )

    if isinstance(saved, dict):
        active_trades = saved
        print(f"✅ Restored Bot 2 active trades: {len(active_trades)}", flush=True)
    else:
        active_trades = {}
        print("⚠️ No valid Bot 2 active trades found", flush=True)


# =========================
# TIME
# =========================

def is_trading_time():
    now = datetime.now(saudi_tz)
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()

    if weekday > 4:
        return False

    if hour > 9 or (hour == 9 and minute >= 30):
        return True

    if hour < 3:
        return True

    return False


def get_trade_age_minutes(trade):
    trade_time = trade.get("time", time.time())

    try:
        return (time.time() - float(trade_time)) / 60
    except Exception:
        return 0


# =========================
# DATA
# =========================

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

            if not symbol:
                continue

            symbol = symbol.upper().strip()

            if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
                continue

            symbols.append(symbol)

    symbols = list(dict.fromkeys(symbols))
    return symbols[:STRONG_COUNT + RADAR_COUNT]

def load_live_radar_from_redis():
    try:
        if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
            print("⚠️ Upstash Redis env vars missing", flush=True)
            return []

        url = f"{UPSTASH_REDIS_REST_URL}/get/{LIVE_MOVERS_REDIS_KEY}"

        headers = {
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"
        }

        r = requests.get(
            url,
            headers=headers,
            timeout=10
        )

        if r.status_code != 200:
            print(f"❌ Redis read failed: {r.status_code} {r.text}", flush=True)
            return []

        data = r.json().get("result")

        if not data:
            return []

        movers = json.loads(data)
        if isinstance(movers, str):
            movers = json.loads(movers)

        symbols = []
        now_ts = time.time()

        for item in movers:
            symbol = str(item.get("symbol", "")).upper().strip()

            if not symbol:
                continue

            ts = item.get("timestamp", 0)
            if ts and now_ts - ts > 180:
                continue

            symbols.append(symbol)

        symbols = list(dict.fromkeys(symbols))

        print(f"✅ Loaded live movers from Redis: {len(symbols)}", flush=True)
        return symbols

    except Exception as e:
        print(f"❌ Redis read exception: {e}", flush=True)
        return []
        
def load_live_radar():
    data = read_gist_file(LIVE_MOVERS_FILE, default=[])

    symbols = []
    now_ts = time.time()

    if isinstance(data, list):
        for item in data:

            if isinstance(item, str):
                symbol = item
                item_time = now_ts

            elif isinstance(item, dict):
                symbol = item.get("symbol")
                item_time = item.get("time", 0)

            else:
                continue

            if not symbol:
                continue

            symbol = symbol.upper().strip()

            if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
                continue

            age = now_ts - item_time if item_time else 999999

            if age > 180:
                continue

            symbols.append(symbol)

    symbols = list(dict.fromkeys(symbols))

    print(
        f"⚡ Loaded Live Movers symbols: {len(symbols)}",
        flush=True
    )

    return symbols
    
def get_alpaca_bars(symbol, minutes=120):
    try:
        end = datetime.now(pytz.UTC)
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

        df = df[needed].dropna().tail(minutes)
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


# =========================
# INDICATORS
# =========================

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


def calculate_trade_plan(entry):

    return {
        "entry": round(entry, 4),

        # 🎯 هدف أول واقعي
        "target_1": round(entry * 1.02, 4),

        # 🚀 هدف ثاني قريب من متوسط الحركة
        "target_2": round(entry * 1.04, 4),

        # 🛑 وقف خسارة -2%
        "stop_loss": round(entry * 0.98, 4)
    }

def calculate_micro_scalp_plan(entry):

    return {
        "entry": round(entry, 4),

        # 🎯 هدف سريع للسكالب
        "target_1": round(entry * 1.015, 4),

        # 🚀 هدف ثاني واقعي
        "target_2": round(entry * 1.03, 4),

        # 🛑 وقف خسارة -2%
        "stop_loss": round(entry * 0.98, 4)
    }    

# =========================
# BOT 2 SCANNER
# =========================

def analyze_symbol(symbol, source_group):
    try:
        df = get_alpaca_bars(symbol, minutes=120)

        if df.empty or len(df) < 30 or df["Volume"].mean() == 0:
            return None

        cp = get_latest_price(symbol, df)

        if not (PRICE_MIN <= cp <= PRICE_MAX):
            return None
            
        day_high = float(df["High"].max())
        day_low = float(df["Low"].min())
        vwap = float((df["Close"] * df["Volume"]).sum() / df["Volume"].sum())

        rsi = calculate_rsi(df["Close"])
        instant_rvol = df["Volume"].tail(3).mean() / df["Volume"].mean()
        recent_move = ((cp - df["Close"].iloc[-10]) / df["Close"].iloc[-10]) * 100
        move_5m = ((cp - df["Close"].iloc[-5]) / df["Close"].iloc[-5]) * 100
        move_3m = ((cp - df["Close"].iloc[-3]) / df["Close"].iloc[-3]) * 100

        df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

        ema9 = float(df["EMA9"].iloc[-1])
        ema20 = float(df["EMA20"].iloc[-1])

        latest_volume = float(df["Volume"].tail(10).sum())
        dollar_volume = cp * latest_volume

        near_high = cp >= day_high * 0.975

        above_vwap = cp > vwap

        above_ema9 = cp > ema9

        ema_ok = ema9 >= ema20 * 0.995

        last_open = float(df["Open"].iloc[-1])
        last_close = float(df["Close"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])

        prev_close = float(df["Close"].iloc[-2])
        prev_high = float(df["High"].iloc[-2])

        candle_range = last_high - last_low

        if candle_range <= 0:
            return None

        close_position = (last_close - last_low) / candle_range
        upper_wick_pct = (last_high - last_close) / candle_range

        last_body = abs(last_close - last_open)
        body_ratio = last_body / candle_range if candle_range > 0 else 0

        last_3_volume = float(df["Volume"].tail(3).mean())
        prev_10_volume = float(df["Volume"].tail(13).head(10).mean())

        volume_acceleration = last_3_volume >= prev_10_volume * 1.6

        strong_candle = (
            close_position >= 0.70
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

        behavior_change = (
            recent_move >= 0.35
            and volume_acceleration
            and strong_candle
        )

        new_money_flow = (
            cp > vwap
            and cp > ema9
            and volume_acceleration
        )

        real_breakout = (
            last_close > prev_high
            and prev_close > prev_high * 0.998
            and instant_rvol >= 2.2
        )

        fake_breakout_risk = (
            upper_wick_pct >= 0.45
            and close_position < 0.55
            and instant_rvol >= 2.5
        )

        overextended = (
            rsi > 78
            or recent_move > 3.0
        )

        distribution_score = 0

        if instant_rvol >= 3.0 and recent_move < 0.75:
            distribution_score += 15

        if upper_wick_pct >= 0.40 and close_position < 0.60:
            distribution_score += 12

        if volume_acceleration and body_ratio < 0.28:
            distribution_score += 10
            
        early_confirmed_explosion = (
            0.45 <= recent_move <= 1.50
            and instant_rvol >= 3.0
            and volume_acceleration
            and cp > vwap
            and cp > ema9
            and ema9 >= ema20
            and near_high
            and (real_breakout or vwap_reclaim or ema_reclaim)
            and close_position >= 0.82
            and upper_wick_pct <= 0.22
            and body_ratio >= 0.40
            and move_3m >= 0.55
            and move_5m >= 0.75
            and move_3m >= move_5m * 0.75
            and distribution_score < 12
            and not fake_breakout_risk
            and not overextended
        )
        
        micro_scalp_setup = (
            1.00 <= cp <= 10.00
            and recent_move <= 1.20
            and instant_rvol >= 3
            and dollar_volume >= 300000
            and volume_acceleration
            and cp > vwap
            and cp > ema9
            and near_high
            and (real_breakout or vwap_reclaim)
            and close_position >= 0.85
            and upper_wick_pct <= 0.15
            and move_3m >= 0.30
            and move_5m >= 0.50
            and move_3m >= move_5m * 0.75
            and distribution_score < 12
            and not fake_breakout_risk
            and not overextended
        )
        
        if (
            instant_rvol >= 4
            and move_3m < move_5m
            and move_5m < recent_move
        ):
            return None

        if recent_move > 2.2:
            return None

        if not near_high:
            return None

        if not (real_breakout or vwap_reclaim or ema_reclaim):
            return None
        if not early_confirmed_explosion and not micro_scalp_setup:
            return None

        if micro_scalp_setup:
            setup_type = "🎯 هدف سنتات بسيطة"
            reason = "هدف قصير 5 إلى 10 سنتات"
            target_note = "🎯 الهدف الأساسي: ربح 5 إلى 10 سنتات فقط"
            timing_note = "⏱️ المدة المتوقعة: 3 إلى 15 دقيقة إذا استمر الزخم"
        else:
            setup_type = "🟢 دخول مبكر مؤكد قبل الانفجار"
            reason = "دخول مبكر مؤكد قبل الانفجار العالي"
            target_note = "🚀 الهدف الأساسي: انفجار أعلى"
            timing_note = "⏱️ المدة المتوقعة: 10 إلى 30 دقيقة حسب قوة الزخم"

        technical_score = 0

        technical_score += min(instant_rvol * 10, 24)

        technical_score += min(
            max(recent_move, 0) * 10,
            26
        )
        if above_vwap:
            technical_score += 10

        if above_ema9:
            technical_score += 10

        if ema_ok:
            technical_score += 8

        if near_high:
            technical_score += 8

        if real_breakout:
            technical_score += 15

        if 52 <= rsi <= 68:
            technical_score += 10
        elif 45 <= rsi < 52:
            technical_score += 5

        if fake_breakout_risk:
            technical_score -= 15

        if source_group == "RADAR":
            technical_score += 12

            if volume_acceleration:
                technical_score += 10
            if strong_candle:
                technical_score += 8
            if vwap_reclaim:
                technical_score += 8
            if ema_reclaim:
                technical_score += 6
            if behavior_change:
                technical_score += 10
            if new_money_flow:
                technical_score += 8

        if micro_scalp_setup:
            plan = calculate_micro_scalp_plan(cp)
        else:
            plan = calculate_trade_plan(cp)

        return {
            "symbol": symbol,
            "source_group": source_group,
            "price": round(cp, 4),
            "day_high": round(day_high, 4),
            "day_low": round(day_low, 4),
            "vwap": round(vwap, 4),
            "ema9": round(ema9, 4),
            "ema20": round(ema20, 4),
            "rsi": round(rsi, 2),
            "instant_rvol": round(float(instant_rvol), 2),
            "recent_move": round(float(recent_move), 2),
            "move_3m": round(float(move_3m), 2),
            "move_5m": round(float(move_5m), 2),
            "dollar_volume_10m": round(float(dollar_volume), 2),
            "close_position": round(float(close_position), 2),
            "upper_wick_pct": round(float(upper_wick_pct), 2),
            "body_ratio": round(float(body_ratio), 2),
            "distribution_score": round(float(distribution_score), 2),
            "source": "LIVE_MOVERS" if source_group in ["STRONG", "RADAR"] else source_group,
            "real_breakout": bool(real_breakout),
            "fake_breakout_risk": bool(fake_breakout_risk),
            "volume_acceleration": bool(volume_acceleration),
            "strong_candle": bool(strong_candle),
            "vwap_reclaim": bool(vwap_reclaim),
            "ema_reclaim": bool(ema_reclaim),
            "behavior_change": bool(behavior_change),
            "new_money_flow": bool(new_money_flow),
            "setup_type": setup_type,
            "reason": reason,
            "target_note": target_note,
            "timing_note": timing_note,
            "technical_score": round(float(technical_score), 2),
            "entry": plan["entry"],
            "target_1": plan["target_1"],
            "target_2": plan["target_2"],
            "stop_loss": plan["stop_loss"],
            "time": time.time(),
            "created_at": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")
        }

    except Exception as e:
        print(f"Analyze error {symbol}: {e}", flush=True)
        return None


def scan_group(symbols, source_group):
    results = []

    for i, symbol in enumerate(symbols, start=1):
        result = analyze_symbol(symbol, source_group)

        if result:
            results.append(result)

        if i % 50 == 0:
            print(f"🔎 {source_group}: scanned {i}/{len(symbols)} | found {len(results)}", flush=True)

        time.sleep(0.03)

    return results


def finalize_and_rank_results(preliminary_results):
    final_results = []

    for r in preliminary_results:

        radar_bonus = 8 if r.get("source_group") == "RADAR" else 0

        final_score = (
            float(r.get("technical_score", 0))
            + radar_bonus
        )

        if final_score >= 90:
            grade = "A++"
        elif final_score >= 80:
            grade = "A+"
        elif final_score >= 70:
            grade = "A"
        elif final_score >= 60:
            grade = "B"
        else:
            grade = "C"

        r["radar_bonus"] = radar_bonus
        r["final_score"] = round(final_score, 2)
        r["grade"] = grade

        final_results.append(r)

    final_results = sorted(
        final_results,
        key=lambda x: x.get("final_score", 0),
        reverse=True
    )

    return final_results

def classify_setup_strength(data):
    score = float(data.get("final_score", 0) or 0)
    rvol = float(data.get("instant_rvol", 0) or 0)
    recent_move = float(data.get("recent_move", 0) or 0)
    move_3m = float(data.get("move_3m", 0) or 0)
    close_position = float(data.get("close_position", 0) or 0)
    distribution_score = float(data.get("distribution_score", 0) or 0)

    volume_acceleration = bool(data.get("volume_acceleration", False))
    real_breakout = bool(data.get("real_breakout", False))

    if (
        score >= 85
        and rvol >= 4
        and move_3m >= 0.55
        and recent_move >= 1.0
        and close_position >= 0.75
        and distribution_score < 15
        and volume_acceleration
        and real_breakout
    ):
        return "💎 فرصة ذهبية"

    if (
        score >= 75
        and rvol >= 3
        and move_3m >= 0.35
        and close_position >= 0.68
        and distribution_score < 25
        and volume_acceleration
    ):
        return "🔥 فرصة قوية جدًا"

    return "🟢 فرصة ممتازة"

def send_bot2_alert(signal):
    symbol = signal["symbol"]

    if sent_alerts.get(symbol):
        return

    grade = signal.get("grade", "C")
    source_group = signal.get("source_group", "UNKNOWN")

    if grade not in ["A", "A+", "A++"]:
        return

    if not can_send_trade_alerts():
        print(f"🔕 Bot 2 alert muted by schedule: {symbol} | {grade}", flush=True)
        return

    mode_text = (
        "🟢 EARLY CONFIRMED ENTRY MODE"
        if source_group == "RADAR"
        else
        "🔥 STRONG EARLY EXPLOSION MODE"
    )

    source_text = signal.get("source", "UNKNOWN")
    setup_strength = classify_setup_strength(signal)
    
    msg = (
        f"🟢🔥 Bot 2 - دخول مبكر مؤكد قبل الانفجار\n\n"
        f"{signal.get('target_note', '')}\n"
        f"{signal.get('timing_note', '')}\n"
        f"📌 نوع الإشارة: {signal.get('setup_type', '')}\n"
        f"🧠 السبب: {signal.get('reason', '')}\n\n"
        f"🎫 السهم: {symbol}\n"
        f"{mode_text}\n"
        f"💰 السعر: {signal.get('price', 0):.2f}\n"
        f"🏆 التصنيف: {grade}\n"
        f"💎 قوة الفرصة: {setup_strength}\n\n"
        f"📡 المصدر: {source_text}\n\n"
        f"📊 القوة:\n"
        f"Final Score: {signal.get('final_score', 0):.1f}\n"
        f"Technical Score: {signal.get('technical_score', 0):.1f}\n"
        f"Radar Bonus: {signal.get('radar_bonus', 0)}\n"
        f"RSI: {signal.get('rsi', 0):.1f}\n"
        f"RVOL: {signal.get('instant_rvol', 0):.2f}x\n"
        f"حركة 10د: {signal.get('recent_move', 0):.2f}%\n\n"
        f"🧪 إشارات الاستيقاظ:\n"
        f"Volume Acceleration: {signal.get('volume_acceleration', False)}\n"
        f"Strong Candle: {signal.get('strong_candle', False)}\n"
        f"VWAP Reclaim: {signal.get('vwap_reclaim', False)}\n"
        f"EMA Reclaim: {signal.get('ema_reclaim', False)}\n"
        f"Behavior Change: {signal.get('behavior_change', False)}\n"
        f"New Money Flow: {signal.get('new_money_flow', False)}\n\n"
        f"🚀 دخول مقترح: {signal.get('entry', 0):.2f}\n"
        f"🎯 هدف 1: {signal.get('target_1', 0):.2f}\n"
        f"🚀 هدف 2: {signal.get('target_2', 0):.2f}\n"
        f"🛑 وقف الخسارة: {signal.get('stop_loss', 0):.2f}\n\n"
        f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
    )

    send_telegram_msg(
        msg,
        TELEGRAM_FAST_CHAT_ID
    )

    sent_alerts[symbol] = {
        "time": time.time(),
        "grade": grade
    }

    active_trades[symbol] = {
        "entry": signal.get("entry"),
        "t1": signal.get("target_1"),
        "t2": signal.get("target_2"),
        "sl": signal.get("stop_loss"),
        "time": time.time(),
        "slow_alerted": False,
        "run_alerted": False,
        "stop_alerted": False
    }

    print(f"📩 Bot 2 alert sent: {symbol} | {grade} | {source_group}", flush=True)


# =========================
# MONITOR ACTIVE TRADES
# =========================

def monitor_active_trades():
    global active_trades

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

            if not entry or not sl:
                continue

            gain_pct = ((cp - entry) / entry) * 100
            age_minutes = get_trade_age_minutes(trade)

            if cp <= sl and not trade.get("stop_alerted", False):
                if can_send_trade_alerts():
                    msg = (
                        f"🛑 *Bot 2 - خروج وقف الخسارة*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"🛑 الوقف: {sl:.2f}"
                    )
                    send_telegram_msg(
                        msg,
                        TELEGRAM_FAST_CHAT_ID
                    )

                trade["stop_alerted"] = True
                active_trades.pop(symbol, None)
                continue

            if age_minutes >= 30 and gain_pct < 0.5 and not trade.get("slow_alerted", False):
                if can_send_trade_alerts():
                    msg = (
                        f"⚠️ *Bot 2 - متابعة الصفقة*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"📊 الحركة بعد الدخول: {gain_pct:.2f}%\n\n"
                        f"⚠️ السهم لم يتحرك بقوة بعد الدخول.\n"
                        f"يفضل تشديد الوقف أو الخروج الجزئي."
                    )
                    send_telegram_msg(
                        msg,
                        TELEGRAM_FAST_CHAT_ID 
                    )

                trade["slow_alerted"] = True

            if gain_pct >= 2 and not trade.get("run_alerted", False):
                new_sl = max(entry, cp * 0.985)

                if can_send_trade_alerts():
                    msg = (
                        f"🚀 *Bot 2 - السهم انطلق*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر الحالي: {cp:.2f}\n"
                        f"🚀 الدخول: {entry:.2f}\n"
                        f"📈 الربح الحالي: {gain_pct:.2f}%\n\n"
                        f"🎯 هدف 1: {t1:.2f}\n"
                        f"🚀 هدف 2: {t2:.2f}\n"
                        f"✅ الوقف المقترح الآن: {new_sl:.2f}"
                    )
                    send_telegram_msg(
                        msg,
                        TELEGRAM_FAST_CHAT_ID 
                    )

                trade["run_alerted"] = True

        except Exception as e:
            print(f"Monitor trade error {symbol}: {e}", flush=True)
            continue


# =========================
# RUN ONCE
# =========================

def run_bot2_once():
    live_symbols = load_live_radar_from_redis()
    master_symbols = load_master_list()

    if live_symbols:
        symbols = live_symbols

        print(
            f"⚡ Bot 2 using WebSocket Live Movers: {len(symbols)} symbols",
            flush=True
        )

    else:
        symbols = master_symbols

        print(
            f"🛟 Bot 2 fallback to Master List: {len(symbols)} symbols",
            flush=True
        )

    symbols = list(dict.fromkeys(symbols))

    if not symbols:
        print("⚠️ No symbols available", flush=True)
        return

    strong_symbols = symbols[:STRONG_COUNT]
    radar_symbols = symbols[STRONG_COUNT:STRONG_COUNT + RADAR_COUNT]

    print(
        f"📦 Bot 2 symbols: {len(symbols)} | Live: {len(live_symbols)} | Master: {len(master_symbols)}",
        flush=True
    )

    print(
        f"🟢 Early Confirmed Explosion (Top): {len(strong_symbols)}",
        flush=True
    )

    print(
        f"🔥 Strong Early Explosion (Next): {len(radar_symbols)}",
        flush=True
    )

    preliminary_results = []

    preliminary_results += scan_group(strong_symbols, "STRONG")
    preliminary_results += scan_group(radar_symbols, "RADAR")

    preliminary_results = sorted(
        preliminary_results,
        key=lambda x: x.get("technical_score", 0),
        reverse=True
    )

    save_gist_file(BOT2_PRELIMINARY_FILE, preliminary_results)

    
    final_results = finalize_and_rank_results(preliminary_results)

    # =========================
        # REMOVE OLD RESULTS (>90 MIN)
        # =========================

    fresh_results = []
    now_ts = time.time()

    for r in final_results:

        try:
            age = now_ts - float(r.get("time", now_ts))
            
        except Exception:
            age = 999999

        if age <= 5400:
            fresh_results.append(r)

    final_results = fresh_results
    save_json_to_redis(
        BOT2_FINAL_RESULTS_REDIS_KEY,
        
    telegram_results = [
        r for r in final_results
        if r.get("grade") in ["A+", "A++"]
    ]

    telegram_results = sorted(
        telegram_results,
        key=lambda x: (
            x.get("final_score", 0),
            x.get("instant_rvol", 0),
            x.get("recent_move", 0),
            x.get("dollar_volume_10m", 0)
        ),
        reverse=True
    )

    print(
        f"🎯 Best Bot 2 candidates selected: {len(telegram_results)}",
        flush=True
    )

    for signal in telegram_results[:2]:
        send_bot2_alert(signal)
        time.sleep(0.5)


# =========================
# MAIN LOOP
# =========================

load_active_trades_from_gist()
print("🟢 BOT 2 - EARLY CONFIRMED EXPLOSION STARTED", flush=True)

send_telegram_msg(
    "🟢 تم تشغيل Bot 2 - الدخول المبكر المؤكد قبل الانفجار",
    TELEGRAM_FAST_CHAT_ID
)
while True:
    try:
        if not is_trading_time():
            print(
                "⏸️ خارج وقت التشغيل - Bot 2 (الدخول المبكر المؤكد) ينتظر",
                flush=True
            )
            time.sleep(300)
            continue

        run_bot2_once()
        monitor_active_trades()

        save_json_to_redis(
            BOT2_ACTIVE_TRADES_REDIS_KEY,
            active_trades
        )

        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("Main loop error:", e, flush=True)
        time.sleep(10)
