import os
import json
import time
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

import requests
import pandas as pd
import numpy as np
import pytz
import alpaca_trade_api as tradeapi

# ==============================================================================
# 🧠 Early Accumulation Radar
# File: early_accumulation_radar.py
# ==============================================================================

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID") or os.getenv("APCA_API_KEY")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY") or os.getenv("APCA_SECRET_KEY")
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

STATE_FILE = "accumulation_state.json"
WATCHLIST_FILE = "accumulation_watchlist.json"
FLOAT_CACHE_FILENAME = "float_cache.json"
BOT_NAME = "🧠 رادار التجميع المبكر"

SAUDI_TZ = pytz.timezone("Asia/Riyadh")
NY_TZ = pytz.timezone("America/New_York")

UNIVERSE_REBUILD_INTERVAL = 30 * 60
ACCUMULATION_SCAN_INTERVAL = 5 * 60
WATCHLIST_MONITOR_INTERVAL = 2 * 60
WATCHLIST_TTL_HOURS = 24

PRICE_MIN = float(os.getenv("PRICE_MIN", "0.5"))
PRICE_MAX = float(os.getenv("PRICE_MAX", "20.0"))
MIN_DOLLAR_VOLUME = float(os.getenv("MIN_DOLLAR_VOLUME", "500000"))
MIN_RVOL_EARLY = float(os.getenv("MIN_RVOL_EARLY", "1.5"))
MAX_DAY_GAIN_FOR_EARLY = float(os.getenv("MAX_DAY_GAIN_FOR_EARLY", "15.0"))
ENTRY_RVOL_MIN = float(os.getenv("ENTRY_RVOL_MIN", "2.5"))
FAIL_RVOL_MIN = float(os.getenv("FAIL_RVOL_MIN", "1.2"))
EARLY_ALERT_MIN_SCORE = int(os.getenv("EARLY_ALERT_MIN_SCORE", "80"))

BARS_LIMIT = 160
OBV_LOOKBACK = 120
RESISTANCE_BODY_LOOKBACK = 100
RECENT_LOW_LOOKBACK = 20
MAX_SYMBOLS_PER_BATCH = 200
BATCH_SLEEP_SEC = 0.8

runtime_stats = {
    "started_at": None,
    "last_universe_build": None,
    "last_accumulation_scan": None,
    "last_watchlist_monitor": None,
    "universe_count": 0,
    "watchlist_count": 0,
    "early_alerts_sent": 0,
    "entry_alerts_sent": 0,
    "failure_alerts_sent": 0,
}
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

def now_saudi() -> datetime:
    return datetime.now(SAUDI_TZ)

def now_ny() -> datetime:
    return datetime.now(NY_TZ)

def iso_now() -> str:
    return now_saudi().isoformat()

def parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None

def is_weekday_ny() -> bool:
    return now_ny().weekday() < 5

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
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"[JSON] Failed writing {path}: {e}")

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
    state = read_json_file(STATE_FILE, default_state())
    for key, value in default_state().items():
        state.setdefault(key, value)
    return state

def save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = iso_now()
    write_json_file(STATE_FILE, state)

def load_watchlist() -> Dict[str, Any]:
    return read_json_file(WATCHLIST_FILE, {})

def save_watchlist(watchlist: Dict[str, Any]) -> None:
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
    print("[UNIVERSE] Building universe from Alpaca assets...")
    try:
        assets = api.list_assets(status="active")
    except Exception as e:
        print(f"[UNIVERSE] Failed list_assets: {e}")
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
    print(f"[UNIVERSE] Clean universe: {len(symbols)} symbols")
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
            print(f"[SNAPSHOTS] Batch failed: {e}")
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

def get_1m_bars(symbol: str, limit: int = BARS_LIMIT) -> pd.DataFrame:
    try:
        bars = api.get_bars(symbol, tradeapi.TimeFrame.Minute, limit=limit, adjustment="raw").df
        if bars is None or bars.empty:
            return pd.DataFrame()
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol)
        bars = bars.reset_index()
        needed = {"open", "high", "low", "close", "volume"}
        if not needed.issubset(set(bars.columns)):
            return pd.DataFrame()
        return bars.sort_values("timestamp").reset_index(drop=True)[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    except Exception as e:
        print(f"[BARS] {symbol} failed: {e}")
        return pd.DataFrame()

def get_1m_bars_batch(symbols: List[str], limit: int = BARS_LIMIT) -> Dict[str, pd.DataFrame]:
    result = {}

    if not symbols:
        return result

    for i in range(0, len(symbols), MAX_SYMBOLS_PER_BATCH):
        batch = symbols[i:i + MAX_SYMBOLS_PER_BATCH]

        try:
            bars = api.get_bars(batch, tradeapi.TimeFrame.Minute, limit=limit, adjustment="raw").df

            if bars is None or bars.empty:
                continue

            if isinstance(bars.index, pd.MultiIndex):
                for symbol in batch:
                    try:
                        df = bars.xs(symbol).reset_index()
                        needed = {"open", "high", "low", "close", "volume"}

                        if needed.issubset(set(df.columns)):
                            result[symbol] = (
                                df.sort_values("timestamp")
                                .reset_index(drop=True)[["timestamp", "open", "high", "low", "close", "volume"]]
                                .copy()
                            )

                    except Exception:
                        continue

            else:
                # fallback نادر إذا رجع رمز واحد فقط
                df = bars.reset_index()
                needed = {"open", "high", "low", "close", "volume"}

                if needed.issubset(set(df.columns)) and len(batch) == 1:
                    result[batch[0]] = (
                        df.sort_values("timestamp")
                        .reset_index(drop=True)[["timestamp", "open", "high", "low", "close", "volume"]]
                        .copy()
                    )

        except Exception as e:
            print(f"[BARS BATCH] Batch failed: {e}", flush=True)

        time.sleep(BATCH_SLEEP_SEC)

    return result
    
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

def volume_expansion_ok(df: pd.DataFrame) -> bool:
    if len(df) < 13:
        return False
    volumes = df["volume"].astype(float)
    last3_avg = volumes.tail(3).mean()
    prev10_avg = volumes.iloc[-13:-3].mean()
    return prev10_avg > 0 and last3_avg > prev10_avg * 1.5

def resistance_body_level(df: pd.DataFrame, lookback: int = RESISTANCE_BODY_LOOKBACK) -> Optional[float]:
    if len(df) < lookback + 1:
        return None
    base = df.iloc[-(lookback + 1):-1].copy()
    body_high = base[["open", "close"]].max(axis=1)
    resistance = safe_float(body_high.max(), 0)
    return resistance if resistance > 0 else None

def close_above_resistance(df: pd.DataFrame, resistance: float) -> bool:
    if df.empty or resistance <= 0:
        return False
    return safe_float(df["close"].iloc[-1], 0) > resistance

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
    score = 0
    if volume_expansion_ok(df):
        score += 5
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

def analyze_symbol(symbol: str, snapshot: Any, float_cache: Dict[str, Any], df: pd.DataFrame) -> Optional[Dict[str, Any]]:    price = get_latest_price_from_snapshot(snapshot)
    if not price or price < PRICE_MIN or price > PRICE_MAX:
        return None
    daily_volume = get_daily_volume_from_snapshot(snapshot)
    dollar_volume = price * daily_volume
    if dollar_volume < MIN_DOLLAR_VOLUME:
        return None
    day_change_pct = get_day_change_pct(snapshot, price)
    if day_change_pct > MAX_DAY_GAIN_FOR_EARLY:
        return None
    if df is None or df.empty or len(df) < OBV_LOOKBACK:
        return None
        
    rvol = calculate_rvol(df)
    if rvol < MIN_RVOL_EARLY:
        return None
    resistance = resistance_body_level(df, RESISTANCE_BODY_LOOKBACK)
    metrics = obv_metrics(df)
    float_shares = extract_float_shares(symbol, float_cache)
    f_score, f_tier = float_score(float_shares)
    o_score = obv_score(metrics)
    rv_score = rvol_score(rvol)
    v_score = volume_score(df, dollar_volume)
    res_score = resistance_score(price, resistance)
    total_score = f_score + o_score + rv_score + v_score + res_score
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
        "volume_expansion": volume_expansion_ok(df),
        "resistance": resistance,
        "resistance_score": res_score,
        "distance_to_resistance_pct": distance_to_resistance_pct(price, resistance),
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

def build_entry_alert_message(current: Dict[str, Any], watch: Dict[str, Any]) -> str:
    return (
        f"🚀 <b>تنبيه دخول - تأكيد الاختراق</b>\n\n"
        f"🏷️ السهم: <b>{current['symbol']}</b>\n"
        f"💰 السعر الحالي: <b>{fmt_price(current['price'])}$</b>\n\n"
        f"⭐ درجة التجميع الحالية: <b>{int(current.get('score', 0))}/100</b>\n"
        f"⚡ RVOL: <b>{safe_float(current.get('rvol')):.2f}x</b>\n\n"
        f"🎯 تم اختراق مقاومة البدنة: <b>{fmt_price(current.get('resistance'))}$</b>\n\n"
        f"✅ شروط الدخول المتحققة:\n"
        f"• اختراق أعلى بدنة خلال آخر 100 شمعة 1m\n"
        f"• إغلاق فوق المقاومة\n"
        f"• RVOL أعلى من 2.5\n"
        f"• مؤشر السيولة الموزونة ما زال صاعدًا\n\n"
        f"📋 النتيجة:\nالسهم انتقل من مرحلة التجميع إلى مرحلة الاختراق."
    )

def build_failure_alert_message(symbol: str, price: float, reason: str) -> str:
    return (
        f"❌ <b>فشل التجميع</b>\n\n"
        f"🏷️ السهم: <b>{symbol}</b>\n"
        f"💰 آخر سعر: <b>{fmt_price(price)}$</b>\n\n"
        f"📋 السبب:\n{reason}\n\n"
        f"⏱️ النتيجة:\nتمت إزالته من قائمة المراقبة."
    )

def already_sent_recently(sent_map: Dict[str, Any], symbol: str, hours: int) -> bool:
    item = sent_map.get(symbol)
    if not item:
        return False
    dt = parse_iso(item.get("time") if isinstance(item, dict) else item)
    if not dt:
        return False
    return now_saudi() - dt < timedelta(hours=hours)

def add_to_watchlist(watchlist: Dict[str, Any], data: Dict[str, Any]) -> None:
    symbol = data["symbol"]
    watchlist[symbol] = {
        "symbol": symbol,
        "created_at": iso_now(),
        "expires_at": (now_saudi() + timedelta(hours=WATCHLIST_TTL_HOURS)).isoformat(),
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

def check_entry_conditions(data: Dict[str, Any], df: pd.DataFrame) -> bool:
    resistance = data.get("resistance")
    if not resistance or resistance <= 0:
        return False
    if safe_float(data.get("price"), 0) <= resistance:
        return False
    if not close_above_resistance(df, resistance):
        return False
    if safe_float(data.get("rvol"), 0) <= ENTRY_RVOL_MIN:
        return False
    if not data.get("obv_curve_ok"):
        return False
    return True

def failure_reason(data: Optional[Dict[str, Any]], df: pd.DataFrame, watch: Dict[str, Any]) -> Optional[str]:
    expires_at = parse_iso(watch.get("expires_at"))
    if expires_at and now_saudi() >= expires_at:
        return "انتهت مهلة 24 ساعة بدون تحقق تنبيه دخول."
    if data is None:
        return "لم تعد بيانات السهم كافية لمتابعة التجميع."
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
    to_remove = []
    for symbol in symbols:
        watch = watchlist.get(symbol, {})
        snapshot = snapshots.get(symbol)
        df = get_1m_bars(symbol, BARS_LIMIT)
        current_data = analyze_symbol(symbol, snapshot, float_cache) if snapshot is not None else None
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
                if send_telegram_message(build_entry_alert_message(current_data, watch)):
                    state["sent_entry_alerts"][symbol] = {"time": iso_now(), "price": current_price}
                    runtime_stats["entry_alerts_sent"] += 1
            to_remove.append(symbol)
            continue
        reason = failure_reason(current_data, df, watch)
        if reason:
            if not state["sent_failure_alerts"].get(symbol):
                if send_telegram_message(build_failure_alert_message(symbol, current_price, reason)):
                    state["sent_failure_alerts"][symbol] = {"time": iso_now(), "price": current_price, "reason": reason}
                    runtime_stats["failure_alerts_sent"] += 1
            to_remove.append(symbol)
            continue
        watchlist[symbol] = watch
    for symbol in to_remove:
        watchlist.pop(symbol, None)
    runtime_stats["watchlist_count"] = len(watchlist)

def scan_accumulation(universe: List[str], watchlist: Dict[str, Any], state: Dict[str, Any], float_cache_data: Dict[str, Any]) -> None:
    if not universe:
        return
    snapshots = get_snapshots(universe)
    candidates = []
    for symbol, snapshot in snapshots.items():
        if symbol in watchlist:
            continue
        if state["sent_entry_alerts"].get(symbol):
            continue
        if already_sent_recently(state["sent_early_alerts"], symbol, 12):
            continue
        data = analyze_symbol(symbol, snapshot, float_cache_data)
        if data and data["score"] >= EARLY_ALERT_MIN_SCORE:
            candidates.append(data)
    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    for data in candidates:
        symbol = data["symbol"]
        if send_telegram_message(build_early_alert_message(data)):
            state["sent_early_alerts"][symbol] = {"time": iso_now(), "price": data["price"], "score": data["score"]}
            add_to_watchlist(watchlist, data)
            runtime_stats["early_alerts_sent"] += 1
    runtime_stats["watchlist_count"] = len(watchlist)

current_universe: List[str] = []
float_cache: Dict[str, Any] = {}

def should_load_float_today(last_float_load_date: Optional[str]) -> bool:
    now_sa = now_saudi()

    if now_sa.hour < 11:
        return False

    today = now_sa.date().isoformat()

    return last_float_load_date != today
    
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

            now_ts = time.time()

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
                print("[WATCHLIST] Monitoring watchlist...", flush=True)
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
    main_loop()
    
