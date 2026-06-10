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

API_KEY    = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GIST_ID      = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

api      = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)
app      = Flask(__name__)
saudi_tz = pytz.timezone("Asia/Riyadh")
ny_tz    = pytz.timezone("America/New_York")

PRICE_MIN          = 0.3
PRICE_MAX          = 25.0
MIN_AVG_VOL        = 50_000
MAX_AVG_VOL        = 800_000
RVOL_MIN           = 3.0
MIN_PRICE_CHANGE   = 5.0

MOMENTUM_RVOL_MIN             = 1.5
MOMENTUM_PRICE_CHANGE_MIN     = 3.0
MOMENTUM_MIN_DOLLAR_VOLUME    = 150_000
MOMENTUM_NEAR_HOD_PCT         = 0.96
MOMENTUM_RESISTANCE_TOLERANCE = 0.95

BATCH_SIZE         = 200
SCAN_INTERVAL      = 60
FULL_SCAN_INTERVAL = 3 * 60
REPEAT_BLOCK_HOURS = 12
STATE_FILE         = "explosion_state.json"

KNOWN_FLOATS = {}
EXPLOSION_CANDIDATE_MIN_SCORE = 70

SYMBOL_BLACKLIST = {
    "JPM","BAC","WFC","C","GS","MS","AXP","USB","TFC","PNC","COF","DFS",
    "MET","PRU","ALL","AIG","AFL","TRV","HIG","CB",
    "DKNG","PENN","WYNN","LVS","MGM","CZR",
    "BUD","TAP","STZ","DEO","PM","MO","BTI",
    "CGC","TLRY","ACB","SNDL","CRON",
    "NCLH","CCL","RCL","AMC","CNK","IMAX",
}

BAD_NAME_KEYWORDS = [
    "etf","fund","trust","warrant","unit","right","preferred",
    "bond","notes","income","index","acquisition","blank check",
    "spac","bank","bancorp","credit","lending","loan","mortgage",
    "insurance","casino","gambling","betting","sportsbook",
    "alcohol","beer","wine","tobacco","cannabis","marijuana",
    "hemp","cruise","cinema","movie","theater",
]

sent_alerts = {}
# قاموس لمتابعة الأسهم النشطة التي يتم مراقبتها حالياً لمنع تكرار خيوط المراقبة
active_monitors = {}

@app.route("/")
def home():
    return "Explosion Bot Running"

def is_scan_time_allowed():
    now_ny = datetime.now(ny_tz)
    if now_ny.weekday() >= 5:
        return False
    start = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
    end   = now_ny.replace(hour=20, minute=0, second=0, microsecond=0)
    return start <= now_ny <= end

def normalize_sent_alerts(raw_alerts):
    normalized = {}
    for symbol, ts in raw_alerts.items():
        try:
            normalized[str(symbol).upper()] = float(ts)
        except Exception:
            continue
    return normalized

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def gist_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def save_state(data):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"save_state local error: {e}", flush=True)

    if not GIST_ID or not GITHUB_TOKEN:
        return

    try:
        requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=gist_headers(),
            json={
                "files": {
                    STATE_FILE: {
                        "content": json.dumps(data, ensure_ascii=False, indent=2)
                    }
                }
            },
            timeout=20,
        )
    except Exception as e:
        print(f"save_state gist error: {e}", flush=True)

def load_state():
    if GIST_ID and GITHUB_TOKEN:
        try:
            r = requests.get(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=gist_headers(),
                timeout=20,
            )
            if r.status_code == 200:
                content = r.json().get("files", {}).get(STATE_FILE, {}).get("content", "")
                if content:
                    return json.loads(content)
        except Exception as e:
            print(f"load_state gist error: {e}", flush=True)

    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"load_state local error: {e}", flush=True)

    return {"sent_alerts": {}}

def is_clean_symbol(symbol):
    symbol = str(symbol).upper().strip()
    if not symbol or len(symbol) > 5:
        return False
    if not symbol.isalpha():
        return False
    if any(c in symbol for c in "./-^"):
        return False
    if symbol.endswith(("W","U","R","P","Q","Z")):
        return False
    if symbol in SYMBOL_BLACKLIST:
        return False
    return True

def get_float_tier(symbol, avg_vol_20):
    real_float = KNOWN_FLOATS.get(symbol)
    if real_float is not None:
        if real_float <= 5_000_000:
            return "ULTRA_LOW_FLOAT", real_float, "REAL"
        if real_float <= 10_000_000:
            return "VERY_LOW_FLOAT", real_float, "REAL"
        if real_float <= 20_000_000:
            return "LOW_FLOAT", real_float, "REAL"
        return "NORMAL_FLOAT", real_float, "REAL"

    if avg_vol_20 <= 150_000:
        return "ULTRA_LOW_FLOAT_LIKE", None, "ESTIMATED"
    if avg_vol_20 <= 300_000:
        return "VERY_LOW_FLOAT_LIKE", None, "ESTIMATED"
    if avg_vol_20 <= 800_000:
        return "LOW_FLOAT_LIKE", None, "ESTIMATED"
    return "NORMAL_FLOAT_LIKE", None, "ESTIMATED"

def calculate_explosion_score(float_tier, rvol, volume_acceleration, breakout_strength_pct, dollar_volume):
    score = 0
    if float_tier in ["ULTRA_LOW_FLOAT", "ULTRA_LOW_FLOAT_LIKE"]: score += 30
    elif float_tier in ["VERY_LOW_FLOAT", "VERY_LOW_FLOAT_LIKE"]: score += 24
    elif float_tier in ["LOW_FLOAT", "LOW_FLOAT_LIKE"]: score += 18
    else: score += 8

    if rvol >= 10: score += 25
    elif rvol >= 7: score += 20
    elif rvol >= 5: score += 16
    elif rvol >= 3: score += 12

    if volume_acceleration: score += 20

    if breakout_strength_pct >= 10: score += 15
    elif breakout_strength_pct >= 5: score += 12
    elif breakout_strength_pct >= 2: score += 8
    elif breakout_strength_pct >= 1: score += 5

    if dollar_volume >= 5_000_000: score += 10
    elif dollar_volume >= 2_000_000: score += 8
    elif dollar_volume >= 1_000_000: score += 6
    elif dollar_volume >= 500_000: score += 4

    return min(score, 100)

def get_all_symbols():
    symbols = []
    try:
        assets = api.list_assets(status="active")
        for asset in assets:
            symbol = getattr(asset, "symbol", "")
            name   = getattr(asset, "name", "") or ""
            if not symbol: continue
            symbol = symbol.upper().strip()
            if not getattr(asset, "tradable", False): continue
            if not is_clean_symbol(symbol): continue
            if any(k in name.lower() for k in BAD_NAME_KEYWORDS): continue
            symbols.append(symbol)
        symbols = list(set(symbols))
        print(f"✅ Total symbols after filter: {len(symbols)}", flush=True)
    except Exception as e:
        print(f"get_all_symbols error: {e}", flush=True)
    return symbols

def get_prices_bulk(symbols):
    prices = {}
    try:
        snapshots = api.get_snapshots(symbols)
        for symbol, snap in snapshots.items():
            price = None
            if snap.latest_trade:
                price = getattr(snap.latest_trade, "price", None)
            if price is None and snap.daily_bar:
                price = getattr(snap.daily_bar, "close", None)
            if price is not None:
                prices[symbol.upper()] = float(price)
    except Exception as e:
        print(f"get_prices_bulk error: {e}", flush=True)
    return prices

def filter_by_price(symbols):
    filtered = []
    for i in range(0, len(symbols), BATCH_SIZE):
        batch  = symbols[i:i + BATCH_SIZE]
        prices = get_prices_bulk(batch)
        for s in batch:
            p = prices.get(s)
            if p and PRICE_MIN <= p <= PRICE_MAX:
                filtered.append(s)
        time.sleep(0.2)
    print(f"✅ After price filter: {len(filtered)}", flush=True)
    return filtered

def get_daily_bars(symbols, limit=70):
    bars_map = {}
    try:
        bars = api.get_bars(symbols, tradeapi.TimeFrame.Day, limit=limit, adjustment="raw").df
        if bars is None or bars.empty: return bars_map

        if isinstance(bars.index, pd.MultiIndex):
            for sym in symbols:
                try:
                    df = bars.xs(sym)
                    if df is not None and not df.empty:
                        bars_map[sym] = df.copy()
                except Exception:
                    continue
        elif "symbol" in bars.columns:
            for sym, df in bars.groupby("symbol"):
                bars_map[str(sym).upper()] = df.copy()
    except Exception as e:
        print(f"get_daily_bars error: {e}", flush=True)
    return bars_map

def get_intraday_stats_bulk(symbols):
    stats = {}
    if not symbols: return stats
    try:
        now_ny = datetime.now(ny_tz)
        start_ny = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
        if now_ny < start_ny: return stats

        bars = api.get_bars(
            symbols,
            tradeapi.TimeFrame(5, tradeapi.TimeFrameUnit.Minute),
            start=start_ny.isoformat(),
            end=now_ny.isoformat(),
            adjustment="raw",
        ).df

        if bars is None or bars.empty: return stats

        if isinstance(bars.index, pd.MultiIndex):
            for sym in symbols:
                try:
                    df = bars.xs(sym)
                    if df is not None and not df.empty:
                        volume_sum = float(df["volume"].sum()) if "volume" in df.columns else 0
                        high_of_day = float(df["high"].max()) if "high" in df.columns else 0
                        recent_volume = float(df["volume"].tail(3).mean()) if len(df) >= 3 else 0
                        previous_volume = float(df["volume"].tail(12).head(9).mean()) if len(df) >= 12 else 0
                        volume_acceleration = (previous_volume > 0 and recent_volume > previous_volume * 1.5)
                        stats[sym.upper()] = {
                            "intraday_volume": volume_sum,
                            "high_of_day": high_of_day,
                            "volume_acceleration": volume_acceleration,
                        }
                except Exception:
                    continue
        elif "symbol" in bars.columns:
            for sym, df in bars.groupby("symbol"):
                if df is not None and not df.empty:
                    volume_sum = float(df["volume"].sum()) if "volume" in df.columns else 0
                    high_of_day = float(df["high"].max()) if "high" in df.columns else 0
                    recent_volume = float(df["volume"].tail(3).mean()) if len(df) >= 3 else 0
                    previous_volume = float(df["volume"].tail(12).head(9).mean()) if len(df) >= 12 else 0
                    volume_acceleration = (previous_volume > 0 and recent_volume > previous_volume * 1.5)
                    stats[str(sym).upper()] = {
                        "intraday_volume": volume_sum,
                        "high_of_day": high_of_day,
                        "volume_acceleration": volume_acceleration,
                    }
    except Exception as e:
        print(f"get_intraday_stats_bulk error: {e}", flush=True)
    return stats

def calculate_atr_14(df):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    true_range = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return float(true_range.tail(14).mean())

def check_explosion(symbol, df, current_price, intraday_volume, high_of_day, volume_acceleration):
    try:
        if df is None or df.empty or len(df) < 50: return None
        required_cols = {"close", "high", "low", "volume"}
        if not required_cols.issubset(set(df.columns)): return None

        close  = df["close"]
        volume = df["volume"]
        prev_close = float(close.iloc[-2])
        avg_vol_20 = float(volume.iloc[:-1].tail(20).mean())
        today_vol = float(intraday_volume or 0)

        if not (MIN_AVG_VOL <= avg_vol_20 <= MAX_AVG_VOL): return None
        if avg_vol_20 <= 0 or today_vol <= 0 or prev_close <= 0: return None

        rvol = today_vol / avg_vol_20
        if rvol < RVOL_MIN: return None

        price_change_pct = ((current_price - prev_close) / prev_close) * 100
        if price_change_pct < MIN_PRICE_CHANGE: return None

        resistance_20 = float(close.iloc[:-1].tail(20).max())
        resistance_50 = float(close.iloc[:-1].tail(50).max())
        if current_price < resistance_20 * 1.01: return None

        atr_14 = calculate_atr_14(df)
        if atr_14 <= 0: return None

        breakout_strength_pct = ((current_price - resistance_20) / resistance_20) * 100
        dollar_volume = current_price * today_vol
        float_tier, float_value, float_source = get_float_tier(symbol, avg_vol_20)

        explosion_score = calculate_explosion_score(
            float_tier=float_tier, rvol=rvol, volume_acceleration=volume_acceleration,
            breakout_strength_pct=breakout_strength_pct, dollar_volume=dollar_volume
        )
        explosion_candidate = explosion_score >= EXPLOSION_CANDIDATE_MIN_SCORE

        target1 = round(current_price + atr_14 * 1.0, 4)
        target2 = round(current_price + atr_14 * 2.0, 4)
        target3 = round(max(resistance_50, current_price + atr_14 * 3.0), 4)
        stop_loss = round(current_price - (atr_14 * 1.5), 4)

        return {
            "symbol": symbol, "price": round(current_price, 4), "prev_close": round(prev_close, 4),
            "price_change_pct": round(price_change_pct, 2), "rvol": round(rvol, 2), "avg_vol_20": int(avg_vol_20),
            "today_vol": int(today_vol), "dollar_volume": int(dollar_volume), "high_of_day": round(float(high_of_day or current_price), 4),
            "resistance_20": round(resistance_20, 4), "resistance_50": round(resistance_50, 4),
            "breakout_strength_pct": round(breakout_strength_pct, 2), "atr_14": round(atr_14, 4),
            "target1": target1, "target2": target2, "target3": target3, "stop_loss": stop_loss,
            "float_tier": float_tier, "float_value": float_value, "float_source": float_source,
            "volume_acceleration": volume_acceleration, "explosion_candidate": explosion_candidate,
            "explosion_score": explosion_score, "near_hod": current_price >= float(high_of_day or current_price) * MOMENTUM_NEAR_HOD_PCT,
            "alert_path": "EARLY_EXPLOSION", "alert_path_ar": "Early Explosion",
        }
    except Exception as e:
        print(f"check_explosion error {symbol}: {e}", flush=True)
        return None

def check_momentum_explosion(symbol, df, current_price, intraday_volume, high_of_day, volume_acceleration):
    try:
        if df is None or df.empty or len(df) < 50: return None
        required_cols = {"close", "high", "low", "volume"}
        if not required_cols.issubset(set(df.columns)): return None

        close  = df["close"]
        volume = df["volume"]
        prev_close = float(close.iloc[-2])
        avg_vol_20 = float(volume.iloc[:-1].tail(20).mean())
        today_vol = float(intraday_volume or 0)

        if avg_vol_20 <= 0 or today_vol <= 0 or prev_close <= 0: return None

        rvol = today_vol / avg_vol_20
        price_change_pct = ((current_price - prev_close) / prev_close) * 100
        dollar_volume = current_price * today_vol

        resistance_20 = float(close.iloc[:-1].tail(20).max())
        resistance_50 = float(close.iloc[:-1].tail(50).max())
        if resistance_20 <= 0: return None

        high_of_day = float(high_of_day or current_price)
        near_hod = current_price >= high_of_day * MOMENTUM_NEAR_HOD_PCT
        breakout_strength_pct = ((current_price - resistance_20) / resistance_20) * 100

        momentum_path = (
            rvol >= MOMENTUM_RVOL_MIN and price_change_pct >= MOMENTUM_PRICE_CHANGE_MIN
            and dollar_volume >= MOMENTUM_MIN_DOLLAR_VOLUME and current_price >= resistance_20 * MOMENTUM_RESISTANCE_TOLERANCE
            and volume_acceleration and near_hod
        )
        if not momentum_path: return None

        atr_14 = calculate_atr_14(df)
        if atr_14 <= 0: return None

        float_tier, float_value, float_source = get_float_tier(symbol, avg_vol_20)
        explosion_score = calculate_explosion_score(
            float_tier=float_tier, rvol=rvol, volume_acceleration=volume_acceleration,
            breakout_strength_pct=breakout_strength_pct, dollar_volume=dollar_volume
        )
        explosion_candidate = explosion_score >= EXPLOSION_CANDIDATE_MIN_SCORE

        target1 = round(current_price + atr_14 * 1.0, 4)
        target2 = round(current_price + atr_14 * 2.0, 4)
        target3 = round(max(resistance_50, current_price + atr_14 * 3.0), 4)
        stop_loss = round(current_price - (atr_14 * 1.5), 4)

        return {
            "symbol": symbol, "price": round(current_price, 4), "prev_close": round(prev_close, 4),
            "price_change_pct": round(price_change_pct, 2), "rvol": round(rvol, 2), "avg_vol_20": int(avg_vol_20),
            "today_vol": int(today_vol), "dollar_volume": int(dollar_volume), "high_of_day": high_of_day,
            "resistance_20": round(resistance_20, 4), "resistance_50": round(resistance_50, 4),
            "breakout_strength_pct": round(breakout_strength_pct, 2), "atr_14": round(atr_14, 4),
            "target1": target1, "target2": target2, "target3": target3, "stop_loss": stop_loss,
            "float_tier": float_tier, "float_value": float_value, "float_source": float_source,
            "volume_acceleration": volume_acceleration, "explosion_candidate": explosion_candidate,
            "explosion_score": explosion_score, "near_hod": near_hod,
            "alert_path": "MOMENTUM_EXPLOSION", "alert_path_ar": "Momentum Explosion",
        }
    except Exception as e:
        print(f"check_momentum_explosion error {symbol}: {e}", flush=True)
        return None

def merge_alert_results(early_result, momentum_result):
    if early_result and momentum_result:
        result = early_result.copy()
        result["alert_path"] = "BOTH"
        result["alert_path_ar"] = "Early Explosion + Momentum Explosion"
        result["near_hod"] = momentum_result.get("near_hod", early_result.get("near_hod", False))
        return result
    if early_result: return early_result
    if momentum_result: return momentum_result
    return None

def send_explosion_alert(result):
    sym   = result["symbol"]
    price = result["price"]
    chg   = result["price_change_pct"]
    rvol  = result["rvol"]
    avg_v = result["avg_vol_20"]
    t_v   = result["today_vol"]
    dvol  = result["dollar_volume"]
    hod   = result["high_of_day"]
    res20 = result["resistance_20"]
    res50 = result["resistance_50"]
    brk   = result["breakout_strength_pct"]
    atr   = result["atr_14"]
    t1    = result["target1"]
    t2    = result["target2"]
    t3    = result["target3"]
    stop  = result["stop_loss"]

    float_tier = result["float_tier"]
    float_source = result["float_source"]
    volume_acceleration = result["volume_acceleration"]
    explosion_score = result["explosion_score"]
    explosion_candidate = result["explosion_candidate"]
    alert_path_ar = result["alert_path_ar"]
    near_hod = result.get("near_hod", False)

    candidate_label = "✅ نعم" if explosion_candidate else "⚠️ مراقبة فقط"
    acceleration_label = "✅ موجود" if volume_acceleration else "❌ غير واضح"
    near_hod_label = "✅ قريب من High of Day" if near_hod else "⚠️ ليس قريباً جداً"

    msg = (
        f"🔥 <b>بداية اشتعال سهم - {sym}</b>\n\n"
        f"🧭 مسار التنبيه: {alert_path_ar}\n\n"
        f"💰 السعر الحالي: {price}\n"
        f"📈 الارتفاع عن إغلاق أمس: +{chg}%\n"
        f"🔥 RVOL intraday: {rvol}x المعدل\n"
        f"📊 حجم اليوم الفعلي intraday: {t_v:,}\n"
        f"💵 Dollar Volume: ${dvol:,}\n"
        f"📊 معدل الحجم 20 يوم: {avg_v:,}\n\n"
        f"🧬 Float Tier: {float_tier}\n"
        f"🧬 Float Source: {float_source}\n"
        f"🚀 Explosion Score: {explosion_score}/100\n"
        f"🚀 Explosion Candidate: {candidate_label}\n"
        f"⚡ تسارع الحجم: {acceleration_label}\n"
        f"📍 القرب من High of Day: {near_hod_label}\n\n"
        f"🧱 مقاومة 20 يوم: {res20}\n"
        f"🧱 مقاومة 50 يوم: {res50}\n"
        f"📍 أعلى سعر اليوم: {hod}\n"
        f"💪 قوة الاختراق: +{brk}% فوق مقاومة 20 يوم\n"
        f"📏 ATR 14: {atr}\n\n"
        f"🎯 الهدف الأول: {t1}\n"
        f"🎯 الهدف الثاني: {t2}\n"
        f"🚀 الهدف الأعلى المتوقع: {t3}\n"
        f"🛑 وقف الخسارة المقترح: {stop}\n\n"
        f"⚠️ سهم سريع الحركة — تقلب عالٍ جداً\n"
        f"🔗 https://www.tradingview.com/chart/?symbol={sym}"
    )
    send_telegram(msg)

# ==========================================
# التعديل الجوهري والمحوري: المحرك الفوري المخصص للمطاردة
# ==========================================
def dedicated_ticker_tracker(symbol, initial_price, target1, target2, target3, initial_stop):
    """
    هذه الدالة تعمل في خيط مستقل مخصص لكل سهم يتم اصطياده.
    تراقب السهم كل 10 ثوانٍ لحساب الاختراقات الجديدة والأهداف العليا (10$, 15$, 20$).
    """
    print(f"🎯 [بدء المراقبة اللحظية الشرسة] لـ {symbol}. السعر الابتدائي: {initial_price}", flush=True)
    
    highest_price_seen = initial_price
    current_stop = initial_stop
    
    t1_hit = False
    t2_hit = False
    t3_hit = False
    
    # محاكاة مستويات الأرقام النفسية الكبرى للأسهم الانفجارية المفتوحة
    psychological_targets = [7.5, 10.0, 12.5, 15.0, 20.0]
    hit_psych_targets = set()

    # تراقب السهم لمدة 3 ساعات متواصلة أو حتى يكسر الوقف
    for _ in range(int((3 * 3600) / 10)):
        if not is_scan_time_allowed():
            break
            
        try:
            # سحب السعر اللحظي الفوري عبر لقطة Alpaca للسهم المفرد
            snap = api.get_snapshot(symbol)
            if not snap or not snap.latest_trade:
                time.sleep(10)
                continue
                
            cp = float(snap.latest_trade.price)
            
            # 1. تحديث أعلى سعر وصل له السهم لتحديث الوقف المتحرك
            if cp > highest_price_seen:
                highest_price_seen = cp
                # رفع الوقف بشكل متحرك (Trailing) لحماية الأرباح (مثلاً الحفاظ على الوقف بمسافة الـ ATR الأصلي)
                atr_dist = initial_price - initial_stop
                current_stop = round(highest_price_seen - atr_dist, 4)

            # 2. فحص كسر الوقف المتحرك لإنهاء المراقبة وحماية الأرباح
            if cp <= current_stop:
                msg = f"🛑 <b>خروج وحماية أرباح السهم المتفجر [{symbol}]</b>\n\n📉 كسر السعر الوقف المتحرك عند: {cp}$\n🔝 أعلى سعر حققه الانفجار: {highest_price_seen}$"
                send_telegram(msg)
                break

            # 3. فحص ضرب الأهداف الفنية المقترحة (الـ ATR)
            if cp >= target1 and not t1_hit:
                t1_hit = True
                send_telegram(f"🎯 <b>[الهدف الأول تم ضربه!] {symbol}</b>\n💰 السعر الحالي: {cp}$\n🛡️ ينصح بتأمين الأرباح ونقل الوقف لنقطة الدخول.")
                
            if cp >= target2 and not t2_hit:
                t2_hit = True
                send_telegram(f"🚀 <b>[الهدف الثاني تم ضربه!] {symbol}</b>\n💰 السعر الحالي: {cp}$\n🔥 السهم يستعد للانفجار الأكبر.")
                
            if cp >= target3 and not t3_hit:
                t3_hit = True
                send_telegram(f"🛸 <b>[الهدف الأعلى المتوقع تم تحقيقه!] {symbol}</b>\n💰 السعر الحالي: {cp}$\n💎 السهم دخل مرحلة الانفجار العظيم (Supernova).")

            # 4. مطاردة الأرقام النفسية والانفجارات المفتوحة حتى 20$
            for pt in psychological_targets:
                if cp >= pt and pt not in hit_psych_targets and pt > initial_price:
                    hit_psych_targets.add(pt)
                    send_telegram(f"👑 <b>[سوبرنوفا!] اختراق حاجز نفسي شرس لـ {symbol}</b>\n🔥 السعر الحالي طار متجاوزاً: {pt}$\n🔝 أعلى سعر حتى الآن: {highest_price_seen}$")

        except Exception as e:
            print(f"Error in tracking thread for {symbol}: {e}", flush=True)
            
        time.sleep(10) # فحص دقيق جداً وثابت كل 10 ثوانٍ

    # تنظيف السهم من القائمة النشطة عند موته أو انتهاء الوقت
    active_monitors.pop(symbol, None)
    print(f"🏁 [انتهاء المراقبة] للسهم {symbol}.", flush=True)

def full_scan():
    print("🔎 Full scan started...", flush=True)
    symbols = get_all_symbols()
    symbols = filter_by_price(symbols)

    now_ts = time.time()
    alerts_sent = 0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        prices = get_prices_bulk(batch)
        bars_map = get_daily_bars(batch, limit=70)
        intraday_stats = get_intraday_stats_bulk(batch)

        for sym in batch:
            last_alert = sent_alerts.get(sym, 0)
            if now_ts - last_alert < REPEAT_BLOCK_HOURS * 3600:
                continue

            cp = prices.get(sym)
            if cp is None: continue

            df = bars_map.get(sym)
            stats = intraday_stats.get(sym, {})

            early_result = check_explosion(
                symbol=sym, df=df, current_price=cp,
                intraday_volume=stats.get("intraday_volume", 0),
                high_of_day=stats.get("high_of_day", cp),
                volume_acceleration=stats.get("volume_acceleration", False),
            )

            momentum_result = check_momentum_explosion(
                symbol=sym, df=df, current_price=cp,
                intraday_volume=stats.get("intraday_volume", 0),
                high_of_day=stats.get("high_of_day", cp),
                volume_acceleration=stats.get("volume_acceleration", False),
            )

            result = merge_alert_results(early_result, momentum_result)

            if result:
                send_explosion_alert(result)
                sent_alerts[sym] = now_ts
                alerts_sent += 1
                
                # إطلاق خيط مراقبة مستقل مخصص لهذا السهم فوراً دون تعطيل مسح السوق
                if sym not in active_monitors:
                    active_monitors[sym] = True
                    t = threading.Thread(
                        target=dedicated_ticker_tracker,
                        args=(sym, result["price"], result["target1"], result["target2"], result["target3"], result["stop_loss"]),
                        daemon=True
                    )
                    t.start()

        time.sleep(0.3)

    print(f"✅ Full scan done | alerts={alerts_sent}", flush=True)
    save_state({"sent_alerts": {k: v for k, v in sent_alerts.items()}})

def run_bot():
    global sent_alerts
    state = load_state()
    sent_alerts = normalize_sent_alerts(state.get("sent_alerts", {}))

    last_full_scan = 0

    while True:
        try:
            now_ts = time.time()
            if is_scan_time_allowed():
                if now_ts - last_full_scan >= FULL_SCAN_INTERVAL:
                    full_scan()
                    last_full_scan = time.time()
            else:
                print("⏸️ Scan skipped: outside US premarket/market hours", flush=True)

            time.sleep(10) # تقليل الانتظار ليكون تفاعل البوت أسرع مع استمرار الحلقات

        except Exception as e:
            print(f"run_bot error: {e}", flush=True)
            time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)والله 
