import os
import json
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import gc
import pytz
import requests
import pandas as pd
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
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

CHECKPOINT_STATE_FILE = "backtest_checkpoint_state.json"
CHECKPOINT_ALERTS_FILE = "backtest_daily_alerts.json"
CHECKPOINT_MISSED_FILE = "backtest_daily_missed.json"
CHECKPOINT_REJECTS_FILE = "backtest_daily_rejects.json"

START_DATE = os.getenv("BACKTEST_START_DATE", "2026-06-01")
END_DATE = os.getenv("BACKTEST_END_DATE", "2026-07-02")

OUTPUT_DIR = os.getenv("BACKTEST_OUTPUT_DIR", "backtest_output")

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
SCAN_INTERVAL_MINUTES = 3
DAILY_BATCH_SIZE = 700
MINUTE_BATCH_SIZE = 150
BULK_SLEEP_SEC = 0.5

MARKET_START_NY = "04:00"
MARKET_END_NY = "20:00"

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
    "symbols_after_asset_filter": 0,
    "daily_symbols_loaded": 0,
    "minute_symbols_loaded": 0,
    "float_records": 0,
    "alerts": 0,
    "error": None,
}

@app.route("/")
def home():
    return f"""
    <h2>⚡ Early Explosion Backtest v1.0</h2>
    <p>Status: {runtime['status']}</p>
    <p>Started: {runtime['started_at']}</p>
    <p>Finished: {runtime['finished_at']}</p>
    <p>Current Day: {runtime['current_day']}</p>
    <p>Symbols After Asset Filter: {runtime['symbols_after_asset_filter']}</p>
    <p>Daily Symbols Loaded: {runtime['daily_symbols_loaded']}</p>
    <p>Minute Symbols Loaded: {runtime['minute_symbols_loaded']}</p>
    <p>Float Records: {runtime['float_records']}</p>
    <p>Alerts: {runtime['alerts']}</p>
    <p>Error: {runtime['error']}</p>
    """, 200


def run_flask() -> None:
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# =========================================================
# GENERAL HELPERS
# =========================================================

def now_ksa_str() -> str:
    return datetime.now(pytz.timezone("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M:%S")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_INVESTMENT_CHAT_ID:
        print(f"[Telegram-Sim]\n{text}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_INVESTMENT_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=15,
        )
    except Exception as exc:
        print(f"❌ Telegram error: {exc}", flush=True)

def send_telegram_document(filepath: str, caption: str = "") -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_INVESTMENT_CHAT_ID:
        print(f"[Telegram-Document-Sim] {filepath}", flush=True)
        return

    if not os.path.exists(filepath):
        print(f"⚠️ Telegram document not found: {filepath}", flush=True)
        return

    try:
        with open(filepath, "rb") as f:
            res = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={
                    "chat_id": TELEGRAM_INVESTMENT_CHAT_ID,
                    "caption": caption,
                },
                files={"document": f},
                timeout=60,
            )

        if res.status_code == 200:
            print(f"✅ Sent Telegram document: {filepath}", flush=True)
        else:
            print(
                f"⚠️ Telegram document failed {filepath}: HTTP {res.status_code} | {res.text}",
                flush=True,
            )

    except Exception as exc:
        print(f"❌ Telegram document error ({filepath}): {exc}", flush=True)
        
def load_float_cache() -> Dict[str, Any]:
    if not FLOAT_CACHE_URL:
        raise RuntimeError("FLOAT_CACHE_URL is missing. Backtest stopped to avoid incomplete float data.")

    print("📥 Loading float cache from FLOAT_CACHE_URL...", flush=True)
    try:
        res = requests.get(FLOAT_CACHE_URL, timeout=40)
        if res.status_code != 200:
            raise RuntimeError(f"HTTP {res.status_code}")
        data = res.json()
        if not isinstance(data, dict) or len(data) == 0:
            raise RuntimeError("float cache is empty or invalid")
        print(f"✅ Float records loaded: {len(data)}", flush=True)
        runtime["float_records"] = len(data)
        return data
    except Exception as exc:
        raise RuntimeError(f"Failed to load float cache from FLOAT_CACHE_URL: {exc}")


def get_api() -> tradeapi.REST:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("Missing Alpaca env vars: APCA_API_KEY_ID / APCA_API_SECRET_KEY")
    return tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")

# =========================================================
# INDICATORS / BOT LOGIC HELPERS
# =========================================================

def calculate_atr_14(df: pd.DataFrame) -> float:
    if df is None or df.empty or len(df) < 14:
        return 0.0
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return safe_float(tr.tail(14).mean())


def calculate_obv_bonus(bars_1m: pd.DataFrame) -> Tuple[int, str]:
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


def calculate_volume_acceleration(bars_1m: pd.DataFrame) -> Dict[str, Any]:
    if bars_1m is None or bars_1m.empty or len(bars_1m) < 13:
        return {
            "volume_acceleration_score": 0,
            "vol_acceleration": 0.0,
            "last_1m_vs_avg": 0.0,
            "last_3m_vs_prev_7m": 0.0,
            "volume_trend_up": False,
            "volume_peak_recent": False,
        }
    bars_1m = bars_1m.sort_index()
    volumes = bars_1m["volume"].astype(float)
    last_1m = safe_float(volumes.iloc[-1])
    avg_prev_10 = safe_float(volumes.iloc[-11:-1].mean())
    last_1m_vs_avg = last_1m / avg_prev_10 if avg_prev_10 > 0 else 0.0
    last_3m_avg = safe_float(volumes.iloc[-3:].mean())
    prev_7m_avg = safe_float(volumes.iloc[-10:-3].mean())
    last_3m_vs_prev_7m = last_3m_avg / prev_7m_avg if prev_7m_avg > 0 else 0.0
    v1, v2, v3 = safe_float(volumes.iloc[-3]), safe_float(volumes.iloc[-2]), safe_float(volumes.iloc[-1])
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
        "volume_peak_recent": bool(volume_peak_recent),
    }


def get_float_bonus(real_float: Optional[float]) -> Tuple[int, str]:
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


def score_bucket(score: float) -> str:
    if score <= 92:
        return "90-92"
    if score <= 95:
        return "93-95"
    return "96-100"


def price_bucket(price: float) -> str:
    if price < 1:
        return "<1"
    if price < 5:
        return "1-5"
    if price < 10:
        return "5-10"
    return "10-25"


def time_window(dt_ny: datetime) -> str:
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
# DATA LOADERS
# =========================================================

def clean_assets(api: tradeapi.REST) -> List[Dict[str, str]]:
    print("📋 Loading Alpaca assets...", flush=True)
    assets = api.list_assets(status="active")
    cleaned: List[Dict[str, str]] = []
    for asset in assets:
        symbol = asset.symbol
        name = getattr(asset, "name", "") or ""
        raw = getattr(asset, "_raw", {}) or {}
        if not getattr(asset, "tradable", False):
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
        cleaned.append({"symbol": symbol, "name": name, "exchange": raw.get("exchange", "")})
    print(f"✅ Assets after initial filter: {len(cleaned)}", flush=True)
    runtime["symbols_after_asset_filter"] = len(cleaned)
    return cleaned


def normalize_bars_df(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    if df is None or df.empty or "symbol" not in df.columns:
        return {}
    df = df.reset_index()
    time_col = "timestamp" if "timestamp" in df.columns else df.columns[0]
    df[time_col] = pd.to_datetime(df[time_col], utc=True)
    out: Dict[str, pd.DataFrame] = {}
    for sym, group in df.groupby("symbol"):
        g = group.copy().set_index(time_col).sort_index()
        out[sym] = g
    return out

def fetch_bars_bulk(
    api: tradeapi.REST,
    symbols: List[str],
    timeframe: Any,
    start_dt: datetime,
    end_dt: datetime,
    label: str,
    batch_size: int,
) -> Dict[str, pd.DataFrame]:
    all_data: Dict[str, pd.DataFrame] = {}
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        batch_no = i // batch_size + 1

        print(
            f"📥 Loading {label} batch {batch_no}/{total_batches} | symbols={len(batch)}",
            flush=True,
        )

        try:
            bars = api.get_bars(
                batch,
                timeframe,
                start=start_dt.isoformat(),
                end=end_dt.isoformat(),
                adjustment="raw",
                feed="iex",
            ).df

            all_data.update(normalize_bars_df(bars))

        except Exception as exc:
            print(f"⚠️ {label} batch error: {exc}", flush=True)

        time.sleep(BULK_SLEEP_SEC)

    print(f"✅ {label} loaded for {len(all_data)} symbols", flush=True)
    return all_data

def fetch_minute_bars_for_day(
    api: tradeapi.REST,
    symbols: List[str],
    current_day,
    tz_ny,
) -> Dict[str, pd.DataFrame]:
    day_start = tz_ny.localize(
        datetime.combine(
            current_day,
            datetime.strptime(MARKET_START_NY, "%H:%M").time(),
        )
    )

    day_end = tz_ny.localize(
        datetime.combine(
            current_day,
            datetime.strptime(MARKET_END_NY, "%H:%M").time(),
        )
    )

    all_data: Dict[str, pd.DataFrame] = {}
    total_batches = (len(symbols) + MINUTE_BATCH_SIZE - 1) // MINUTE_BATCH_SIZE

    print(f"📅 Loading MINUTE data for {current_day}", flush=True)

    for i in range(0, len(symbols), MINUTE_BATCH_SIZE):
        batch = symbols[i:i + MINUTE_BATCH_SIZE]
        batch_no = i // MINUTE_BATCH_SIZE + 1

        print(
            f"📥 MINUTE {current_day} batch {batch_no}/{total_batches} | symbols={len(batch)}",
            flush=True,
        )

        try:
            bars = api.get_bars(
                batch,
                tradeapi.rest.TimeFrame.Minute,
                start=day_start.isoformat(),
                end=day_end.isoformat(),
                adjustment="raw",
                feed="iex",
            ).df

            all_data.update(normalize_bars_df(bars))

        except Exception as exc:
            print(
                f"⚠️ MINUTE {current_day} batch {batch_no} error: {exc}",
                flush=True,
            )

        time.sleep(BULK_SLEEP_SEC)

    print(f"✅ MINUTE {current_day} loaded for {len(all_data)} symbols", flush=True)
    return all_data

def gist_headers() -> Dict[str, str]:
    if not GITHUB_TOKEN or not GIST_ID:
        raise RuntimeError("GITHUB_TOKEN or GIST_ID is missing for checkpointing.")

    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def load_gist_file_json(filename: str, default_value: Any) -> Any:
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        res = requests.get(url, headers=gist_headers(), timeout=20)

        if res.status_code != 200:
            print(f"⚠️ Gist load failed {filename}: HTTP {res.status_code}", flush=True)
            return default_value

        files = res.json().get("files", {})

        if filename not in files:
            return default_value

        content = files[filename].get("content", "")

        if not content:
            return default_value

        return json.loads(content)

    except Exception as exc:
        print(f"⚠️ Gist load error {filename}: {exc}", flush=True)
        return default_value


def save_gist_file_json(filename: str, data: Any) -> None:
    url = f"https://api.github.com/gists/{GIST_ID}"

    payload = {
        "files": {
            filename: {
                "content": json.dumps(data, indent=2, default=str)
            }
        }
    }

    res = requests.patch(
        url,
        headers=gist_headers(),
        json=payload,
        timeout=40,
    )

    if res.status_code not in [200, 201]:
        raise RuntimeError(f"Gist save failed {filename}: HTTP {res.status_code}")

    print(f"✅ Saved checkpoint file to Gist: {filename}", flush=True)
    
# =========================================================
# BACKTEST ENGINE
# =========================================================

class EarlyExplosionBacktester:
    def __init__(self) -> None:
        self.tz_ny = pytz.timezone("America/New_York")
        self.tz_ksa = pytz.timezone("Asia/Riyadh")
        self.api = get_api()
        self.float_cache = load_float_cache()
        self.assets_by_symbol: Dict[str, Dict[str, str]] = {}
        self.daily_data: Dict[str, pd.DataFrame] = {}
        self.minute_data: Dict[str, pd.DataFrame] = {}
        self.radar_watchlist: Dict[str, Dict[str, Any]] = {}
        self.sent_alerts: Dict[str, float] = {}
        self.alerts: List[Dict[str, Any]] = []
        self.reject_stats: Dict[str, int] = {}
        self.missed: List[Dict[str, Any]] = []
        self.daily_reject_start: Dict[str, int] = {}

    def reject(self, reason: str) -> None:
        self.reject_stats[reason] = self.reject_stats.get(reason, 0) + 1
        
    def get_last_completed_day(self) -> Optional[str]:
        state = load_gist_file_json(CHECKPOINT_STATE_FILE, {})
        return state.get("last_completed_day")

    def save_day_checkpoint(self, current_day) -> None:
        day_key = str(current_day)

        all_alerts = load_gist_file_json(CHECKPOINT_ALERTS_FILE, {})
        all_missed = load_gist_file_json(CHECKPOINT_MISSED_FILE, {})
        all_rejects = load_gist_file_json(CHECKPOINT_REJECTS_FILE, {})

        alerts_df = self.alerts_df()
        day_alerts = []

        if not alerts_df.empty and "alert_time_ny" in alerts_df.columns:
            temp = alerts_df.copy()
            temp["_day"] = pd.to_datetime(
                temp["alert_time_ny"],
                errors="coerce"
            ).dt.date.astype(str)

            day_alerts = temp[temp["_day"] == day_key].drop(
                columns=["_day"],
                errors="ignore"
            ).to_dict("records")

        day_missed = []

        for item in self.missed:
            item_time = item.get("time_ny")

            if item_time is None:
                continue

            item_day = str(pd.to_datetime(item_time).date())

            if item_day == day_key:
                day_missed.append({
                    k: str(v) if "time" in k else v
                    for k, v in item.items()
                })

        day_rejects = {}

        for reason, count in self.reject_stats.items():
            start_count = self.daily_reject_start.get(reason, 0)
            delta = count - start_count

            if delta > 0:
                day_rejects[reason] = delta

        all_alerts[day_key] = day_alerts
        all_missed[day_key] = day_missed
        all_rejects[day_key] = day_rejects

        save_gist_file_json(CHECKPOINT_ALERTS_FILE, all_alerts)
        save_gist_file_json(CHECKPOINT_MISSED_FILE, all_missed)
        save_gist_file_json(CHECKPOINT_REJECTS_FILE, all_rejects)

        save_gist_file_json(
            CHECKPOINT_STATE_FILE,
            {
                "last_completed_day": day_key,
                "updated_at_ksa": now_ksa_str(),
            }
        )

        print(
            f"✅ Daily checkpoint saved | {day_key} | "
            f"alerts={len(day_alerts)} | missed={len(day_missed)} | rejects={len(day_rejects)}",
            flush=True,
        )

    def load_checkpoint_results(self) -> None:
        all_alerts = load_gist_file_json(CHECKPOINT_ALERTS_FILE, {})
        all_missed = load_gist_file_json(CHECKPOINT_MISSED_FILE, {})
        all_rejects = load_gist_file_json(CHECKPOINT_REJECTS_FILE, {})

        self.alerts = []
        self.missed = []
        self.reject_stats = {}

        for day_items in all_alerts.values():
            if isinstance(day_items, list):
                self.alerts.extend(day_items)

        for day_items in all_missed.values():
            if isinstance(day_items, list):
                self.missed.extend(day_items)

        for day_rejects in all_rejects.values():
            if not isinstance(day_rejects, dict):
                continue

            for reason, count in day_rejects.items():
                self.reject_stats[reason] = self.reject_stats.get(reason, 0) + int(count)

        runtime["alerts"] = len(self.alerts)

        print(
            f"📦 Loaded checkpoint totals | alerts={len(self.alerts)} | "
            f"missed={len(self.missed)} | reject_reasons={len(self.reject_stats)}",
            flush=True,
        )

    def clear_daily_memory_after_checkpoint(self) -> None:
        self.alerts = []
        self.missed = []
        self.radar_watchlist = {}
        self.daily_reject_start = {}
        gc.collect()
        
    def load_data(self) -> None:
        assets = clean_assets(self.api)
        self.assets_by_symbol = {a["symbol"]: a for a in assets}

        symbols = list(self.assets_by_symbol.keys())

        start_daily = (
            self.tz_ny.localize(
                datetime.strptime(START_DATE, "%Y-%m-%d")
            )
            - timedelta(days=150)
        )

        end_all = (
            self.tz_ny.localize(
                datetime.strptime(END_DATE, "%Y-%m-%d")
            )
            + timedelta(days=1)
        )

        self.daily_data = fetch_bars_bulk(
            self.api,
            symbols,
            tradeapi.rest.TimeFrame.Day,
            start_daily,
            end_all,
            "DAILY",
            DAILY_BATCH_SIZE,
        )

        runtime["daily_symbols_loaded"] = len(self.daily_data)
                
    def get_previous_daily_context(self, symbol: str, current_day) -> Optional[pd.DataFrame]:
        df = self.daily_data.get(symbol)
        if df is None or df.empty:
            return None
        temp = df.copy()
        temp["_date"] = temp.index.tz_convert(self.tz_ny).date
        previous = temp[temp["_date"] < current_day].copy()
        previous.drop(columns=["_date"], inplace=True, errors="ignore")
        if len(previous) < 50:
            return None
        return previous

    def update_radar_watchlist(self, symbol: str, current_price: float, prev_close: float, today_vol: float, now_ts: float) -> bool:
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
            "gain_trend": gain_trend,
        }
        return True

    def clean_radar_watchlist(self, now_ts: float) -> None:
        expired = []
        for symbol, data in self.radar_watchlist.items():
            if now_ts - data.get("first_seen", now_ts) > RADAR_EXPIRE_MINUTES * 60:
                expired.append(symbol)
        for symbol in expired:
            self.radar_watchlist.pop(symbol, None)

    def calculate_signal(self, symbol: str, now_dt_ny: datetime, day_df: pd.DataFrame, idx: int) -> Optional[Dict[str, Any]]:
        asset = self.assets_by_symbol.get(symbol, {})
        asset_name = asset.get("name", "")
        if symbol in SYMBOL_BLACKLIST:
            self.reject("Blacklist")
            return None
        if any(kw in asset_name.lower() for kw in BAD_NAME_KEYWORDS):
            self.reject("BadName")
            return None
        current_row = day_df.iloc[idx]
        current_price = safe_float(current_row["close"])
        today_vol = safe_float(day_df.iloc[:idx + 1]["volume"].sum())
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
        rvol = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0.0
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
        bars_1m = day_df.iloc[max(0, idx - 19):idx + 1]
        acc = calculate_volume_acceleration(bars_1m)
        obv_bonus_raw, obv_tier = calculate_obv_bonus(bars_1m)
        obv_bonus = min(obv_bonus_raw, 10)
        float_info = self.float_cache.get(symbol)
        real_float = None
        if isinstance(float_info, dict):
            real_float = float_info.get("float")
        elif isinstance(float_info, (int, float)):
            real_float = float_info
        real_float = safe_float(real_float, None) if real_float is not None else None
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
        score += acc["volume_acceleration_score"]
        if not acc["volume_trend_up"] and acc["volume_peak_recent"]:
            score -= 8
        if dollar_volume >= 10_000_000:
            score += 10
        elif dollar_volume >= 2_000_000:
            score += 7
        elif dollar_volume >= MIN_DOLLAR_VOLUME:
            score += 5
        if acc["last_1m_vs_avg"] < 0.8 and not acc["volume_trend_up"] and not acc["volume_peak_recent"]:
            self.reject("Score")
            self.track_missed(symbol, now_dt_ny, day_df, idx, current_price, score, "cooled_volume")
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
            self.track_missed(symbol, now_dt_ny, day_df, idx, current_price, score, "gain_trend<=0")
            return None
        if acc["vol_acceleration"] < 1.0 and obv_bonus == 0:
            self.reject("Score")
            self.track_missed(symbol, now_dt_ny, day_df, idx, current_price, score, "accel<1_and_obv0")
            return None
        score = min(score, 100)
        if score < EXPLOSION_CANDIDATE_MIN_SCORE:
            self.reject("Score")
            self.track_missed(symbol, now_dt_ny, day_df, idx, current_price, score, f"Score={score}")
            return None
        digits = 4 if current_price < 1 else 2
        atr_14 = calculate_atr_14(previous_bars)
        stop_loss = round(current_price * 0.93, digits)
        target1 = round(current_price + atr_14, digits)
        target2 = round(current_price + atr_14 * 2, digits)
        target3 = round(max(resistance_50, current_price + atr_14 * 3), digits)
        return {
            "symbol": symbol,
            "asset_name": asset_name,
            "alert_time_ny": now_dt_ny,
            "alert_time_ksa": now_dt_ny.astimezone(self.tz_ksa),
            "price": round(current_price, digits),
            "rvol": round(rvol, 2),
            "change_pct": round(price_change_pct, 2),
            "score": int(score),
            "score_bucket": score_bucket(score),
            "price_bucket": price_bucket(current_price),
            "time_window": time_window(now_dt_ny),
            "float_tier": float_tier,
            "real_float": round(real_float, 0) if real_float else None,
            "float_bonus": float_bonus,
            "obv_bonus": obv_bonus,
            "obv_tier": obv_tier,
            "resistance_20": round(resistance_20, digits),
            "resistance_50": round(resistance_50, digits),
            "atr_14": round(atr_14, digits),
            "vol_acceleration": acc["vol_acceleration"],
            "volume_acceleration_score": acc["volume_acceleration_score"],
            "last_1m_vs_avg": acc["last_1m_vs_avg"],
            "last_3m_vs_prev_7m": acc["last_3m_vs_prev_7m"],
            "volume_trend_up": acc["volume_trend_up"],
            "volume_peak_recent": acc["volume_peak_recent"],
            "dollar_volume": round(dollar_volume, 0),
            "target1": target1,
            "target2": target2,
            "target3": target3,
            "stop_loss": stop_loss,
            "_day_df": day_df,
            "_idx": idx,
        }

    def monitor_alert(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        df = alert["_day_df"]
        start_idx = alert["_idx"] + 1
        entry = alert["price"]
        t1, t2, t3, sl = alert["target1"], alert["target2"], alert["target3"], alert["stop_loss"]
        h1 = h2 = h3 = False
        t1_time = t2_time = t3_time = stop_time = None
        stop_hit = False
        max_price = entry
        close_price = entry
        exit_status = "EOD"
        for i in range(start_idx, len(df)):
            row = df.iloc[i]
            ts = df.index[i].tz_convert(self.tz_ny)
            high, low, close = safe_float(row["high"]), safe_float(row["low"]), safe_float(row["close"])
            close_price = close
            if high > max_price:
                max_price = high
            # Conservative order: stop first when both target and stop exist in same minute.
            if low <= sl:
                stop_hit = True
                stop_time = ts
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
        max_gain_pct = ((max_price - entry) / entry) * 100 if entry > 0 else 0
        close_gain_pct = ((close_price - entry) / entry) * 100 if entry > 0 else 0
        alert.update({
            "t1_hit": h1,
            "t2_hit": h2,
            "t3_hit": h3,
            "stop_hit": stop_hit,
            "t1_time_ny": t1_time,
            "t2_time_ny": t2_time,
            "t3_time_ny": t3_time,
            "stop_time_ny": stop_time,
            "t1_time_ksa": t1_time.astimezone(self.tz_ksa) if t1_time else None,
            "t2_time_ksa": t2_time.astimezone(self.tz_ksa) if t2_time else None,
            "t3_time_ksa": t3_time.astimezone(self.tz_ksa) if t3_time else None,
            "stop_time_ksa": stop_time.astimezone(self.tz_ksa) if stop_time else None,
            "t1_minutes": self.minutes_between(alert["alert_time_ny"], t1_time),
            "t2_minutes": self.minutes_between(alert["alert_time_ny"], t2_time),
            "t3_minutes": self.minutes_between(alert["alert_time_ny"], t3_time),
            "stop_minutes": self.minutes_between(alert["alert_time_ny"], stop_time),
            "max_price": round(max_price, 4 if max_price < 1 else 2),
            "max_gain_pct": round(max_gain_pct, 2),
            "close_price": round(close_price, 4 if close_price < 1 else 2),
            "close_gain_pct": round(close_gain_pct, 2),
            "exit_status": exit_status,
        })
        alert.pop("_day_df", None)
        alert.pop("_idx", None)
        return alert

    @staticmethod
    def minutes_between(start: datetime, end: Optional[datetime]) -> Optional[float]:
        if end is None:
            return None
        return round((end - start).total_seconds() / 60, 1)

    def track_missed(self, symbol: str, now_dt_ny: datetime, day_df: pd.DataFrame, idx: int, current_price: float, score: float, reason: str) -> None:
        future = day_df.iloc[idx + 1:]
        if future.empty or current_price <= 0:
            return
        max_future_high = safe_float(future["high"].max())
        future_gain = ((max_future_high - current_price) / current_price) * 100
        if future_gain >= 20:
            self.missed.append({
                "symbol": symbol,
                "time_ny": now_dt_ny,
                "time_ksa": now_dt_ny.astimezone(self.tz_ksa),
                "price": round(current_price, 4 if current_price < 1 else 2),
                "score_at_reject": round(score, 2),
                "future_max_gain_pct": round(future_gain, 2),
                "reject_reason": reason,
            })

    def run_day(self, current_day) -> None:
        runtime["current_day"] = str(current_day)
        print(f"📅 Backtesting {current_day}", flush=True)

        symbols = list(self.assets_by_symbol.keys())

        minute_data_for_day = fetch_minute_bars_for_day(
            self.api,
            symbols,
            current_day,
            self.tz_ny,
        )

        runtime["minute_symbols_loaded"] = len(minute_data_for_day)

        day_start = self.tz_ny.localize(
            datetime.combine(
                current_day,
                datetime.strptime(MARKET_START_NY, "%H:%M").time(),
            )
        )

        day_end = self.tz_ny.localize(
            datetime.combine(
                current_day,
                datetime.strptime(MARKET_END_NY, "%H:%M").time(),
            )
        )

        for symbol, full_df in minute_data_for_day.items():
            if symbol not in self.assets_by_symbol:
                continue

            idx_ny = full_df.index.tz_convert(self.tz_ny)
            day_df = full_df[(idx_ny >= day_start) & (idx_ny <= day_end)].copy()

            if day_df.empty or len(day_df) < 30:
                continue

            for idx in range(20, len(day_df), SCAN_INTERVAL_MINUTES):
                now_dt_ny = day_df.index[idx].tz_convert(self.tz_ny)
                now_ts = now_dt_ny.timestamp()

                self.clean_radar_watchlist(now_ts)

                previous_bars = self.get_previous_daily_context(symbol, current_day)

                if previous_bars is None:
                    continue

                prev_close = safe_float(previous_bars["close"].iloc[-1])
                current_price = safe_float(day_df.iloc[idx]["close"])
                today_vol = safe_float(day_df.iloc[:idx + 1]["volume"].sum())

                if not self.update_radar_watchlist(
                    symbol,
                    current_price,
                    prev_close,
                    today_vol,
                    now_ts,
                ):
                    continue

                last_alert_ts = self.sent_alerts.get(symbol)

                if last_alert_ts and (now_ts - last_alert_ts < ALERT_COOLDOWN_SEC):
                    continue

                alert = self.calculate_signal(symbol, now_dt_ny, day_df, idx)

                if alert:
                    final_alert = self.monitor_alert(alert)
                    self.alerts.append(final_alert)
                    self.sent_alerts[symbol] = now_ts
                    runtime["alerts"] = len(self.alerts)

                    print(
                        f"🚀 ALERT {symbol} | {now_dt_ny.strftime('%Y-%m-%d %H:%M')} NY | "
                        f"Score={final_alert['score']} | MaxGain={final_alert['max_gain_pct']}%",
                        flush=True,
                    )

        del minute_data_for_day

    def run(self) -> None:
        runtime["status"] = "running"
        runtime["started_at"] = now_ksa_str()
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        self.load_data()

        start_d = datetime.strptime(START_DATE, "%Y-%m-%d").date()
        end_d = datetime.strptime(END_DATE, "%Y-%m-%d").date()

        last_completed_day = self.get_last_completed_day()

        if last_completed_day:
            resume_day = datetime.strptime(last_completed_day, "%Y-%m-%d").date() + timedelta(days=1)
            d = max(start_d, resume_day)

            print(
                f"🔁 Resume enabled | last_completed_day={last_completed_day} | starting_from={d}",
                flush=True,
            )
        else:
            d = start_d
            print("🆕 No checkpoint found. Starting from beginning.", flush=True)

        while d <= end_d:
            if d.weekday() < 5:
                self.daily_reject_start = dict(self.reject_stats)

                self.run_day(d)

                self.save_day_checkpoint(d)
                self.clear_daily_memory_after_checkpoint()

            else:
                print(f"⏸️ Skipping weekend: {d}", flush=True)

            d += timedelta(days=1)

        print("📦 Loading all checkpoint results for final report...", flush=True)
        self.load_checkpoint_results()

        self.save_results()

        runtime["status"] = "finished"
        runtime["finished_at"] = now_ksa_str()

        msg = self.build_telegram_summary()
        print(msg, flush=True)
        send_telegram_message(msg)

        self.send_report_files_to_telegram()

        send_telegram_message(
            "✅ تم إرسال ملفات CSV الخاصة بباك تيست Early Explosion.\n"
            "سيتم الآن إيقاف السكربت."
        )

        print("✅ Backtest completed. Reports sent. Process will exit now.", flush=True)
        time.sleep(5)
        os._exit(0)
        
    def alerts_df(self) -> pd.DataFrame:
        df = pd.DataFrame(self.alerts)
        if df.empty:
            return df
        for col in df.columns:
            if "time" in col:
                df[col] = df[col].astype(str)
        return df
        
    def save_results(self) -> None:
        alerts_df = self.alerts_df()

        # 1) Full alerts file
        alerts_path = os.path.join(OUTPUT_DIR, "backtest_alerts.csv")
        alerts_df.to_csv(alerts_path, index=False)

        # 2) Alert timeline file
        timeline_cols = [
            "alert_time_ny",
            "alert_time_ksa",
            "symbol",
            "price",
            "score",
            "rvol",
            "change_pct",
            "target1",
            "target2",
            "target3",
            "stop_loss",
            "t1_hit",
            "t1_time_ny",
            "t1_minutes",
            "t2_hit",
            "t2_time_ny",
            "t2_minutes",
            "t3_hit",
            "t3_time_ny",
            "t3_minutes",
            "stop_hit",
            "stop_time_ny",
            "stop_minutes",
            "max_gain_pct",
            "close_gain_pct",
            "exit_status",
        ]

        if not alerts_df.empty:
            existing_cols = [c for c in timeline_cols if c in alerts_df.columns]
            timeline_df = alerts_df[existing_cols].copy()
            timeline_df = timeline_df.sort_values("alert_time_ny")
        else:
            timeline_df = pd.DataFrame(columns=timeline_cols)

        timeline_df.to_csv(
            os.path.join(OUTPUT_DIR, "backtest_alert_timeline.csv"),
            index=False
        )

        # 3) Reject stats
        reject_df = pd.DataFrame([
            {"reason": reason, "count": count}
            for reason, count in sorted(
                self.reject_stats.items(),
                key=lambda x: x[1],
                reverse=True
            )
        ])
        reject_df.to_csv(
            os.path.join(OUTPUT_DIR, "backtest_reject_stats.csv"),
            index=False
        )

        # 4) Missed opportunities
        missed_df = pd.DataFrame(self.missed)
        if not missed_df.empty:
            for col in ["time_ny", "time_ksa"]:
                if col in missed_df.columns:
                    missed_df[col] = missed_df[col].astype(str)

            missed_df = missed_df.sort_values(
                "future_max_gain_pct",
                ascending=False
            ).head(50)

        missed_df.to_csv(
            os.path.join(OUTPUT_DIR, "backtest_missed_opportunities.csv"),
            index=False
        )

        # 5) Summary
        summary_df = pd.DataFrame(self.summary_rows(alerts_df))
        summary_df.to_csv(
            os.path.join(OUTPUT_DIR, "backtest_summary.csv"),
            index=False
        )

        # 6) Breakdowns + charts
        self.breakdown_csvs(alerts_df)
        self.generate_charts(alerts_df, reject_df)

        print(f"✅ Reports saved in {OUTPUT_DIR}/", flush=True)
        
    def send_report_files_to_telegram(self) -> None:
        files = [
            "backtest_summary.csv",
            "backtest_alerts.csv",
            "backtest_reject_stats.csv",
            "backtest_missed_opportunities.csv",
            "backtest_alert_timeline.csv",
            "breakdown_by_score_bucket.csv",
            "breakdown_by_time_window.csv",
            "breakdown_by_float_tier.csv",
            "breakdown_by_price_bucket.csv",
        ]

        for filename in files:
            path = os.path.join(OUTPUT_DIR, filename)
            send_telegram_document(path, f"📎 {filename}")
            time.sleep(1)

    def summary_rows(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        if df.empty:
            return [
                {"metric": "period", "value": f"{START_DATE} to {END_DATE}"},
                {"metric": "total_alerts", "value": 0},
                {"metric": "symbols_after_asset_filter", "value": runtime["symbols_after_asset_filter"]},
            ]
        best = df.sort_values("max_gain_pct", ascending=False).iloc[0]
        return [
            {"metric": "period", "value": f"{START_DATE} to {END_DATE}"},
            {"metric": "symbols_after_asset_filter", "value": runtime["symbols_after_asset_filter"]},
            {"metric": "daily_symbols_loaded", "value": runtime["daily_symbols_loaded"]},
            {"metric": "minute_symbols_loaded", "value": runtime["minute_symbols_loaded"]},
            {"metric": "float_records", "value": runtime["float_records"]},
            {"metric": "total_alerts", "value": len(df)},
            {"metric": "unique_symbols", "value": df["symbol"].nunique()},
            {"metric": "t1_hits", "value": int(df["t1_hit"].sum())},
            {"metric": "t1_rate_pct", "value": round(df["t1_hit"].mean() * 100, 2)},
            {"metric": "t2_hits", "value": int(df["t2_hit"].sum())},
            {"metric": "t2_rate_pct", "value": round(df["t2_hit"].mean() * 100, 2)},
            {"metric": "t3_hits", "value": int(df["t3_hit"].sum())},
            {"metric": "t3_rate_pct", "value": round(df["t3_hit"].mean() * 100, 2)},
            {"metric": "stop_hits", "value": int(df["stop_hit"].sum())},
            {"metric": "stop_rate_pct", "value": round(df["stop_hit"].mean() * 100, 2)},
            {"metric": "avg_score", "value": round(df["score"].mean(), 2)},
            {"metric": "avg_max_gain_pct", "value": round(df["max_gain_pct"].mean(), 2)},
            {"metric": "best_trade_symbol", "value": best["symbol"]},
            {"metric": "best_trade_gain_pct", "value": best["max_gain_pct"]},
        ]

    def breakdown_csvs(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        for col in ["score_bucket", "time_window", "float_tier", "price_bucket"]:
            if col not in df.columns:
                continue
            rows = []
            for val, g in df.groupby(col):
                rows.append({
                    col: val,
                    "alerts": len(g),
                    "t1_rate_pct": round(g["t1_hit"].mean() * 100, 2),
                    "t2_rate_pct": round(g["t2_hit"].mean() * 100, 2),
                    "t3_rate_pct": round(g["t3_hit"].mean() * 100, 2),
                    "stop_rate_pct": round(g["stop_hit"].mean() * 100, 2),
                    "avg_max_gain_pct": round(g["max_gain_pct"].mean(), 2),
                })
            pd.DataFrame(rows).to_csv(os.path.join(OUTPUT_DIR, f"breakdown_by_{col}.csv"), index=False)

    def generate_charts(self, alerts_df: pd.DataFrame, reject_df: pd.DataFrame) -> None:
        if alerts_df.empty:
            return
        chart_df = alerts_df.copy()
        chart_df["alert_date"] = pd.to_datetime(chart_df["alert_time_ny"], errors="coerce").dt.date
        self.save_bar(chart_df.groupby("alert_date").size(), "alerts_by_day.png", "Alerts by Day")
        self.save_hist(chart_df["score"], "score_distribution.png", "Score Distribution")
        self.save_hist(chart_df["max_gain_pct"], "max_gain_distribution.png", "Max Gain Distribution")
        for col, fname, title in [
            ("score_bucket", "by_score_bucket_t1.png", "T1 Rate by Score Bucket"),
            ("time_window", "by_time_window_t1.png", "T1 Rate by Time Window"),
            ("float_tier", "by_float_tier_t1.png", "T1 Rate by Float Tier"),
            ("price_bucket", "by_price_bucket_t1.png", "T1 Rate by Price Bucket"),
        ]:
            if col in chart_df.columns:
                s = chart_df.groupby(col)["t1_hit"].mean() * 100
                self.save_bar(s, fname, title)
        if not reject_df.empty:
            self.save_bar(reject_df.head(12).set_index("reason")["count"], "reject_reasons.png", "Top Reject Reasons")

    def save_bar(self, series: pd.Series, filename: str, title: str) -> None:
        plt.figure(figsize=(10, 5))
        series.plot(kind="bar")
        plt.title(title)
        plt.tight_layout()
        plt.savefig(os.path.join(OUTPUT_DIR, filename))
        plt.close()

    def save_hist(self, series: pd.Series, filename: str, title: str) -> None:
        plt.figure(figsize=(10, 5))
        series.dropna().plot(kind="hist", bins=20)
        plt.title(title)
        plt.tight_layout()
        plt.savefig(os.path.join(OUTPUT_DIR, filename))
        plt.close()

    def build_telegram_summary(self) -> str:
        df = pd.DataFrame(self.alerts)
        if df.empty:
            return (
                "📊 *Early Explosion Backtest v1.0 انتهى*\n\n"
                f"🗓️ الفترة: `{START_DATE}` → `{END_DATE}`\n"
                f"📌 الأسهم بعد الفلترة الأولية: `{runtime['symbols_after_asset_filter']}`\n"
                "🚫 لم يتم تسجيل أي تنبيه مطابق للشروط.\n"
                "✅ انتهى الفحص وتوقف السكربت."
            )
        total = len(df)
        best = df.sort_values("max_gain_pct", ascending=False).iloc[0]
        return (
            "📊 *Early Explosion Backtest v1.0 انتهى*\n\n"
            f"🗓️ الفترة: `{START_DATE}` → `{END_DATE}`\n"
            f"📌 الأسهم بعد الفلترة الأولية: `{runtime['symbols_after_asset_filter']}`\n"
            f"🧬 Float records: `{runtime['float_records']}`\n"
            f"🚀 عدد التنبيهات: `{total}`\n"
            f"🎫 الأسهم الفريدة: `{df['symbol'].nunique()}`\n\n"
            f"✅ T1: `{df['t1_hit'].mean() * 100:.1f}%`\n"
            f"🔥 T2: `{df['t2_hit'].mean() * 100:.1f}%`\n"
            f"🚀 T3: `{df['t3_hit'].mean() * 100:.1f}%`\n"
            f"🛑 Stop: `{df['stop_hit'].mean() * 100:.1f}%`\n\n"
            f"📈 متوسط أعلى ربح: `{df['max_gain_pct'].mean():.2f}%`\n"
            f"🏆 أفضل صفقة: `{best['symbol']}` | `{best['max_gain_pct']}%`\n"
            f"📁 التقارير داخل `{OUTPUT_DIR}`\n"
            "✅ انتهى الفحص بالكامل وتوقف السكربت."
        )

# =========================================================
# MAIN
# =========================================================

def start_backtest_thread() -> None:
    def runner() -> None:
        try:
            bt = EarlyExplosionBacktester()
            bt.run()
        except Exception as exc:
            runtime["status"] = "error"
            runtime["error"] = str(exc)
            runtime["finished_at"] = now_ksa_str()
            print(f"❌ Fatal Backtest Error: {exc}", flush=True)
            send_telegram_message(f"❌ *Early Explosion Backtest Error*\n`{exc}`")
            time.sleep(10)
            os._exit(1)
    threading.Thread(target=runner, daemon=True).start()


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    start_backtest_thread()
    while True:
        time.sleep(60)
