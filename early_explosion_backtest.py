# early_explosion_backtest.py
import os
import json
import time
import math
import threading
from datetime import datetime, timedelta, date

import pytz
import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from flask import Flask
import alpaca_trade_api as tradeapi


# =========================================================
# CONFIG
# =========================================================

ALPACA_API_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_INVESTMENT_CHAT_ID = os.getenv("TELEGRAM_INVESTMENT_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

FLOAT_CACHE_URL = os.getenv("FLOAT_CACHE_URL")

START_DATE = os.getenv("BACKTEST_START_DATE", "2026-06-01")
END_DATE = os.getenv("BACKTEST_END_DATE", "2026-07-02")

PRICE_MIN = 0.3
PRICE_MAX = 25.0
MIN_AVG_VOL = 50_000
MAX_AVG_VOL = 5_000_000
MIN_DOLLAR_VOLUME = 300_000

RVOL_MIN = 1.8
MIN_PRICE_CHANGE = 4.0
EXPLOSION_CANDIDATE_MIN_SCORE = 90

RADAR_TRIGGER_CHANGE_PCT = 4.0
RADAR_MIN_DOLLAR_VOLUME = 100_000
RADAR_EXPIRE_MINUTES = 30

ALERT_COOLDOWN_SEC = 3600

BULK_BATCH_SIZE = 700
SCAN_INTERVAL_MINUTES = 3

MARKET_START_NY = "04:00"
MARKET_END_NY = "20:00"

OUTPUT_DIR = "backtest_output"

SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS",
    "AXP", "USB", "TFC", "PNC", "COF", "DFS",
    "MET", "PRU", "ALL", "AIG", "AFL", "TRV", "HIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR",
    "BUD", "TAP", "STZ", "DEO",
    "PM", "MO", "BTI",
    "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL",
    "AMC", "CNK", "IMAX", "HITI",
    "GPRE", "SKLZ", "PGY", "JELD",
    "TWO", "PGEN", "GENI", "TRC", "SVA", "SHOE",
}

BAD_NAME_KEYWORDS = [
    "etf", "fund", "trust", "warrant", "unit", "right", "preferred",
    "bond", "notes", "income", "index", "acquisition", "blank check",
    "spac", "bank", "bancorp", "credit", "lending", "loan", "mortgage",
    "insurance", "casino", "gambling", "betting", "sportsbook",
    "alcohol", "beer", "wine", "tobacco", "cannabis", "marijuana",
    "hemp", "cruise", "cinema", "movie", "theater"
]


# =========================================================
# FLASK STATUS
# =========================================================

app = Flask(__name__)

runtime = {
    "status": "starting",
    "started_at": None,
    "finished_at": None,
    "current_day": None,
    "symbols_loaded": 0,
    "alerts": 0,
    "error": None,
}


@app.route("/")
def home():
    return f"""
    <h2>⚡ Early Explosion Backtest</h2>
    <p>Status: {runtime["status"]}</p>
    <p>Started: {runtime["started_at"]}</p>
    <p>Finished: {runtime["finished_at"]}</p>
    <p>Current Day: {runtime["current_day"]}</p>
    <p>Symbols Loaded: {runtime["symbols_loaded"]}</p>
    <p>Alerts: {runtime["alerts"]}</p>
    <p>Error: {runtime["error"]}</p>
    """, 200


def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)


# =========================================================
# HELPERS
# =========================================================

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_INVESTMENT_CHAT_ID:
        print(f"[Telegram-Sim]\n{text}", flush=True)
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_INVESTMENT_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)


def safe_float(x, default=0.0):
    try:
        if x is None or pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default

def load_float_cache():
    if not FLOAT_CACHE_URL:
        raise RuntimeError("FLOAT_CACHE_URL is missing in Render environment variables.")

    print("📥 Loading float cache from Gist...", flush=True)

    try:
        res = requests.get(FLOAT_CACHE_URL, timeout=30)

        if res.status_code != 200:
            raise RuntimeError(f"Float cache HTTP error: {res.status_code}")

        data = res.json()

        if not isinstance(data, dict) or len(data) == 0:
            raise RuntimeError("Float cache is empty or invalid.")

        print(f"✅ Float records loaded: {len(data)}", flush=True)
        return data

    except Exception as e:
        raise RuntimeError(f"Failed to load float cache from FLOAT_CACHE_URL: {e}")

def get_float_bonus(real_float):
    if real_float is None:
        return 0, "UNKNOWN_FLOAT"

    if real_float <= 5_000_000:
        return 20, "ULTRA_LOW_FLOAT"
    if real_float <= 15_000_000:
        return 15, "VERY_LOW_FLOAT"
    if real_float <= 30_000_000:
        return 10, "LOW_FLOAT"
    if real_float <= 60_000_000:
        return 5, "MEDIUM_FLOAT"
    return 0, "HIGH_FLOAT"


def calculate_atr_14(daily_df):
    if daily_df is None or len(daily_df) < 14:
        return 0.0

    high = daily_df["high"].astype(float)
    low = daily_df["low"].astype(float)
    close = daily_df["close"].astype(float)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)

    return safe_float(tr.tail(14).mean())


def calculate_obv_bonus(bars_1m):
    if bars_1m is None or bars_1m.empty or len(bars_1m) < 10:
        return 0, "UNKNOWN_OBV"

    bars_1m = bars_1m.sort_index()
    closes = bars_1m["close"].tolist()
    volumes = bars_1m["volume"].tolist()

    obv = 0
    hist = []

    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
        hist.append(obv)

    if len(hist) < 5:
        return 0, "UNKNOWN_OBV"

    recent_obv = hist[-1]
    older_obv = hist[-5]
    growth = recent_obv - older_obv

    if growth > 0 and recent_obv > 0:
        return 10, "STRONG_OBV"
    if growth > 0:
        return 5, "POSITIVE_OBV"
    return 0, "WEAK_OBV"


def calculate_volume_acceleration(bars_1m):
    if bars_1m is None or bars_1m.empty or len(bars_1m) < 13:
        return {
            "volume_acceleration_score": 0,
            "vol_acceleration": 0.0,
            "last_1m_vs_avg": 0.0,
            "last_3m_vs_prev_7m": 0.0,
            "volume_trend_up": False,
            "volume_peak_recent": False
        }

    bars_1m = bars_1m.sort_index()
    volumes = bars_1m["volume"].astype(float)

    last_1m = safe_float(volumes.iloc[-1])
    avg_prev_10 = safe_float(volumes.iloc[-11:-1].mean())
    last_1m_vs_avg = last_1m / avg_prev_10 if avg_prev_10 > 0 else 0.0

    last_3m_avg = safe_float(volumes.iloc[-3:].mean())
    prev_7m_avg = safe_float(volumes.iloc[-10:-3].mean())
    last_3m_vs_prev_7m = last_3m_avg / prev_7m_avg if prev_7m_avg > 0 else 0.0

    v1, v2, v3 = volumes.iloc[-3], volumes.iloc[-2], volumes.iloc[-1]
    volume_trend_up = v1 <= v2 <= v3

    lookback = volumes.iloc[-13:]
    peak_idx = lookback.idxmax()
    volume_peak_recent = peak_idx in set(volumes.tail(3).index)

    score = 0

    if last_1m_vs_avg >= 3.0:
        score += 8
    elif last_1m_vs_avg >= 2.0:
        score += 5
    elif last_1m_vs_avg >= 1.5:
        score += 3

    if last_3m_vs_prev_7m >= 2.5:
        score += 7
    elif last_3m_vs_prev_7m >= 1.8:
        score += 5
    elif last_3m_vs_prev_7m >= 1.3:
        score += 3

    if volume_trend_up:
        score += 2
    if volume_peak_recent:
        score += 3

    return {
        "volume_acceleration_score": min(score, 15),
        "vol_acceleration": round(max(last_1m_vs_avg, last_3m_vs_prev_7m), 2),
        "last_1m_vs_avg": round(last_1m_vs_avg, 2),
        "last_3m_vs_prev_7m": round(last_3m_vs_prev_7m, 2),
        "volume_trend_up": bool(volume_trend_up),
        "volume_peak_recent": bool(volume_peak_recent)
    }


def get_score_bucket(score):
    if score <= 92:
        return "90-92"
    if score <= 95:
        return "93-95"
    return "96-100"


def get_price_bucket(price):
    if price < 1:
        return "<1"
    if price < 5:
        return "1-5"
    if price < 10:
        return "5-10"
    return "10-25"


def get_time_window(dt_ny):
    t = dt_ny.time()
    if t < datetime.strptime("09:30", "%H:%M").time():
        return "Premarket"
    if t < datetime.strptime("10:30", "%H:%M").time():
        return "First Hour"
    if t < datetime.strptime("15:00", "%H:%M").time():
        return "Midday"
    if t < datetime.strptime("16:00", "%H:%M").time():
        return "Last Hour"
    return "After Hours"


# =========================================================
# DATA LOADING
# =========================================================

def get_api():
    return tradeapi.REST(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
        ALPACA_BASE_URL,
        api_version="v2"
    )


def clean_assets(api):
    assets = api.list_assets(status="active")
    cleaned = []

    for a in assets:
        symbol = a.symbol
        name = getattr(a, "name", "") or ""
        raw = getattr(a, "_raw", {}) or {}

        if not getattr(a, "tradable", False):
            continue
        if raw.get("class", "") != "us_equity":
            continue
        if raw.get("exchange", "") not in ["NASDAQ", "NYSE", "AMEX"]:
            continue
        if "/" in symbol or "." in symbol or "-" in symbol or "^" in symbol:
            continue
        if symbol in SYMBOL_BLACKLIST:
            continue
        if any(kw in name.lower() for kw in BAD_NAME_KEYWORDS):
            continue

        cleaned.append({
            "symbol": symbol,
            "name": name,
            "exchange": raw.get("exchange", "")
        })

    return cleaned


def normalize_bars_df(df):
    if df is None or df.empty:
        return {}

    out = {}

    if "symbol" not in df.columns:
        return out

    df = df.reset_index()
    time_col = "timestamp" if "timestamp" in df.columns else df.columns[0]

    for sym, g in df.groupby("symbol"):
        g = g.copy()
        g[time_col] = pd.to_datetime(g[time_col], utc=True)
        g = g.set_index(time_col).sort_index()
        out[sym] = g

    return out


def fetch_bars_bulk(api, symbols, timeframe, start_dt, end_dt, label):
    all_data = {}

    for i in range(0, len(symbols), BULK_BATCH_SIZE):
        batch = symbols[i:i + BULK_BATCH_SIZE]
        print(f"📥 Loading {label} batch {i // BULK_BATCH_SIZE + 1} | size={len(batch)}", flush=True)

        try:
            bars = api.get_bars(
                batch,
                timeframe,
                start=start_dt.isoformat(),
                end=end_dt.isoformat(),
                adjustment="raw",
                feed="iex"
            ).df

            data = normalize_bars_df(bars)
            all_data.update(data)

        except Exception as e:
            print(f"⚠️ Bulk {label} error: {e}", flush=True)

        time.sleep(0.5)

    return all_data


# =========================================================
# BACKTEST CORE
# =========================================================

class EarlyExplosionBacktester:
    def __init__(self):
        self.api = get_api()
        self.float_cache = load_float_cache()

        self.alerts = []
        self.reject_stats = {}
        self.missed = []

        self.radar_watchlist = {}
        self.sent_alerts = {}

        self.daily_data = {}
        self.minute_data = {}

        self.tz_ny = pytz.timezone("America/New_York")
        self.tz_ksa = pytz.timezone("Asia/Riyadh")

    def reject(self, reason):
        self.reject_stats[reason] = self.reject_stats.get(reason, 0) + 1

    def load_data(self):
        assets = clean_assets(self.api)
        runtime["symbols_loaded"] = len(assets)

        symbols = [a["symbol"] for a in assets]
        self.assets_by_symbol = {a["symbol"]: a for a in assets}

        start = self.tz_ny.localize(datetime.strptime(START_DATE, "%Y-%m-%d")) - timedelta(days=140)
        end = self.tz_ny.localize(datetime.strptime(END_DATE, "%Y-%m-%d")) + timedelta(days=1)

        self.daily_data = fetch_bars_bulk(
            self.api,
            symbols,
            tradeapi.rest.TimeFrame.Day,
            start,
            end,
            "DAILY"
        )

        minute_start = self.tz_ny.localize(datetime.strptime(START_DATE, "%Y-%m-%d"))
        minute_end = self.tz_ny.localize(datetime.strptime(END_DATE, "%Y-%m-%d")) + timedelta(days=1)

        self.minute_data = fetch_bars_bulk(
            self.api,
            symbols,
            tradeapi.rest.TimeFrame.Minute,
            minute_start,
            minute_end,
            "MINUTE"
        )

        print(
            f"✅ Data loaded | Daily symbols={len(self.daily_data)} | Minute symbols={len(self.minute_data)}",
            flush=True
        )

    def update_radar_watchlist(self, symbol, current_price, prev_close, today_vol, now_ts):
        if prev_close <= 0:
            return False

        change_pct = ((current_price - prev_close) / prev_close) * 100
        dollar_volume = current_price * today_vol

        if change_pct < RADAR_TRIGGER_CHANGE_PCT:
            return False
        if dollar_volume < RADAR_MIN_DOLLAR_VOLUME:
            return False

        existing = self.radar_watchlist.get(symbol, {})
        previous_gain = existing.get("last_gain", change_pct)
        gain_trend = change_pct - previous_gain

        self.radar_watchlist[symbol] = {
            "first_seen": existing.get("first_seen", now_ts),
            "last_seen": now_ts,
            "highest_gain": max(existing.get("highest_gain", change_pct), change_pct),
            "highest_dollar_volume": max(existing.get("highest_dollar_volume", dollar_volume), dollar_volume),
            "last_gain": change_pct,
            "gain_trend": gain_trend
        }

        return True

    def clean_radar_watchlist(self, now_ts):
        expired = []
        for symbol, data in self.radar_watchlist.items():
            if now_ts - data.get("first_seen", now_ts) > RADAR_EXPIRE_MINUTES * 60:
                expired.append(symbol)

        for symbol in expired:
            self.radar_watchlist.pop(symbol, None)

    def get_previous_daily_context(self, symbol, current_day):
        df = self.daily_data.get(symbol)
        if df is None or df.empty:
            return None

        df = df.copy()
        df["date"] = df.index.tz_convert(self.tz_ny).date
        previous = df[df["date"] < current_day]

        if len(previous) < 50:
            return None

        return previous

    def calculate_signal(self, symbol, now_dt_ny, day_minute_df, current_idx):
        asset = self.assets_by_symbol.get(symbol, {})
        asset_name = asset.get("name", "")

        if symbol in SYMBOL_BLACKLIST:
            self.reject("Blacklist")
            return None

        if any(kw in asset_name.lower() for kw in BAD_NAME_KEYWORDS):
            self.reject("BadName")
            return None

        current_row = day_minute_df.iloc[current_idx]
        current_price = safe_float(current_row["close"])
        today_vol = safe_float(day_minute_df.iloc[:current_idx + 1]["volume"].sum())

        current_day = now_dt_ny.date()
        previous_bars = self.get_previous_daily_context(symbol, current_day)

        if previous_bars is None or len(previous_bars) < 50:
            self.reject("History")
            return None

        prev_close = safe_float(previous_bars["close"].iloc[-1])
        if prev_close <= 0:
            self.reject("PrevBars")
            return None

        price_change_pct = ((current_price - prev_close) / prev_close) * 100

        if not (PRICE_MIN <= current_price <= PRICE_MAX):
            self.reject("Price")
            return None

        avg_vol_20 = safe_float(previous_bars["volume"].tail(20).mean())

        if avg_vol_20 < MIN_AVG_VOL or avg_vol_20 > MAX_AVG_VOL:
            self.reject("AvgVol")
            return None

        if price_change_pct < MIN_PRICE_CHANGE:
            self.reject("PriceChange")
            return None

        rvol = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0
        if rvol < RVOL_MIN:
            self.reject("RVOL")
            return None

        dollar_volume = today_vol * current_price
        if dollar_volume < MIN_DOLLAR_VOLUME:
            self.reject("DollarVol")
            return None

        resistance_20 = safe_float(previous_bars["high"].tail(20).max())
        resistance_50 = safe_float(previous_bars["high"].tail(50).max())

        if current_price < resistance_20 * 0.99:
            self.reject("Resistance")
            return None

        bars_1m = day_minute_df.iloc[max(0, current_idx - 19):current_idx + 1]
        acceleration_data = calculate_volume_acceleration(bars_1m)

        obv_bonus_raw, obv_tier = calculate_obv_bonus(bars_1m)
        obv_bonus = min(obv_bonus_raw, 10)

        float_info = self.float_cache.get(symbol)
        real_float = None
        if isinstance(float_info, dict):
            real_float = float_info.get("float")

        float_bonus_raw, float_tier = get_float_bonus(real_float)
        float_bonus = min(float_bonus_raw, 5)

        radar_data = self.radar_watchlist.get(symbol, {})
        gain_trend = radar_data.get("gain_trend", 0)

        score = 0

        if rvol >= 10:
            score += 25
        elif rvol >= 5:
            score += 20
        elif rvol >= RVOL_MIN:
            score += 15

        if price_change_pct >= 20:
            score += 20
        elif price_change_pct >= 10:
            score += 12
        elif price_change_pct >= MIN_PRICE_CHANGE:
            score += 8

        if current_price >= resistance_20:
            score += 15
        elif current_price >= resistance_20 * 0.99:
            score += 8

        score += acceleration_data["volume_acceleration_score"]

        if not acceleration_data["volume_trend_up"] and acceleration_data["volume_peak_recent"]:
            score -= 8

        if dollar_volume >= 10_000_000:
            score += 10
        elif dollar_volume >= 2_000_000:
            score += 7
        elif dollar_volume >= MIN_DOLLAR_VOLUME:
            score += 5

        if (
            acceleration_data["last_1m_vs_avg"] < 0.8
            and not acceleration_data["volume_trend_up"]
            and not acceleration_data["volume_peak_recent"]
        ):
            self.reject("Score")
            return None

        if gain_trend >= 1.0:
            score += 5
        elif gain_trend >= 0.5:
            score += 3
        elif gain_trend > 0:
            score += 1

        score += obv_bonus
        score += float_bonus

        if gain_trend <= 0:
            self.reject("Score")
            return None

        if acceleration_data["vol_acceleration"] < 1.0 and obv_bonus == 0:
            self.reject("Score")
            return None

        score = min(score, 100)

        if score < EXPLOSION_CANDIDATE_MIN_SCORE:
            self.reject("Score")
            self.track_missed(
                symbol=symbol,
                now_dt_ny=now_dt_ny,
                day_minute_df=day_minute_df,
                current_idx=current_idx,
                current_price=current_price,
                score=score,
                reason=f"Score={score}"
            )
            return None

        digits = 4 if current_price < 1 else 2
        atr_14 = calculate_atr_14(previous_bars)

        stop_loss = round(current_price * 0.93, digits)
        target1 = round(current_price + atr_14, digits)
        target2 = round(current_price + (atr_14 * 2), digits)
        target3 = round(max(resistance_50, current_price + (atr_14 * 3)), digits)

        return {
            "symbol": symbol,
            "alert_time_ny": now_dt_ny,
            "alert_time_ksa": now_dt_ny.astimezone(self.tz_ksa),
            "price": round(current_price, digits),
            "rvol": round(rvol, 2),
            "change_pct": round(price_change_pct, 2),
            "score": int(score),
            "score_bucket": get_score_bucket(score),
            "price_bucket": get_price_bucket(current_price),
            "time_window": get_time_window(now_dt_ny),
            "float_tier": float_tier,
            "real_float": round(real_float, 0) if real_float else None,
            "float_bonus": float_bonus,
            "obv_bonus": obv_bonus,
            "obv_tier": obv_tier,
            "resistance_20": round(resistance_20, digits),
            "resistance_50": round(resistance_50, digits),
            "atr_14": round(atr_14, digits),
            "vol_acceleration": acceleration_data["vol_acceleration"],
            "volume_acceleration_score": acceleration_data["volume_acceleration_score"],
            "last_1m_vs_avg": acceleration_data["last_1m_vs_avg"],
            "last_3m_vs_prev_7m": acceleration_data["last_3m_vs_prev_7m"],
            "volume_trend_up": acceleration_data["volume_trend_up"],
            "volume_peak_recent": acceleration_data["volume_peak_recent"],
            "dollar_volume": round(dollar_volume, 0),
            "target1": target1,
            "target2": target2,
            "target3": target3,
            "stop_loss": stop_loss,
            "_day_minute_df": day_minute_df,
            "_current_idx": current_idx
        }

    def monitor_alert(self, alert):
        df = alert["_day_minute_df"]
        start_idx = alert["_current_idx"] + 1

        entry = alert["price"]
        t1 = alert["target1"]
        t2 = alert["target2"]
        t3 = alert["target3"]
        sl = alert["stop_loss"]

        h1 = h2 = h3 = False
        t1_time = t2_time = t3_time = None
        sl_hit = False
        sl_time = None

        max_price = entry
        max_gain = 0.0

        close_price = entry
        exit_status = "EOD"

        for i in range(start_idx, len(df)):
            row = df.iloc[i]
            ts = df.index[i].tz_convert(self.tz_ny)
            high = safe_float(row["high"])
            low = safe_float(row["low"])
            close = safe_float(row["close"])

            close_price = close

            if high > max_price:
                max_price = high
                max_gain = ((max_price - entry) / entry) * 100

            if low <= sl:
                sl_hit = True
                sl_time = ts
                exit_status = "STOP"
                break

            if high >= t1 and not h1:
                h1 = True
                t1_time = ts

            if high >= t2 and not h2:
                h2 = True
                t2_time = ts

            if high >= t3 and not h3:
                h3 = True
                t3_time = ts

        close_gain = ((close_price - entry) / entry) * 100

        alert["t1_hit"] = h1
        alert["t2_hit"] = h2
        alert["t3_hit"] = h3
        alert["stop_hit"] = sl_hit

        alert["t1_time_ny"] = t1_time
        alert["t2_time_ny"] = t2_time
        alert["t3_time_ny"] = t3_time
        alert["stop_time_ny"] = sl_time

        alert["t1_minutes"] = self.minutes_between(alert["alert_time_ny"], t1_time)
        alert["t2_minutes"] = self.minutes_between(alert["alert_time_ny"], t2_time)
        alert["t3_minutes"] = self.minutes_between(alert["alert_time_ny"], t3_time)
        alert["stop_minutes"] = self.minutes_between(alert["alert_time_ny"], sl_time)

        alert["max_price"] = round(max_price, 4 if max_price < 1 else 2)
        alert["max_gain_pct"] = round(max_gain, 2)
        alert["close_price"] = round(close_price, 4 if close_price < 1 else 2)
        alert["close_gain_pct"] = round(close_gain, 2)
        alert["exit_status"] = exit_status

        alert.pop("_day_minute_df", None)
        alert.pop("_current_idx", None)

        return alert

    @staticmethod
    def minutes_between(start, end):
        if end is None:
            return None
        return round((end - start).total_seconds() / 60, 1)

    def track_missed(self, symbol, now_dt_ny, day_minute_df, current_idx, current_price, score, reason):
        future = day_minute_df.iloc[current_idx + 1:]
        if future.empty:
            return

        max_future_high = safe_float(future["high"].max())
        future_gain = ((max_future_high - current_price) / current_price) * 100 if current_price > 0 else 0

        if future_gain >= 20:
            self.missed.append({
                "symbol": symbol,
                "time_ny": now_dt_ny,
                "price": round(current_price, 4 if current_price < 1 else 2),
                "score": score,
                "future_max_gain_pct": round(future_gain, 2),
                "reason": reason
            })

    def run_day(self, current_day):
        runtime["current_day"] = str(current_day)
        print(f"📅 Backtesting day: {current_day}", flush=True)

        day_start = self.tz_ny.localize(datetime.combine(current_day, datetime.strptime(MARKET_START_NY, "%H:%M").time()))
        day_end = self.tz_ny.localize(datetime.combine(current_day, datetime.strptime(MARKET_END_NY, "%H:%M").time()))

        for symbol, full_minute_df in self.minute_data.items():
            if symbol not in self.assets_by_symbol:
                continue

            df = full_minute_df.copy()
            df_ny = df.index.tz_convert(self.tz_ny)
            day_df = df[(df_ny >= day_start) & (df_ny <= day_end)].copy()

            if day_df.empty or len(day_df) < 30:
                continue

            for idx in range(20, len(day_df), SCAN_INTERVAL_MINUTES):
                now_dt_ny = day_df.index[idx].tz_convert(self.tz_ny)
                now_ts = now_dt_ny.timestamp()

                self.clean_radar_watchlist(now_ts)

                current_price = safe_float(day_df.iloc[idx]["close"])
                today_vol = safe_float(day_df.iloc[:idx + 1]["volume"].sum())

                previous_bars = self.get_previous_daily_context(symbol, current_day)
                if previous_bars is None:
                    continue

                prev_close = safe_float(previous_bars["close"].iloc[-1])
                if prev_close <= 0:
                    continue

                if not self.update_radar_watchlist(symbol, current_price, prev_close, today_vol, now_ts):
                    continue

                if symbol not in self.radar_watchlist:
                    continue

                last_alert_ts = self.sent_alerts.get(symbol)
                if last_alert_ts and now_ts - last_alert_ts < ALERT_COOLDOWN_SEC:
                    continue

                alert = self.calculate_signal(symbol, now_dt_ny, day_df, idx)

                if alert:
                    final_alert = self.monitor_alert(alert)
                    self.alerts.append(final_alert)
                    self.sent_alerts[symbol] = now_ts
                    runtime["alerts"] = len(self.alerts)

                    print(
                        f"🚀 ALERT {symbol} | {now_dt_ny} | Score={final_alert['score']} | MaxGain={final_alert['max_gain_pct']}%",
                        flush=True
                    )

    def run(self):
        runtime["status"] = "running"
        runtime["started_at"] = datetime.now(self.tz_ksa).strftime("%Y-%m-%d %H:%M:%S")

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        self.load_data()

        start_d = datetime.strptime(START_DATE, "%Y-%m-%d").date()
        end_d = datetime.strptime(END_DATE, "%Y-%m-%d").date()

        d = start_d
        while d <= end_d:
            if d.weekday() < 5:
                self.run_day(d)
            d += timedelta(days=1)

        self.save_results()
        runtime["status"] = "finished"
        runtime["finished_at"] = datetime.now(self.tz_ksa).strftime("%Y-%m-%d %H:%M:%S")

        summary_msg = self.build_telegram_summary()
        print(summary_msg, flush=True)
        send_telegram_message(summary_msg)

        print("✅ Backtest completed. Exiting process.", flush=True)
        time.sleep(5)
        os._exit(0)

    def save_results(self):
        alerts_df = pd.DataFrame(self.alerts)

        if not alerts_df.empty:
            for col in ["alert_time_ny", "alert_time_ksa", "t1_time_ny", "t2_time_ny", "t3_time_ny", "stop_time_ny"]:
                if col in alerts_df.columns:
                    alerts_df[col] = alerts_df[col].astype(str)

        alerts_path = os.path.join(OUTPUT_DIR, "backtest_alerts.csv")
        alerts_df.to_csv(alerts_path, index=False)

        reject_df = pd.DataFrame([
            {"reason": k, "count": v}
            for k, v in sorted(self.reject_stats.items(), key=lambda x: x[1], reverse=True)
        ])
        reject_df.to_csv(os.path.join(OUTPUT_DIR, "backtest_reject_stats.csv"), index=False)

        missed_df = pd.DataFrame(self.missed)
        if not missed_df.empty:
            missed_df = missed_df.sort_values("future_max_gain_pct", ascending=False).head(50)
            missed_df["time_ny"] = missed_df["time_ny"].astype(str)
        missed_df.to_csv(os.path.join(OUTPUT_DIR, "backtest_missed_opportunities.csv"), index=False)

        summary_rows = self.build_summary_rows(alerts_df)
        summary_df = pd.DataFrame(summary_rows)
        summary_df.to_csv(os.path.join(OUTPUT_DIR, "backtest_summary.csv"), index=False)

        self.generate_charts(alerts_df, reject_df)

        print(f"✅ Files saved in {OUTPUT_DIR}/", flush=True)

    def build_summary_rows(self, alerts_df):
        total = len(alerts_df)
        if total == 0:
            return [{"metric": "total_alerts", "value": 0}]

        return [
            {"metric": "period", "value": f"{START_DATE} to {END_DATE}"},
            {"metric": "symbols_loaded", "value": runtime["symbols_loaded"]},
            {"metric": "total_alerts", "value": total},
            {"metric": "unique_symbols", "value": alerts_df["symbol"].nunique()},
            {"metric": "t1_hits", "value": int(alerts_df["t1_hit"].sum())},
            {"metric": "t1_rate_pct", "value": round(alerts_df["t1_hit"].mean() * 100, 2)},
            {"metric": "t2_hits", "value": int(alerts_df["t2_hit"].sum())},
            {"metric": "t2_rate_pct", "value": round(alerts_df["t2_hit"].mean() * 100, 2)},
            {"metric": "t3_hits", "value": int(alerts_df["t3_hit"].sum())},
            {"metric": "t3_rate_pct", "value": round(alerts_df["t3_hit"].mean() * 100, 2)},
            {"metric": "stop_hits", "value": int(alerts_df["stop_hit"].sum())},
            {"metric": "stop_rate_pct", "value": round(alerts_df["stop_hit"].mean() * 100, 2)},
            {"metric": "avg_score", "value": round(alerts_df["score"].mean(), 2)},
            {"metric": "avg_max_gain_pct", "value": round(alerts_df["max_gain_pct"].mean(), 2)},
            {"metric": "best_trade_symbol", "value": alerts_df.sort_values("max_gain_pct", ascending=False).iloc[0]["symbol"]},
            {"metric": "best_trade_gain_pct", "value": round(alerts_df["max_gain_pct"].max(), 2)},
        ]

    def generate_charts(self, alerts_df, reject_df):
        if alerts_df.empty:
            return

        alerts_df["alert_date"] = pd.to_datetime(alerts_df["alert_time_ny"]).dt.date

        chart_specs = [
            ("alerts_by_day.png", alerts_df.groupby("alert_date").size(), "Alerts by Day"),
            ("score_distribution.png", alerts_df["score"], "Score Distribution"),
            ("max_gain_distribution.png", alerts_df["max_gain_pct"], "Max Gain Distribution"),
        ]

        for filename, data, title in chart_specs:
            plt.figure()
            if hasattr(data, "plot"):
                data.plot(kind="bar" if filename == "alerts_by_day.png" else "hist")
            plt.title(title)
            plt.tight_layout()
            plt.savefig(os.path.join(OUTPUT_DIR, filename))
            plt.close()

        for col, filename, title in [
            ("score_bucket", "by_score_bucket.png", "Performance by Score Bucket"),
            ("time_window", "by_time_window.png", "Performance by Time Window"),
            ("float_tier", "by_float_tier.png", "Performance by Float Tier"),
            ("price_bucket", "by_price_bucket.png", "Performance by Price Bucket"),
        ]:
            if col in alerts_df.columns:
                grouped = alerts_df.groupby(col)["t1_hit"].mean() * 100
                plt.figure()
                grouped.plot(kind="bar")
                plt.title(title + " - T1 Rate %")
                plt.tight_layout()
                plt.savefig(os.path.join(OUTPUT_DIR, filename))
                plt.close()

        if not reject_df.empty:
            plt.figure()
            reject_df.head(12).set_index("reason")["count"].plot(kind="bar")
            plt.title("Top Reject Reasons")
            plt.tight_layout()
            plt.savefig(os.path.join(OUTPUT_DIR, "reject_reasons.png"))
            plt.close()

    def build_telegram_summary(self):
        alerts_df = pd.DataFrame(self.alerts)

        if alerts_df.empty:
            return (
                "📊 *Early Explosion Backtest انتهى*\n\n"
                f"الفترة: `{START_DATE}` → `{END_DATE}`\n"
                "لم يتم تسجيل أي تنبيه مطابق للشروط."
            )

        total = len(alerts_df)
        t1 = alerts_df["t1_hit"].mean() * 100
        t2 = alerts_df["t2_hit"].mean() * 100
        t3 = alerts_df["t3_hit"].mean() * 100
        stop = alerts_df["stop_hit"].mean() * 100
        avg_gain = alerts_df["max_gain_pct"].mean()

        best = alerts_df.sort_values("max_gain_pct", ascending=False).iloc[0]

        return (
            "📊 *Early Explosion Backtest انتهى*\n\n"
            f"🗓️ الفترة: `{START_DATE}` → `{END_DATE}`\n"
            f"📌 الأسهم المحملة: `{runtime['symbols_loaded']}`\n"
            f"🚀 عدد التنبيهات: `{total}`\n"
            f"🎫 الأسهم الفريدة: `{alerts_df['symbol'].nunique()}`\n\n"
            f"✅ T1: `{t1:.1f}%`\n"
            f"🔥 T2: `{t2:.1f}%`\n"
            f"🚀 T3: `{t3:.1f}%`\n"
            f"🛑 Stop: `{stop:.1f}%`\n\n"
            f"📈 متوسط أعلى ربح: `{avg_gain:.2f}%`\n"
            f"🏆 أفضل صفقة: `{best['symbol']}` | `{best['max_gain_pct']}%`\n\n"
            f"📁 تم حفظ CSV والرسوم داخل `{OUTPUT_DIR}`.\n"
            "✅ انتهى الفحص بالكامل وتوقف السكربت."
        )


# =========================================================
# ENTRY
# =========================================================

def start_backtest_thread():
    def runner():
        try:
            bt = EarlyExplosionBacktester()
            bt.run()
        except Exception as e:
            runtime["status"] = "error"
            runtime["error"] = str(e)
            send_telegram_message(f"❌ Early Explosion Backtest Error:\n`{e}`")
            print(f"❌ Fatal error: {e}", flush=True)

    t = threading.Thread(target=runner, daemon=True)
    t.start()


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    start_backtest_thread()

    while True:
        time.sleep(60)
