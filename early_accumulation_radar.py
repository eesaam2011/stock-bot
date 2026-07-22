import os
import json
import time
import math
import datetime as dt
from typing import Dict, List, Optional, Tuple, Any
import requests
import pandas as pd
import numpy as np
import pytz
import alpaca_trade_api as tradeapi
from flask import Flask, jsonify
import threading

# ==============================================================================
# 🧠 Early Accumulation Radar
# File: early_accumulation_radar.py
# ==============================================================================

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID") or os.getenv("APCA_API_KEY")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY") or os.getenv("APCA_SECRET_KEY")
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_BOT3_CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

REDIS_STATE_KEY = "accumulation:state"
REDIS_WATCHLIST_KEY = "accumulation:watchlist"

STATE_FILE = "accumulation_state.json"
WATCHLIST_FILE = "accumulation_watchlist.json"
FLOAT_CACHE_FILENAME = "float_cache.json"
BOT_NAME = "🧠 رادار التجميع المبكر"

SAUDI_TZ = pytz.timezone("Asia/Riyadh")
NY_TZ = pytz.timezone("America/New_York")

UNIVERSE_REBUILD_INTERVAL = 30 * 60      # إعادة بناء الـ Universe كل 30 دقيقة
ACCUMULATION_SCAN_INTERVAL = 2 * 60      # فحص السوق كل دقيقتين
WATCHLIST_MONITOR_INTERVAL = 10          # مراقبة جميع أسهم الـ Watchlist كل 30 ثانية
WATCHLIST_TTL_HOURS = 24                 # الاحتفاظ بالسهم في الـ Watchlist لمدة 24 ساعة

PRICE_MIN = float(os.getenv("PRICE_MIN", "0.5"))
PRICE_MAX = float(os.getenv("PRICE_MAX", "20.0"))
MIN_DOLLAR_VOLUME = float(os.getenv("MIN_DOLLAR_VOLUME", "500000"))
MIN_RVOL_EARLY = float(os.getenv("MIN_RVOL_EARLY", "1.5"))
MAX_DAY_GAIN_FOR_EARLY = float(os.getenv("MAX_DAY_GAIN_FOR_EARLY", "15.0"))
MAX_DAY_GAIN_HARD_LIMIT = float(
    os.getenv("MAX_DAY_GAIN_HARD_LIMIT", "25.0")
)
ENTRY_RVOL_MIN = float(os.getenv("ENTRY_RVOL_MIN", "2.5"))
ENTRY_MIN_BREAKOUT_PCT = float(os.getenv("ENTRY_MIN_BREAKOUT_PCT", "0.15"))

ENTRY_MAX_CLOSED_BAR_AGE_SECONDS = int(
    os.getenv("ENTRY_MAX_CLOSED_BAR_AGE_SECONDS", "120")
)

ENTRY_BREAKOUT_VOLUME_MULTIPLIER = float(
    os.getenv("ENTRY_BREAKOUT_VOLUME_MULTIPLIER", "1.25")
)

ENTRY_BREAKOUT_VOLUME_LOOKBACK = int(
    os.getenv("ENTRY_BREAKOUT_VOLUME_LOOKBACK", "10")
)
ENTRY_BREAKOUT_VOLUME_LOOKBACK = int(
    os.getenv("ENTRY_BREAKOUT_VOLUME_LOOKBACK", "10")
)

TRADE_PLAN_ATR_PERIOD = int(
    os.getenv("TRADE_PLAN_ATR_PERIOD", "14")
)

TRADE_PLAN_SWING_LOOKBACK = int(
    os.getenv("TRADE_PLAN_SWING_LOOKBACK", "10")
)

STRONG_MAX_STOP_PCT = float(
    os.getenv("STRONG_MAX_STOP_PCT", "5.0")
)

EXPLOSION_MAX_STOP_PCT = float(
    os.getenv("EXPLOSION_MAX_STOP_PCT", "7.0")
)

EXPLOSION_CONFIDENCE_MIN = int(
    os.getenv("EXPLOSION_CONFIDENCE_MIN", "94")
)
FAIL_RVOL_MIN = float(os.getenv("FAIL_RVOL_MIN", "1.2"))
EARLY_ALERT_MIN_SCORE = int(os.getenv("EARLY_ALERT_MIN_SCORE", "80"))

BARS_LIMIT = 160
OBV_LOOKBACK = 120
RESISTANCE_BODY_LOOKBACK = 100
RECENT_LOW_LOOKBACK = 20
MAX_SYMBOLS_PER_BATCH = 500
BARS_SYMBOLS_PER_BATCH = 60
BATCH_SLEEP_SEC = 0.8
WATCHLIST_GRACE_MINUTES = 10
MIN_ENTRY_CONFIDENCE = 70

runtime_stats = {
    "started_at": None,
    "startup_message_sent": False,
    "last_universe_build": None,
    "last_accumulation_scan": None,
    "last_watchlist_monitor": None,
    "universe_count": 0,
    "watchlist_count": 0,
    "early_alerts_sent": 0,
    "entry_alerts_sent": 0,
    "failure_alerts_sent": 0,
}
app = Flask(__name__)

BAD_SUFFIXES = ("W", "U", "R", "P", "Q", "Z")

BAD_NAME_KEYWORDS = [
    "ETF", "ETN", "FUND", "TRUST", "INDEX",
    "WARRANT", "WARRANTS", "UNIT", "UNITS", "RIGHT", "RIGHTS",
    "PREFERRED", "PREF", "NOTE", "NOTES", "BOND",
    "ACQUISITION", "BLANK CHECK", "SPAC",
    "ACQUISITION CORP", "ACQUISITION CORPORATION",
]
SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC", "PNC", "COF", "DFS",
    "MET", "PRU", "ALL", "AIG", "AFL", "TRV", "HIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR",
    "BUD", "TAP", "STZ", "DEO", "PM", "MO", "BTI",
    "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL", "AMC", "CNK", "IMAX", "HITI",
}

def now_saudi() -> dt.datetime:
    return dt.datetime.now(SAUDI_TZ)

def now_ny() -> dt.datetime:
    return dt.datetime.now(NY_TZ)

def iso_now() -> str:
    return now_saudi().isoformat()

def parse_iso(dt_str: Optional[str]) -> Optional[dt.datetime]:
    if not dt_str:
        return None
    try:
        return dt.datetime.fromisoformat(dt_str)
    except Exception:
        return None

def is_weekday_ny() -> bool:
    return dt.datetime.now(NY_TZ).weekday() < 5
    
def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default
        
def make_json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): make_json_safe(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]

    if isinstance(obj, tuple):
        return [make_json_safe(v) for v in obj]

    if isinstance(obj, np.bool_):
        return bool(obj)

    if isinstance(obj, np.integer):
        return int(obj)

    if isinstance(obj, np.floating):
        return float(obj)

    if pd.isna(obj):
        return None

    return obj
    
def fmt_price(price: float) -> str:
    price = safe_float(price)
    if price < 1:
        return f"{price:.4f}"
    if price < 10:
        return f"{price:.3f}"
    return f"{price:.2f}"

def fmt_millions(value: Optional[float]) -> str:
    if value is None:
        return "غير معروف"
    value = safe_float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f} مليون"
    if value >= 1_000:
        return f"{value / 1_000:.1f} ألف"
    return f"{value:.0f}"

def fmt_dollar(value: float) -> str:
    value = safe_float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M$"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K$"
    return f"{value:.0f}$"

def read_json_file(path: str, default: Any) -> Any:
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[JSON] Failed reading {path}: {e}")
        return default

def write_json_file(path: str, data: Any) -> None:
    try:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(make_json_safe(data), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"[JSON] Failed writing {path}: {e}")

def redis_get_json(key: str, default: Any) -> Any:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return default

    try:
        url = f"{UPSTASH_REDIS_REST_URL}/get/{key}"
        headers = {
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"
        }

        r = requests.get(
            url,
            headers=headers,
            timeout=15
        )

        if r.status_code != 200:
            print(
                f"[REDIS] GET failed {key}: "
                f"{r.status_code} {r.text[:200]}",
                flush=True
            )
            return default

        value = r.json().get("result")

        if value is None or value == "":
            return default

        parsed = value

        for _ in range(3):
            if not isinstance(parsed, str):
                break

            try:
                parsed = json.loads(parsed)
            except Exception:
                break

        return parsed

    except Exception as e:
        print(
            f"[REDIS] GET exception {key}: {e}",
            flush=True
        )
        return default

def redis_set_json(key: str, data: Any) -> bool:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return False

    try:
        url = f"{UPSTASH_REDIS_REST_URL}/set/{key}"
        headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
        payload = json.dumps(make_json_safe(data), ensure_ascii=False)

        r = requests.post(url, headers=headers, json=payload, timeout=15)

        if r.status_code != 200:
            print(f"[REDIS] SET failed {key}: {r.status_code} {r.text[:200]}", flush=True)
            return False

        return True

    except Exception as e:
        print(f"[REDIS] SET exception {key}: {e}", flush=True)
        return False
        
def load_json_from_gist(filename: str, default: Any) -> Any:
    if not GITHUB_TOKEN or not GIST_ID:
        return read_json_file(filename, default)

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        r = requests.get(url, headers=headers, timeout=20)

        if r.status_code != 200:
            print(f"[GIST] Load failed {filename}: {r.status_code}", flush=True)
            return read_json_file(filename, default)

        files = r.json().get("files", {})
        if filename not in files:
            return default

        content = files[filename].get("content")
        if not content:
            return default

        return json.loads(content)

    except Exception as e:
        print(f"[GIST] Load exception {filename}: {e}", flush=True)
        return read_json_file(filename, default)


def save_json_to_gist(filename: str, data: Any) -> None:
    write_json_file(filename, data)

    if not GITHUB_TOKEN or not GIST_ID:
        return

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        payload = {
            "files": {
                filename: {
                    "content": json.dumps(make_json_safe(data), ensure_ascii=False, indent=2)
                }
            }
        }

        r = requests.patch(url, headers=headers, json=payload, timeout=20)

        if r.status_code not in (200, 201):
            print(f"[GIST] Save failed {filename}: {r.status_code} {r.text[:200]}", flush=True)

    except Exception as e:
        print(f"[GIST] Save exception {filename}: {e}", flush=True)
        
def default_state() -> Dict[str, Any]:
    return {
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "last_universe_build": None,
        "last_accumulation_scan": None,
        "last_watchlist_monitor": None,
        "sent_early_alerts": {},
        "sent_entry_alerts": {},
        "sent_failure_alerts": {},
    }

def load_state() -> Dict[str, Any]:
    loaded_state = redis_get_json(
        REDIS_STATE_KEY,
        None
    )

    if loaded_state is None:
        loaded_state = read_json_file(
            STATE_FILE,
            default_state()
        )

    if not isinstance(loaded_state, dict):
        print(
            f"[STATE] Invalid Redis type: "
            f"{type(loaded_state).__name__}. "
            f"Using default state.",
            flush=True
        )
        loaded_state = default_state()

    defaults = default_state()

    for key, value in defaults.items():
        loaded_state.setdefault(key, value)

    return loaded_state

def save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = iso_now()
    if not redis_set_json(REDIS_STATE_KEY, state):
        write_json_file(STATE_FILE, state)
        
def load_watchlist() -> Dict[str, Any]:
    loaded_watchlist = redis_get_json(
        REDIS_WATCHLIST_KEY,
        {}
    )

    if not isinstance(loaded_watchlist, dict):
        print(
            f"[WATCHLIST] Invalid Redis type: "
            f"{type(loaded_watchlist).__name__}. "
            f"Starting with empty watchlist.",
            flush=True
        )
        return {}

    return loaded_watchlist
    
def save_watchlist(watchlist: Dict[str, Any]) -> None:
    if not redis_set_json(REDIS_WATCHLIST_KEY, watchlist):
        write_json_file(WATCHLIST_FILE, watchlist)
        
def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        print(message)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"[TELEGRAM] Failed {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[TELEGRAM] Exception: {e}")
        return False

def load_float_cache_from_gist() -> Dict[str, Any]:
    if not GITHUB_TOKEN or not GIST_ID:
        print("[FLOAT] Missing GITHUB_TOKEN or GIST_ID")
        return {}
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            print(f"[FLOAT] Gist fetch failed {r.status_code}: {r.text[:300]}")
            return {}
        files = r.json().get("files", {})
        if FLOAT_CACHE_FILENAME not in files:
            print(f"[FLOAT] {FLOAT_CACHE_FILENAME} not found in Gist")
            return {}
        content = files[FLOAT_CACHE_FILENAME].get("content")
        if not content:
            raw_url = files[FLOAT_CACHE_FILENAME].get("raw_url")
            if raw_url:
                rr = requests.get(raw_url, timeout=20)
                if rr.status_code == 200:
                    content = rr.text
        if not content:
            return {}
        parsed = json.loads(content)
        print(f"[FLOAT] Loaded float cache: {len(parsed)} symbols")
        return parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        print(f"[FLOAT] Failed loading from Gist: {e}")
        return {}

def extract_float_shares(symbol: str, float_cache: Dict[str, Any]) -> Optional[float]:
    item = float_cache.get(symbol)
    if item is None:
        return None
    if isinstance(item, (int, float)):
        val = safe_float(item, 0)
        return val if val > 0 else None
    if isinstance(item, dict):
        for key in ("float_shares", "floatingShare", "float", "shares_float", "free_float"):
            if key in item:
                val = safe_float(item.get(key), 0)
                if val > 0:
                    return val
    return None

def make_alpaca_api():
    if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
        raise RuntimeError("Missing Alpaca API keys")
    return tradeapi.REST(APCA_API_KEY_ID, APCA_API_SECRET_KEY, APCA_API_BASE_URL, api_version="v2")

api = make_alpaca_api()

def is_clean_symbol(symbol: str) -> bool:
    symbol = (symbol or "").upper().strip()
    if len(symbol) > 5 or not symbol.isalpha():
        return False
    if any(x in symbol for x in [".", "-", "/", "^"]):
        return False
    if symbol.endswith(BAD_SUFFIXES):
        return False
    if symbol in SYMBOL_BLACKLIST:
        return False
    return True

def has_bad_name(asset: Any) -> bool:
    name = (getattr(asset, "name", "") or "").upper()
    symbol = (getattr(asset, "symbol", "") or "").upper()
    text = f"{symbol} {name}"
    return any(keyword in text for keyword in BAD_NAME_KEYWORDS)

def build_universe() -> List[str]:
    print("[UNIVERSE] Building universe from Alpaca assets...", flush=True)
    try:
        assets = api.list_assets(status="active")
    except Exception as e:
        print(f"[UNIVERSE] Failed list_assets: {e}", flush=True)
        return []
    symbols = []
    for asset in assets:
        try:
            symbol = (asset.symbol or "").upper().strip()
            if not getattr(asset, "tradable", False):
                continue
            if not is_clean_symbol(symbol):
                continue
            if has_bad_name(asset):
                continue
            symbols.append(symbol)
        except Exception:
            continue
    symbols = sorted(set(symbols))
    print(f"[UNIVERSE] Clean universe: {len(symbols)} symbols", flush=True)
    return symbols

def get_snapshots(symbols: List[str]) -> Dict[str, Any]:
    snapshots = {}
    for i in range(0, len(symbols), MAX_SYMBOLS_PER_BATCH):
        batch = symbols[i:i + MAX_SYMBOLS_PER_BATCH]
        try:
            data = api.get_snapshots(batch)
            if data:
                snapshots.update(data)
        except Exception as e:
            print(f"[SNAPSHOTS] Batch failed: {e}", flush=True)
        time.sleep(BATCH_SLEEP_SEC)
    return snapshots

def get_latest_price_from_snapshot(snapshot: Any) -> Optional[float]:
    for attr_group in ("latest_trade", "minute_bar", "daily_bar", "prev_daily_bar"):
        obj = getattr(snapshot, attr_group, None)
        if obj is None:
            continue
        for attr in ("p", "c", "close"):
            val = getattr(obj, attr, None)
            if val is not None:
                price = safe_float(val, 0)
                if price > 0:
                    return price
    return None

def get_daily_volume_from_snapshot(snapshot: Any) -> float:
    daily_bar = getattr(snapshot, "daily_bar", None)
    if daily_bar is None:
        return 0.0
    for attr in ("v", "volume"):
        val = getattr(daily_bar, attr, None)
        if val is not None:
            return safe_float(val, 0)
    return 0.0

def get_day_change_pct(snapshot: Any, price: float) -> float:
    prev_bar = getattr(snapshot, "prev_daily_bar", None)
    if prev_bar is None:
        return 0.0
    prev_close = None
    for attr in ("c", "close"):
        val = getattr(prev_bar, attr, None)
        if val is not None:
            prev_close = safe_float(val, 0)
            break
    if not prev_close or prev_close <= 0:
        return 0.0
    return ((price - prev_close) / prev_close) * 100.0

def get_1m_bars_batch(
    symbols: List[str],
    limit: int = BARS_LIMIT
) -> Dict[str, pd.DataFrame]:
    results: Dict[str, pd.DataFrame] = {}

    if not symbols:
        return results

    for i in range(0, len(symbols), BARS_SYMBOLS_PER_BATCH):
        batch = symbols[i:i + BARS_SYMBOLS_PER_BATCH]

        try:
            request_limit = min(10000, limit * len(batch))

            bars_df = api.get_bars(
                batch,
                tradeapi.TimeFrame.Minute,
                limit=request_limit,
                adjustment="split"
            ).df

            if bars_df is None or bars_df.empty:
                print(
                    f"[BARS] Empty batch | Symbols={len(batch)}",
                    flush=True
                )
                time.sleep(BATCH_SLEEP_SEC)
                continue

            if isinstance(bars_df.index, pd.MultiIndex):
                if "symbol" not in bars_df.index.names:
                    print(
                        f"[BARS] MultiIndex missing symbol: "
                        f"{bars_df.index.names}",
                        flush=True
                    )
                    time.sleep(BATCH_SLEEP_SEC)
                    continue

                available_symbols = (
                    bars_df.index
                    .get_level_values("symbol")
                    .unique()
                )

                for sym in available_symbols:
                    df = (
                        bars_df
                        .xs(sym, level="symbol")
                        .reset_index()
                    )

                    if "timestamp" not in df.columns and "time" in df.columns:
                        df.rename(
                            columns={"time": "timestamp"},
                            inplace=True
                        )

                    needed = {
                        "timestamp",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                    }

                    if not needed.issubset(df.columns):
                        continue

                    results[str(sym)] = (
                        df.sort_values("timestamp")
                        .tail(limit)
                        .reset_index(drop=True)[
                            [
                                "timestamp",
                                "open",
                                "high",
                                "low",
                                "close",
                                "volume",
                            ]
                        ]
                        .copy()
                    )

            elif "symbol" in bars_df.columns:
                work = bars_df.reset_index()

                if "timestamp" not in work.columns and "time" in work.columns:
                    work.rename(
                        columns={"time": "timestamp"},
                        inplace=True
                    )

                needed = {
                    "symbol",
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                }

                if needed.issubset(work.columns):
                    for sym, df in work.groupby("symbol"):
                        results[str(sym)] = (
                            df.sort_values("timestamp")
                            .tail(limit)
                            .reset_index(drop=True)[
                                [
                                    "timestamp",
                                    "open",
                                    "high",
                                    "low",
                                    "close",
                                    "volume",
                                ]
                            ]
                            .copy()
                        )

            elif len(batch) == 1:
                sym = batch[0]
                df = bars_df.reset_index()

                if "timestamp" not in df.columns and "time" in df.columns:
                    df.rename(
                        columns={"time": "timestamp"},
                        inplace=True
                    )

                needed = {
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                }

                if needed.issubset(df.columns):
                    results[sym] = (
                        df.sort_values("timestamp")
                        .tail(limit)
                        .reset_index(drop=True)[
                            [
                                "timestamp",
                                "open",
                                "high",
                                "low",
                                "close",
                                "volume",
                            ]
                        ]
                        .copy()
                    )

            print(
                f"[BARS] BatchSymbols={len(batch)} "
                f"Rows={len(bars_df)} "
                f"TotalLoaded={len(results)}",
                flush=True
            )

        except Exception as e:
            print(
                f"[BARS] Batch failed for {len(batch)} symbols: {e}",
                flush=True
            )

        time.sleep(BATCH_SLEEP_SEC)

    return results
                                    
def get_1m_bars_single(symbol: str, limit: int = BARS_LIMIT) -> pd.DataFrame:
    try:
        bars = api.get_bars(
            symbol,
            tradeapi.TimeFrame.Minute,
            limit=limit,
            adjustment="split"
        ).df

        if bars is None or bars.empty:
            return pd.DataFrame()

        df = bars.reset_index()

        if "timestamp" not in df.columns and "time" in df.columns:
            df.rename(columns={"time": "timestamp"}, inplace=True)

        needed = {"timestamp", "open", "high", "low", "close", "volume"}

        if not needed.issubset(set(df.columns)):
            return pd.DataFrame()

        return (
            df.sort_values("timestamp")
            .reset_index(drop=True)[["timestamp", "open", "high", "low", "close", "volume"]]
            .copy()
        )

    except Exception as e:
        print(f"[BARS SINGLE] {symbol} failed: {e}", flush=True)
        return pd.DataFrame()

def prefer_current_ny_session_bars(
    df: pd.DataFrame,
    minimum_current_bars: int = OBV_LOOKBACK
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if "timestamp" not in df.columns:
        return df

    work = df.copy()

    timestamps = pd.to_datetime(
        work["timestamp"],
        utc=True,
        errors="coerce"
    )

    valid_mask = timestamps.notna()

    if not valid_mask.any():
        return df

    work = work.loc[valid_mask].copy()
    timestamps = timestamps.loc[valid_mask]

    ny_dates = timestamps.dt.tz_convert(
        "America/New_York"
    ).dt.date

    current_ny_date = now_ny().date()

    current_session = work.loc[
        ny_dates == current_ny_date
    ].copy()

    if len(current_session) >= minimum_current_bars:
        return (
            current_session
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    return (
        work
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    
def calculate_obv(df: pd.DataFrame) -> pd.Series:
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()

def calculate_rvol(df: pd.DataFrame) -> float:
    if len(df) < 30:
        return 0.0
    volumes = df["volume"].astype(float)
    recent_vol = volumes.tail(5).sum()
    historical = volumes.iloc[:-5]
    avg_5bar_vol = historical.rolling(5).sum().dropna().tail(20).mean()
    if not avg_5bar_vol or avg_5bar_vol <= 0:
        return 0.0
    return safe_float(recent_vol / avg_5bar_vol, 0.0)

def calculate_volume_acceleration(df: pd.DataFrame) -> Dict[str, Any]:
    if df is None or df.empty or len(df) < 13:
        return {
            "volume_acceleration": False,
            "volume_acceleration_score": 0,
            "last_1m_vs_avg": 0.0,
            "last_3m_vs_prev_7m": 0.0,
            "volume_trend_up": False,
            "volume_peak_recent": False,
        }

    volumes = df["volume"].astype(float)

    last_1m = safe_float(volumes.iloc[-1], 0)
    avg_prev_10 = safe_float(volumes.iloc[-11:-1].mean(), 0)
    last_1m_vs_avg = last_1m / avg_prev_10 if avg_prev_10 > 0 else 0.0

    last_3m_avg = safe_float(volumes.iloc[-3:].mean(), 0)
    prev_7m_avg = safe_float(volumes.iloc[-10:-3].mean(), 0)
    last_3m_vs_prev_7m = last_3m_avg / prev_7m_avg if prev_7m_avg > 0 else 0.0

    v1 = safe_float(volumes.iloc[-3], 0)
    v2 = safe_float(volumes.iloc[-2], 0)
    v3 = safe_float(volumes.iloc[-1], 0)
    volume_trend_up = v1 <= v2 <= v3

    lookback = volumes.iloc[-13:]
    peak_idx = lookback.idxmax()
    recent_peak_indexes = set(volumes.tail(3).index)
    volume_peak_recent = peak_idx in recent_peak_indexes

    score = 0

    if last_1m_vs_avg >= 3.0:
        score += 8
    elif last_1m_vs_avg >= 2.0:
        score += 5
    elif last_1m_vs_avg >= 1.5:
        score += 3

    if last_3m_vs_prev_7m >= 2.5:
        score += 8
    elif last_3m_vs_prev_7m >= 1.8:
        score += 5
    elif last_3m_vs_prev_7m >= 1.3:
        score += 3

    if volume_trend_up:
        score += 2

    if volume_peak_recent:
        score += 3

    volume_acceleration = (
        score >= 8
        and last_1m_vs_avg >= 1.5
        and last_3m_vs_prev_7m >= 1.3
        and volume_peak_recent
    )

    return {
        "volume_acceleration": bool(volume_acceleration),
        "volume_acceleration_score": int(score),
        "last_1m_vs_avg": round(last_1m_vs_avg, 2),
        "last_3m_vs_prev_7m": round(last_3m_vs_prev_7m, 2),
        "volume_trend_up": bool(volume_trend_up),
        "volume_peak_recent": bool(volume_peak_recent),
    }


def volume_expansion_ok(df: pd.DataFrame) -> bool:
    return calculate_volume_acceleration(df).get("volume_acceleration", False)
    
def resistance_body_level(
    df: pd.DataFrame,
    lookback: int = RESISTANCE_BODY_LOOKBACK
) -> Optional[float]:
    if len(df) < lookback + 1:
        return None

    base = df.iloc[-(lookback + 1):-1].copy()

    # استخدم أعلى القمم الفعلية بدلاً من أعلى جسم شمعة
    resistance = safe_float(base["high"].max(), 0)

    return resistance if resistance > 0 else None
    
def close_above_resistance(df: pd.DataFrame, resistance: float) -> bool:
    if df.empty or resistance <= 0:
        return False
    return safe_float(df["close"].iloc[-1], 0) > resistance

def get_last_closed_bar(
    df: pd.DataFrame
) -> Optional[Tuple[pd.Series, pd.Timestamp, int]]:
    if df is None or df.empty:
        return None

    if "timestamp" not in df.columns:
        return None

    timestamps = pd.to_datetime(
        df["timestamp"],
        utc=True,
        errors="coerce"
    )

    now_utc = pd.Timestamp.now(tz="UTC")

    closed_mask = (
        timestamps.notna()
        & ((timestamps + pd.Timedelta(minutes=1)) <= now_utc)
    )

    if not closed_mask.any():
        return None

    closed_positions = np.flatnonzero(
        closed_mask.to_numpy()
    )

    if len(closed_positions) == 0:
        return None

    last_position = int(closed_positions[-1])
    last_bar = df.iloc[last_position]
    last_timestamp = timestamps.iloc[last_position]

    return last_bar, last_timestamp, last_position
    
def distance_to_resistance_pct(price: float, resistance: Optional[float]) -> float:
    if not resistance or resistance <= 0:
        return 999.0
    return ((resistance - price) / price) * 100.0

def recent_low_break(df: pd.DataFrame, lookback: int = RECENT_LOW_LOOKBACK) -> bool:
    if len(df) < lookback + 1:
        return False
    last_close = safe_float(df["close"].iloc[-1], 0)
    recent_low = safe_float(df["low"].iloc[-(lookback + 1):-1].min(), 0)
    return recent_low > 0 and last_close < recent_low

def vwap_value(df: pd.DataFrame) -> Optional[float]:
    if df.empty:
        return None
    typical = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3.0
    vol = df["volume"].astype(float)
    total_vol = vol.sum()
    if total_vol <= 0:
        return None
    return safe_float((typical * vol).sum() / total_vol, 0)

def below_vwap(df: pd.DataFrame) -> bool:
    vw = vwap_value(df)
    if not vw:
        return False
    return safe_float(df["close"].iloc[-1], 0) < vw

def obv_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    if len(df) < 30:
        return {"obv_current": 0.0, "obv_ema10": 0.0, "obv_trend": False, "obv_breakout": False, "obv_curve_ok": False}
    work = df.tail(OBV_LOOKBACK).copy()
    obv = calculate_obv(work)
    ema10 = obv.ewm(span=10, adjust=False).mean()
    current = safe_float(obv.iloc[-1], 0)
    ema_now = safe_float(ema10.iloc[-1], 0)
    ema_prev3 = safe_float(ema10.iloc[-4], 0) if len(ema10) >= 4 else ema_now
    return {
        "obv_current": current,
        "obv_ema10": ema_now,
        "obv_trend": current > ema_now,
        "obv_breakout": current >= safe_float(obv.tail(20).max(), 0),
        "obv_curve_ok": current > ema_now and ema_now >= ema_prev3 * 0.995,
    }

def float_score(float_shares: Optional[float]) -> Tuple[int, str]:
    if float_shares is None or float_shares <= 0:
        return 5, "غير معروف"

    if float_shares < 5_000_000:
        return 40, "منخفض جدًا"
    if float_shares < 10_000_000:
        return 35, "منخفض"
    if float_shares < 20_000_000:
        return 28, "قوي"
    if float_shares < 50_000_000:
        return 20, "جيد"
    if float_shares < 100_000_000:
        return 10, "مقبول"

    return 0, "مرتفع"

def obv_score(metrics: Dict[str, Any]) -> int:
    score = 0
    if metrics.get("obv_breakout"):
        score += 15
    if metrics.get("obv_trend"):
        score += 10
    if metrics.get("obv_curve_ok"):
        score += 5
    return score

def rvol_score(rvol: float) -> int:
    if rvol >= 4.0:
        return 10
    if rvol >= 3.0:
        return 8
    if rvol >= 2.0:
        return 5
    if rvol >= 1.5:
        return 3
    return 0

def volume_score(df: pd.DataFrame, dollar_volume: float) -> int:
    accel = calculate_volume_acceleration(df)
    score = min(int(accel.get("volume_acceleration_score", 0)), 10)

    if dollar_volume >= 1_000_000:
        score += 5
    elif dollar_volume >= MIN_DOLLAR_VOLUME:
        score += 3

    return score

def resistance_score(price: float, resistance: Optional[float]) -> int:
    dist = distance_to_resistance_pct(price, resistance)
    if dist < 0:
        return 0
    if dist < 1:
        return 5
    if dist < 2:
        return 3
    if dist < 3:
        return 1
    return 0

# 🌟 تم إضافة متغير early_mode لتمكين تعطيل فلتر صعود اليوم أثناء المراقبة
def analyze_symbol(
    symbol: str,
    snapshot: Any,
    df: pd.DataFrame,
    float_cache: Dict[str, Any],
    early_mode: bool = True
) -> Optional[Dict[str, Any]]:
    price = get_latest_price_from_snapshot(snapshot)

    if not price or price < PRICE_MIN or price > PRICE_MAX:
        return None

    daily_volume = get_daily_volume_from_snapshot(snapshot)
    dollar_volume = price * daily_volume

    if dollar_volume < MIN_DOLLAR_VOLUME:
        return None

    day_change_pct = get_day_change_pct(
        snapshot,
        price
    )

    if (
        early_mode
        and day_change_pct > MAX_DAY_GAIN_HARD_LIMIT
    ):
        return None

    if df.empty or len(df) < OBV_LOOKBACK:
        return None

    rvol = calculate_rvol(df)
    accel = calculate_volume_acceleration(df)
    metrics = obv_metrics(df)

    if early_mode and rvol < MIN_RVOL_EARLY:
        return None

    if (
        early_mode
        and day_change_pct > MAX_DAY_GAIN_FOR_EARLY
        and not (
            rvol >= ENTRY_RVOL_MIN
            and accel.get("volume_acceleration", False)
            and metrics.get("obv_breakout", False)
            and metrics.get("obv_curve_ok", False)
        )
    ):
        return None

    resistance = resistance_body_level(
        df,
        RESISTANCE_BODY_LOOKBACK
    )

    float_shares = extract_float_shares(
        symbol,
        float_cache
    )

    f_score, f_tier = float_score(float_shares)
    o_score = obv_score(metrics)
    rv_score = rvol_score(rvol)
    v_score = volume_score(df, dollar_volume)
    res_score = resistance_score(price, resistance)

    total_score = (
        f_score
        + o_score
        + rv_score
        + v_score
        + res_score
    )

    return {
        "symbol": symbol,
        "price": price,
        "score": total_score,
        "float_score": f_score,
        "float_tier": f_tier,
        "float_shares": float_shares,
        "obv_score": o_score,
        "obv_trend": bool(metrics.get("obv_trend")),
        "obv_breakout": bool(metrics.get("obv_breakout")),
        "obv_curve_ok": bool(metrics.get("obv_curve_ok")),
        "rvol": rvol,
        "rvol_score": rv_score,
        "volume_score": v_score,
        "volume_expansion": bool(
            accel.get("volume_acceleration", False)
        ),
        "volume_acceleration_score": int(
            accel.get("volume_acceleration_score", 0)
        ),
        "last_1m_vs_avg": safe_float(
            accel.get("last_1m_vs_avg"),
            0
        ),
        "last_3m_vs_prev_7m": safe_float(
            accel.get("last_3m_vs_prev_7m"),
            0
        ),
        "volume_trend_up": bool(
            accel.get("volume_trend_up", False)
        ),
        "volume_peak_recent": bool(
            accel.get("volume_peak_recent", False)
        ),
        "resistance": resistance,
        "resistance_score": res_score,
        "distance_to_resistance_pct": distance_to_resistance_pct(
            price,
            resistance
        ),
        "daily_volume": daily_volume,
        "dollar_volume": dollar_volume,
        "day_change_pct": day_change_pct,
        "timestamp": iso_now(),
    }
    
def build_early_alert_message(data: Dict[str, Any]) -> str:
    return (
        f"🧠 <b>تنبيه مبكر - رادار التجميع</b>\n\n"
        f"🏷️ السهم: <b>{data['symbol']}</b>\n"
        f"💰 السعر: <b>{fmt_price(data['price'])}$</b>\n\n"
        f"⭐ درجة التجميع: <b>{int(data['score'])}/100</b>\n\n"
        f"📊 الفلوت: <b>{fmt_millions(data.get('float_shares'))}</b>\n"
        f"🟢 فئة الفلوت: <b>{data.get('float_tier', 'غير معروف')}</b>\n\n"
        f"📈 مؤشر السيولة الموزونة: <b>{'صاعد' if data.get('obv_trend') else 'ضعيف'}</b>\n"
        f"🚀 اختراق السيولة: <b>{'نعم' if data.get('obv_breakout') else 'لا'}</b>\n"
        f"📐 منحنى السيولة: <b>{'سليم' if data.get('obv_curve_ok') else 'ضعيف'}</b>\n\n"
        f"⚡ النشاط الحالي: <b>{safe_float(data.get('rvol')):.2f}x</b>\n"
        f"📦 تسارع الحجم: <b>{'نعم' if data.get('volume_expansion') else 'لا'}</b>\n"
        f"💵 السيولة المتداولة: <b>{fmt_dollar(data.get('dollar_volume', 0))}</b>\n\n"
        f"🎯 المقاومة القريبة: <b>{fmt_price(data.get('resistance')) if data.get('resistance') else 'غير متاحة'}$</b>\n"
        f"📏 المسافة للمقاومة: <b>{safe_float(data.get('distance_to_resistance_pct'), 999):.2f}%</b>\n\n"
        f"📋 القراءة:\nتم رصد تجميع وسيولة متزايدة. هذا تنبيه مبكر وليس إشارة دخول مباشرة.\n\n"
        f"👀 تمت إضافته إلى قائمة المراقبة لمدة 24 ساعة."
    )

            
def calculate_intraday_atr(
    df: pd.DataFrame,
    period: int = TRADE_PLAN_ATR_PERIOD
) -> float:
    if df is None or df.empty or len(df) < period + 1:
        return 0.0

    work = df.copy()

    high = work["high"].astype(float)
    low = work["low"].astype(float)
    close = work["close"].astype(float)
    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1
    ).max(axis=1)

    atr = safe_float(
        true_range.tail(period).mean(),
        0
    )

    return atr if atr > 0 else 0.0

def classify_entry_signal(
    current: Dict[str, Any],
    df: pd.DataFrame
) -> Tuple[str, int]:
    confidence = 0

    rvol = safe_float(
        current.get("rvol"),
        0
    )

    float_shares = safe_float(
        current.get("float_shares"),
        0
    )

    dollar_volume = safe_float(
        current.get("dollar_volume"),
        0
    )

    volume_acceleration_score = safe_float(
        current.get("volume_acceleration_score"),
        0
    )

    resistance = safe_float(
        current.get("resistance"),
        0
    )

    closed_bar_result = get_last_closed_bar(df)

    if closed_bar_result is not None:
        last_bar, _, _ = closed_bar_result
        entry_price = safe_float(
            last_bar.get("close"),
            0
        )
    else:
        entry_price = safe_float(
            current.get("price"),
            0
        )

    breakout_pct = 0.0

    if resistance > 0 and entry_price > resistance:
        breakout_pct = (
            (entry_price - resistance)
            / resistance
        ) * 100.0

    accumulation_score = safe_float(
        current.get("score"),
        0
    )

    if accumulation_score >= 95:
        confidence += 18
    elif accumulation_score >= 90:
        confidence += 15
    elif accumulation_score >= 85:
        confidence += 12
    else:
        confidence += 9

    if rvol >= 8:
        confidence += 22
    elif rvol >= 6:
        confidence += 19
    elif rvol >= 4:
        confidence += 15
    elif rvol >= ENTRY_RVOL_MIN:
        confidence += 10

    if volume_acceleration_score >= 16:
        confidence += 16
    elif volume_acceleration_score >= 12:
        confidence += 13
    elif volume_acceleration_score >= 8:
        confidence += 10

    if current.get("volume_expansion"):
        confidence += 5

    if current.get("obv_breakout"):
        confidence += 8

    if current.get("obv_curve_ok"):
        confidence += 6

    if float_shares > 0:
        if float_shares <= 5_000_000:
            confidence += 13
        elif float_shares <= 10_000_000:
            confidence += 11
        elif float_shares <= 20_000_000:
            confidence += 8
        elif float_shares <= 50_000_000:
            confidence += 5
        elif float_shares <= 100_000_000:
            confidence += 2

    if breakout_pct >= 1.0:
        confidence += 7
    elif breakout_pct >= 0.5:
        confidence += 5
    elif breakout_pct >= ENTRY_MIN_BREAKOUT_PCT:
        confidence += 3

    if dollar_volume >= 10_000_000:
        confidence += 5
    elif dollar_volume >= 3_000_000:
        confidence += 4
    elif dollar_volume >= 1_000_000:
        confidence += 3
    elif dollar_volume >= MIN_DOLLAR_VOLUME:
        confidence += 1

    confidence = max(
        0,
        min(100, int(confidence))
    )

    explosion_conditions = (
        confidence >= EXPLOSION_CONFIDENCE_MIN
        and rvol >= 6.0
        and current.get("volume_expansion")
        and current.get("obv_breakout")
        and current.get("obv_curve_ok")
        and breakout_pct >= 0.50
        and dollar_volume >= 1_000_000
        and (
            float_shares <= 10_000_000
            if float_shares > 0
            else False
        )
    )

    if explosion_conditions:
        return "🚀 انفجار محتمل", confidence

    return "🔥 اختراق قوي", confidence

def calculate_entry_trade_plan(
    current: Dict[str, Any],
    df: pd.DataFrame,
    signal_title: str
) -> Dict[str, Any]:
    resistance = safe_float(
        current.get("resistance"),
        0
    )

    live_price = safe_float(
        current.get("price"),
        0
    )

    closed_bar_result = get_last_closed_bar(df)

    if closed_bar_result is not None:
        last_bar, _, last_position = closed_bar_result

        entry_price = safe_float(
            last_bar.get("close"),
            live_price
        )
    else:
        entry_price = live_price
        last_position = len(df) - 1

    atr = calculate_intraday_atr(
        df,
        TRADE_PLAN_ATR_PERIOD
    )

    swing_start = max(
        0,
        last_position - TRADE_PLAN_SWING_LOOKBACK
    )

    swing_section = df.iloc[
        swing_start:last_position
    ]

    if not swing_section.empty:
        swing_low = safe_float(
            swing_section["low"].astype(float).min(),
            0
        )
    else:
        swing_low = 0.0

    is_explosion = signal_title.startswith("🚀")

    if is_explosion:
        atr_stop = (
            resistance - (atr * 0.75)
            if resistance > 0 and atr > 0
            else 0
        )

        technical_candidates = [
            value
            for value in (swing_low, atr_stop)
            if value > 0 and value < entry_price
        ]

        technical_stop = (
            min(technical_candidates)
            if technical_candidates
            else entry_price * 0.95
        )

        maximum_stop_pct = EXPLOSION_MAX_STOP_PCT

    else:
        atr_stop = (
            resistance - (atr * 0.50)
            if resistance > 0 and atr > 0
            else 0
        )

        technical_candidates = [
            value
            for value in (swing_low, atr_stop)
            if value > 0 and value < entry_price
        ]

        technical_stop = (
            max(technical_candidates)
            if technical_candidates
            else entry_price * 0.97
        )

        maximum_stop_pct = STRONG_MAX_STOP_PCT

    maximum_allowed_stop = entry_price * (
        1 - (maximum_stop_pct / 100.0)
    )

    stop_price = max(
        technical_stop,
        maximum_allowed_stop
    )

    if stop_price >= entry_price:
        stop_price = maximum_allowed_stop

    risk_per_share = entry_price - stop_price

    if risk_per_share <= 0:
        risk_per_share = entry_price * 0.02
        stop_price = entry_price - risk_per_share

    stop_pct = (
        risk_per_share / entry_price
    ) * 100.0

    if is_explosion:
        target_1 = entry_price + (
            risk_per_share * 1.5
        )

        target_2 = entry_price + (
            risk_per_share * 3.0
        )

        target_3 = entry_price + (
            risk_per_share * 5.0
        )

        extended_target = entry_price + (
            risk_per_share * 8.0
        )

    else:
        target_1 = entry_price + risk_per_share
        target_2 = entry_price + (
            risk_per_share * 2.0
        )

        target_3 = entry_price + (
            risk_per_share * 3.0
        )

        extended_target = None

    return {
        "entry_price": entry_price,
        "live_price": live_price,
        "resistance": resistance,
        "atr": atr,
        "swing_low": swing_low,
        "stop_price": stop_price,
        "stop_pct": stop_pct,
        "risk_per_share": risk_per_share,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "extended_target": extended_target,
    }
    
def build_entry_alert_message(
    current: Dict[str, Any],
    watch: Dict[str, Any],
    df: pd.DataFrame
) -> str:
    signal_title, confidence = classify_entry_signal(
        current,
        df
    )

    trade_plan = calculate_entry_trade_plan(
        current,
        df,
        signal_title
    )

    entry_price = safe_float(
        trade_plan.get("entry_price"),
        0
    )

    live_price = safe_float(
        trade_plan.get("live_price"),
        0
    )

    resistance = safe_float(
        trade_plan.get("resistance"),
        0
    )

    stop_price = safe_float(
        trade_plan.get("stop_price"),
        0
    )

    stop_pct = safe_float(
        trade_plan.get("stop_pct"),
        0
    )

    target_1 = safe_float(
        trade_plan.get("target_1"),
        0
    )

    target_2 = safe_float(
        trade_plan.get("target_2"),
        0
    )

    target_3 = safe_float(
        trade_plan.get("target_3"),
        0
    )

    extended_target = trade_plan.get(
        "extended_target"
    )

    message = (
        f"{signal_title}\n\n"
        f"🏷️ السهم: <b>{current['symbol']}</b>\n"
        f"💰 سعر الدخول الفني: "
        f"<b>{fmt_price(entry_price)}$</b>\n"
        f"📍 السعر اللحظي: "
        f"<b>{fmt_price(live_price)}$</b>\n"
        f"🎯 المقاومة المخترقة: "
        f"<b>{fmt_price(resistance)}$</b>\n\n"

        f"💯 نسبة الثقة: "
        f"<b>{confidence}/100</b>\n"
        f"⭐ درجة التجميع: "
        f"<b>{int(current.get('score', 0))}/100</b>\n"
        f"⚡ RVOL: "
        f"<b>{safe_float(current.get('rvol')):.2f}x</b>\n"
        f"📦 تسارع الحجم: "
        f"<b>{'نعم' if current.get('volume_expansion') else 'لا'}</b>\n"
        f"🚀 اختراق السيولة: "
        f"<b>{'نعم' if current.get('obv_breakout') else 'لا'}</b>\n\n"

        f"🛑 وقف الخسارة الفني: "
        f"<b>{fmt_price(stop_price)}$</b>\n"
        f"📉 مسافة الوقف: "
        f"<b>{stop_pct:.2f}%</b>\n\n"

        f"🎯 الهدف الأول: "
        f"<b>{fmt_price(target_1)}$</b>\n"
        f"🎯 الهدف الثاني: "
        f"<b>{fmt_price(target_2)}$</b>\n"
        f"🎯 الهدف الثالث: "
        f"<b>{fmt_price(target_3)}$</b>\n"
    )

    if (
        signal_title.startswith("🚀")
        and extended_target is not None
    ):
        message += (
            f"🌟 الهدف الممتد: "
            f"<b>{fmt_price(extended_target)}$</b>\n\n"
            f"💎 يستمر الهدف الممتد ما دام الزخم "
            f"والسيولة لم يظهرا ضعفًا واضحًا.\n"
        )
    else:
        message += "\n"

    message += (
        f"✅ شروط التأكيد:\n"
        f"• إغلاق شمعة دقيقة مكتملة فوق المقاومة.\n"
        f"• السعر اللحظي ما زال محافظًا فوق المقاومة.\n"
        f"• حجم شمعة الاختراق أعلى من المتوسط المطلوب.\n"
        f"• RVOL أعلى من الحد المطلوب.\n"
        f"• منحنى السيولة الموزونة ما زال سليمًا.\n\n"
        f"📋 النتيجة:\n"
        f"السهم انتقل من مرحلة التجميع "
        f"إلى مرحلة الاختراق المؤكد."
    )

    return message

def build_failure_alert_message(symbol: str, price: float, reason: str) -> str:
    return (
        f"❌ <b>فشل التجميع</b>\n\n"
        f"🏷️ السهم: <b>{symbol}</b>\n"
        f"💰 آخر سعر: <b>{fmt_price(price)}$</b>\n\n"
        f"📋 السبب:\n{reason}\n\n"
        f"⏱️ النتيجة:\nتمت إزالته من قائمة المراقبة."
    )

def already_sent_recently(
    sent_map: Dict[str, Any],
    symbol: str,
    hours: int
) -> bool:
    item = sent_map.get(symbol)

    if not item:
        return False

    alert_time = parse_iso(
        item.get("time") if isinstance(item, dict) else item
    )

    if not alert_time:
        return False

    return now_saudi() - alert_time < dt.timedelta(hours=hours)
    
def add_to_watchlist(watchlist: Dict[str, Any], data: Dict[str, Any]) -> None:
    symbol = data["symbol"]
    watchlist[symbol] = {
        "symbol": symbol,
        "created_at": iso_now(),
        "expires_at": (now_saudi() + dt.timedelta(hours=WATCHLIST_TTL_HOURS)).isoformat(),
        "early_price": data["price"],
        "early_score": data["score"],
        "early_resistance": data.get("resistance"),
        "float_shares": data.get("float_shares"),
        "float_tier": data.get("float_tier"),
        "best_score": data["score"],
        "last_score": data["score"],
        "status": "watching",
        "snapshot": data,
    }

def check_entry_conditions(
    data: Dict[str, Any],
    df: pd.DataFrame
) -> bool:
    resistance = safe_float(
        data.get("resistance"),
        0
    )

    if resistance <= 0:
        return False

    closed_bar_result = get_last_closed_bar(df)

    if closed_bar_result is None:
        return False

    last_bar, last_timestamp, last_position = closed_bar_result

    if last_position < 1:
        return False

    previous_bar = df.iloc[last_position - 1]

    last_close = safe_float(
        last_bar.get("close"),
        0
    )

    previous_close = safe_float(
        previous_bar.get("close"),
        0
    )

    last_volume = safe_float(
        last_bar.get("volume"),
        0
    )

    # اشتراط إغلاق آخر شمعتين مكتملتين فوق المقاومة
    if (
        previous_close <= resistance
        or last_close <= resistance
    ):
        return False

    live_price = safe_float(
        data.get("price"),
        0
    )

    if live_price < (resistance * 0.995):
        return False

    breakout_pct = (
        (last_close - resistance)
        / resistance
    ) * 100.0

    if breakout_pct < ENTRY_MIN_BREAKOUT_PCT:
        return False

    now_utc = pd.Timestamp.now(tz="UTC")

    bar_close_time = (
        last_timestamp
        + pd.Timedelta(minutes=1)
    )

    closed_bar_age_seconds = (
        now_utc - bar_close_time
    ).total_seconds()

    if (
        closed_bar_age_seconds < 0
        or closed_bar_age_seconds
        > ENTRY_MAX_CLOSED_BAR_AGE_SECONDS
    ):
        return False

    previous_start = max(
        0,
        last_position - ENTRY_BREAKOUT_VOLUME_LOOKBACK
    )

    previous_volumes = (
        df.iloc[previous_start:last_position]["volume"]
        .astype(float)
    )

    if previous_volumes.empty:
        return False

    average_previous_volume = safe_float(
        previous_volumes.mean(),
        0
    )

    if average_previous_volume <= 0:
        return False

    breakout_volume_ratio = (
        last_volume / average_previous_volume
    )

    if (
        breakout_volume_ratio
        < ENTRY_BREAKOUT_VOLUME_MULTIPLIER
    ):
        return False

    if safe_float(data.get("rvol"), 0) <= ENTRY_RVOL_MIN:
        return False

    if not data.get("obv_curve_ok"):
        return False

    return True

def failure_reason(data: Optional[Dict[str, Any]], df: pd.DataFrame, watch: Dict[str, Any]) -> Optional[str]:
    created_at = parse_iso(watch.get("created_at"))
    if created_at and now_saudi() - created_at < dt.timedelta(minutes=WATCHLIST_GRACE_MINUTES):
        return None
        
    expires_at = parse_iso(watch.get("expires_at"))
    if expires_at and now_saudi() >= expires_at:
        return "انتهت مهلة 24 ساعة بدون تحقق تنبيه دخول."
    if data is None:
        return None
    price = safe_float(data.get("price"), 0)
    if not data.get("obv_curve_ok"):
        return "كسر اتجاه مؤشر السيولة الموزونة ولم يعد منحنى السيولة صاعدًا."
    if safe_float(data.get("rvol"), 0) < FAIL_RVOL_MIN:
        return "ضعف النشاط؛ هبط RVOL تحت المستوى المطلوب."
    if not data.get("volume_expansion") and safe_float(data.get("dollar_volume"), 0) < MIN_DOLLAR_VOLUME:
        return "اختفى تسارع الحجم وانخفضت السيولة المتداولة."
    resistance = data.get("resistance")
    if resistance and distance_to_resistance_pct(price, resistance) > 5:
        return "فشل الاقتراب من الاختراق؛ ابتعد السعر عن المقاومة بأكثر من 5%."
    if not df.empty and below_vwap(df):
        return "كسر السعر منطقة التجميع وهبط تحت VWAP."
    if not df.empty and recent_low_break(df, RECENT_LOW_LOOKBACK):
        return "كسر السعر قاع آخر 20 شمعة، مما يضعف نموذج التجميع."
    return None

def monitor_watchlist(watchlist: Dict[str, Any], state: Dict[str, Any], float_cache: Dict[str, Any]) -> None:
    if not watchlist:
        return
    symbols = list(watchlist.keys())
    snapshots = get_snapshots(symbols)
    
    batched_bars = get_1m_bars_batch(symbols, BARS_LIMIT)
    
    to_remove = []
    for symbol in symbols:
        watch = watchlist.get(symbol, {})
        snapshot = snapshots.get(symbol)
        df = batched_bars.get(symbol, pd.DataFrame())

        if len(df) < OBV_LOOKBACK:
            df = get_1m_bars_single(
                symbol,
                BARS_LIMIT
            )

        df = prefer_current_ny_session_bars(
            df,
            OBV_LOOKBACK
        )        
        # 🌟 تم تعطيل الفلتر هنا بتمرير early_mode=False (التعديل الخاص بك)
        current_data = analyze_symbol(symbol, snapshot, df, float_cache, early_mode=False) if snapshot is not None else None
        current_price = safe_float(watch.get("early_price"), 0)
        
        if current_data:
            current_price = safe_float(current_data.get("price"), 0)
        elif snapshot is not None:
            current_price = safe_float(get_latest_price_from_snapshot(snapshot), current_price)
            
        if current_data and current_data.get("score", 0) > watch.get("best_score", 0):
            watch["best_score"] = current_data["score"]
            watch["last_score"] = current_data["score"]
            watch["snapshot"] = current_data
            
        if current_data and not df.empty and check_entry_conditions(current_data, df):
            if not state["sent_entry_alerts"].get(symbol):
                if send_telegram_message(
                    build_entry_alert_message(
                        current_data,
                        watch,
                        df
                    )
                ):
                    state["sent_entry_alerts"][symbol] = {
                        "time": iso_now(),
                        "price": current_price,
                    }
                    runtime_stats["entry_alerts_sent"] += 1

            to_remove.append(symbol)
            continue

        reason = failure_reason(
            current_data,
            df,
            watch
        )

        if reason:
            previous_reason = watch.get(
                "pending_failure_reason"
            )

            if previous_reason == reason:
                watch["failure_count"] = (
                    int(watch.get("failure_count", 0))
                    + 1
                )
            else:
                watch["pending_failure_reason"] = reason
                watch["failure_count"] = 1

            # لا نحذف السهم بسبب قراءة واحدة مؤقتة.
            # مع فحص كل 10 ثوانٍ، 6 قراءات تعني نحو دقيقة.
            if watch["failure_count"] < 6:
                watchlist[symbol] = watch
                continue

            if not state["sent_failure_alerts"].get(symbol):
                if send_telegram_message(
                    build_failure_alert_message(
                        symbol,
                        current_price,
                        reason,
                    )
                ):
                    state["sent_failure_alerts"][symbol] = {
                        "time": iso_now(),
                        "price": current_price,
                        "reason": reason,
                    }

                    runtime_stats[
                        "failure_alerts_sent"
                    ] += 1

            to_remove.append(symbol)
            continue

        # تعافت الإشارة، فنصفر عداد الفشل المؤقت.
        watch["failure_count"] = 0
        watch["pending_failure_reason"] = None

        watchlist[symbol] = watch
        
    for symbol in to_remove:
        watchlist.pop(symbol, None)
    runtime_stats["watchlist_count"] = len(watchlist)

def scan_accumulation(universe: List[str], watchlist: Dict[str, Any], state: Dict[str, Any], float_cache_data: Dict[str, Any]) -> None:
    if not universe:
        return
    snapshots = get_snapshots(universe)

    print(f"[SCAN] Universe={len(universe)} Snapshots={len(snapshots)}", flush=True)
    
    candidates_symbols = []
    for symbol, snapshot in snapshots.items():
        if symbol in watchlist:
            continue
        if state["sent_entry_alerts"].get(symbol):
            continue
        if already_sent_recently(state["sent_early_alerts"], symbol, 12):
            continue
            
        price = get_latest_price_from_snapshot(snapshot)
        if not price or price < PRICE_MIN or price > PRICE_MAX:
            continue
        daily_volume = get_daily_volume_from_snapshot(snapshot)
        if price * daily_volume < MIN_DOLLAR_VOLUME:
            continue
        if (
            get_day_change_pct(snapshot, price)
            > MAX_DAY_GAIN_HARD_LIMIT
        ):
            continue
            
        candidates_symbols.append(symbol)

    print(f"[SCAN] Prequalified={len(candidates_symbols)}", flush=True)

    batched_bars = get_1m_bars_batch(candidates_symbols, BARS_LIMIT)

    print(f"[SCAN] BarsLoaded={len(batched_bars)}", flush=True)

    candidates = []
    fallback_used = 0
    
    for symbol in candidates_symbols:
        snapshot = snapshots.get(symbol)
        df = batched_bars.get(symbol, pd.DataFrame())

        if len(df) < OBV_LOOKBACK:
            fallback_used += 1
            df = get_1m_bars_single(
                symbol,
                BARS_LIMIT
            )

        df = prefer_current_ny_session_bars(
            df,
            OBV_LOOKBACK
        )

        data = analyze_symbol(
            symbol,
            snapshot,
            df,
            float_cache_data,
            early_mode=True
        )
        
        if (
            data
            and data["score"] >= EARLY_ALERT_MIN_SCORE
            and data.get("obv_breakout")
            and data.get("volume_expansion")
            and safe_float(data.get("distance_to_resistance_pct"), 999) <= 5
        ):
            candidates.append(data)

    print(f"[SCAN] FallbackUsed={fallback_used}", flush=True)

    print(f"[SCAN] FinalCandidates={len(candidates)}", flush=True)

    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    for data in candidates:
        symbol = data["symbol"]

        print(
            f"[ALERT CHECK] {symbol} "
            f"Score={data.get('score')} "
            f"RVOL={safe_float(data.get('rvol')):.2f} "
            f"FloatTier={data.get('float_tier')}",
            flush=True
        )

        if send_telegram_message(build_early_alert_message(data)):
            state["sent_early_alerts"][symbol] = {"time": iso_now(), "price": data["price"], "score": data["score"]}
            add_to_watchlist(watchlist, data)
            runtime_stats["early_alerts_sent"] += 1
            
    runtime_stats["watchlist_count"] = len(watchlist)

@app.route("/")
def home():
    return jsonify({
        "bot": BOT_NAME,
        "status": "running",
        "started_at": runtime_stats["started_at"],
        "universe_count": runtime_stats["universe_count"],
        "watchlist_count": runtime_stats["watchlist_count"],
        "early_alerts_sent": runtime_stats["early_alerts_sent"],
        "entry_alerts_sent": runtime_stats["entry_alerts_sent"],
        "failure_alerts_sent": runtime_stats["failure_alerts_sent"],
        "last_universe_build": runtime_stats["last_universe_build"],
        "last_accumulation_scan": runtime_stats["last_accumulation_scan"],
        "last_watchlist_monitor": runtime_stats["last_watchlist_monitor"],
    })


@app.route("/health")
def health():
    return "ok", 200
    
current_universe: List[str] = []
float_cache: Dict[str, Any] = {}

def should_load_float_today(last_float_load_date: Optional[str]) -> bool:
    now_sa = now_saudi()
    if now_sa.hour < 11:
        return False
    today = now_sa.date().isoformat()
    return last_float_load_date != today

def cleanup_old_alerts(
    state: Dict[str, Any],
    max_age_hours: int = 48
) -> None:
    cutoff = now_saudi() - dt.timedelta(hours=max_age_hours)

    for key in (
        "sent_early_alerts",
        "sent_entry_alerts",
        "sent_failure_alerts"
    ):
        items = state.get(key, {})
        cleaned = {}

        for symbol, item in items.items():
            alert_time = parse_iso(
                item.get("time") if isinstance(item, dict) else item
            )

            if alert_time and alert_time >= cutoff:
                cleaned[symbol] = item

        state[key] = cleaned
        
def main_loop():
    global current_universe, float_cache

    runtime_stats["started_at"] = iso_now()
    state = load_state()
    watchlist = load_watchlist()

    print(f"[START] {BOT_NAME}", flush=True)

    last_universe_build_ts = 0.0
    last_scan_ts = 0.0
    last_monitor_ts = 0.0
    last_float_load_date = None
    last_cleanup_ts = 0.0

    while True:
        try:
            if not is_weekday_ny():
                print("[TIME] NY weekend. Sleeping...", flush=True)
                time.sleep(300)
                continue

            if should_load_float_today(last_float_load_date):
                print("[FLOAT] Loading daily float cache from Gist...", flush=True)
                new_float_cache = load_float_cache_from_gist()
                if new_float_cache:
                    float_cache = new_float_cache
                    last_float_load_date = now_saudi().date().isoformat()

            if not float_cache:
                print("[FLOAT] Waiting for daily float cache at 11:00 Saudi...", flush=True)
                time.sleep(300)
                continue

            if runtime_stats.get("startup_message_sent") is not True:
                send_telegram_message(
                    f"✅ <b>{BOT_NAME}</b>\n\n"
                    f"🚀 تم تشغيل البوت بنجاح.\n\n"
                    f"📦 Universe: <b>{len(current_universe)}</b>\n"
                    f"📊 Float Cache: <b>{len(float_cache)}</b>\n"
                    f"👀 Watchlist: <b>{len(watchlist)}</b>\n\n"
                    f"🕒 {now_saudi().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                runtime_stats["startup_message_sent"] = True
                
            now_ts = time.time()
            if now_ts - last_cleanup_ts >= 60 * 60:
                cleanup_old_alerts(state)
                last_cleanup_ts = now_ts

            if now_ts - last_universe_build_ts >= UNIVERSE_REBUILD_INTERVAL or not current_universe:
                current_universe = build_universe()
                runtime_stats["universe_count"] = len(current_universe)
                runtime_stats["last_universe_build"] = iso_now()
                state["last_universe_build"] = iso_now()
                last_universe_build_ts = now_ts
                save_state(state)

            if now_ts - last_scan_ts >= ACCUMULATION_SCAN_INTERVAL:
                print("[SCAN] Running accumulation scan...", flush=True)
                scan_accumulation(current_universe, watchlist, state, float_cache)
                runtime_stats["last_accumulation_scan"] = iso_now()
                state["last_accumulation_scan"] = iso_now()
                save_watchlist(watchlist)
                save_state(state)
                last_scan_ts = now_ts

            if now_ts - last_monitor_ts >= WATCHLIST_MONITOR_INTERVAL:
                print(f"[WATCHLIST] Monitoring watchlist... Count={len(watchlist)}", flush=True)
                monitor_watchlist(watchlist, state, float_cache)
                runtime_stats["last_watchlist_monitor"] = iso_now()
                state["last_watchlist_monitor"] = iso_now()
                save_watchlist(watchlist)
                save_state(state)
                last_monitor_ts = now_ts

            time.sleep(10)

        except Exception as e:
            print(f"[MAIN] Loop error: {e}", flush=True)
            time.sleep(30)


if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
