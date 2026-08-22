# ==============================================================================
# Unified Live Trade Manager
# Version : 0.1.0
# Build   : LTM-2026-08-17-A
# File    : live_trade_manager.py
#
# Purpose:
#   Unified post-entry manager for:
#   - Early Explosion Bot
#   - Hunter + Direct Entry
#   - Market Radar Bot
#   - Elite Explosion Bot
#   - Elite Catalyst Radar
#
# Notes:
#   - Does NOT place broker orders.
#   - Produces HOLD / PROTECT / EXIT / REENTRY guidance.
#   - Designed to reduce false exits via multi-signal confirmation + recovery window.
# ==============================================================================

import os
import json
import time
import math
import threading
import traceback
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from flask import Flask
import pandas as pd
import pytz
import requests
import alpaca_trade_api as tradeapi

# ------------------------------------------------------------------------------
# Timezones
# ------------------------------------------------------------------------------

TZ_KSA = pytz.timezone("Asia/Riyadh")
TZ_NY = pytz.timezone("America/New_York")

# ------------------------------------------------------------------------------
# Environment
# ------------------------------------------------------------------------------

ALPACA_API_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_BOT3_CHAT_ID")

UPSTASH_REDIS_REST_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

# ------------------------------------------------------------------------------
# Redis keys
# ------------------------------------------------------------------------------

REDIS_PREFIX = "live_trade_manager"
KEY_INCOMING = f"{REDIS_PREFIX}:incoming"
KEY_ACTIVE = f"{REDIS_PREFIX}:active_trades"
KEY_POST_EXIT = f"{REDIS_PREFIX}:post_exit_watch"
KEY_HISTORY = f"{REDIS_PREFIX}:history"
KEY_EVENTS = f"{REDIS_PREFIX}:events"

# ------------------------------------------------------------------------------
# Runtime configuration
# ------------------------------------------------------------------------------

MONITOR_INTERVAL_SEC = float(os.getenv("LTM_MONITOR_INTERVAL_SEC", "3"))
BAR_REFRESH_SEC = float(os.getenv("LTM_BAR_REFRESH_SEC", "15"))
RECOVERY_WINDOW_SEC = float(os.getenv("LTM_RECOVERY_WINDOW_SEC", "25"))
POST_EXIT_WATCH_MINUTES = int(os.getenv("LTM_POST_EXIT_WATCH_MINUTES", "20"))

WEAKENING_CONFIRMATIONS = int(os.getenv("LTM_WEAKENING_CONFIRMATIONS", "2"))
FAILED_CONFIRMATIONS = int(os.getenv("LTM_FAILED_CONFIRMATIONS", "2"))
REENTRY_CONFIRMATIONS = int(os.getenv("LTM_REENTRY_CONFIRMATIONS", "2"))

FALSE_EXIT_REBOUND_PCT = float(os.getenv("LTM_FALSE_EXIT_REBOUND_PCT", "3.0"))
REENTRY_SUCCESS_PCT = float(os.getenv("LTM_REENTRY_SUCCESS_PCT", "3.0"))

MAX_HISTORY_RECORDS = int(os.getenv("LTM_MAX_HISTORY_RECORDS", "5000"))
MAX_EVENT_RECORDS = int(os.getenv("LTM_MAX_EVENT_RECORDS", "10000"))

# Weekly report default: Saturday 10:00 KSA. Configurable through env.
WEEKLY_REPORT_WEEKDAY = int(os.getenv("LTM_WEEKLY_REPORT_WEEKDAY", "5"))  # Monday=0
WEEKLY_REPORT_HOUR_KSA = int(os.getenv("LTM_WEEKLY_REPORT_HOUR_KSA", "10"))
WEEKLY_REPORT_MINUTE_KSA = int(os.getenv("LTM_WEEKLY_REPORT_MINUTE_KSA", "0"))

SOURCE_BOTS = [
    "early_explosion",
    "hunter_direct_entry",
    "market_radar",
    "elite_explosion",
    "elite_catalyst",
]

# ------------------------------------------------------------------------------
# Clients
# ------------------------------------------------------------------------------

if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
    raise RuntimeError(
        "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are required."
    )


class UpstashRestRedis:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def command(self, *args):
        res = requests.post(
            self.url,
            headers=self.headers,
            json=[str(x) for x in args],
            timeout=10,
        )

        if res.status_code != 200:
            raise RuntimeError(
                f"Upstash REST error {res.status_code}: {res.text[:300]}"
            )

        data = res.json()

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"])

        return data.get("result") if isinstance(data, dict) else data

    def ping(self):
        return self.command("PING")

    def hset(self, key, field, value):
        return self.command("HSET", key, field, value)

    def hget(self, key, field):
        return self.command("HGET", key, field)

    def hgetall(self, key):
        result = self.command("HGETALL", key) or []

        if isinstance(result, dict):
            return result

        out = {}

        if isinstance(result, list):
            for i in range(0, len(result) - 1, 2):
                out[str(result[i])] = result[i + 1]

        return out

    def hdel(self, key, field):
        return self.command("HDEL", key, field)

    def hlen(self, key):
        return int(self.command("HLEN", key) or 0)

    def hincrby(self, key, field, amount):
        return self.command(
            "HINCRBY",
            key,
            field,
            int(amount)
        )

    def hincrbyfloat(self, key, field, amount):
        return self.command(
            "HINCRBYFLOAT",
            key,
            field,
            float(amount)
        )

    def rpush(self, key, value):
        return self.command(
            "RPUSH",
            key,
            value
        )

    def lpop(self, key):
        return self.command(
            "LPOP",
            key
        )

    def ltrim(self, key, start, stop):
        return self.command(
            "LTRIM",
            key,
            start,
            stop
        )

    def lrange(self, key, start, stop):
        return self.command(
            "LRANGE",
            key,
            start,
            stop
        ) or []

    def set(self, key, value, nx=False, ex=None):
        args = [
            "SET",
            key,
            value
        ]

        if nx:
            args.append("NX")

        if ex is not None:
            args.extend([
                "EX",
                int(ex)
            ])

        result = self.command(*args)

        return result == "OK"


redis_client = UpstashRestRedis(
    UPSTASH_REDIS_REST_URL,
    UPSTASH_REDIS_REST_TOKEN,
)

api = tradeapi.REST(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    api_version="v2",
)

app = Flask(__name__)

@app.route("/")
def home():
    try:
        active_count = redis_client.hlen(KEY_ACTIVE)
        post_exit_count = redis_client.hlen(KEY_POST_EXIT)

        return (
            f"🧠 Unified Live Trade Manager is Running<br>"
            f"Active Trades: {active_count}<br>"
            f"Post Exit Watch: {post_exit_count}<br>"
            f"Monitor Interval: {MONITOR_INTERVAL_SEC}s<br>"
            f"Recovery Window: {RECOVERY_WINDOW_SEC}s"
        ), 200

    except Exception as e:
        return f"Live Trade Manager Running | Redis error: {e}", 200


@app.route("/health")
def health():
    return "OK", 200


def run_flask():
    port = int(os.getenv("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
        threaded=True
    )
# ------------------------------------------------------------------------------
# In-memory caches / locks
# ------------------------------------------------------------------------------

bar_cache: Dict[str, dict] = {}
bar_cache_lock = threading.Lock()
processing_lock = threading.Lock()

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def now_ts() -> float:
    return time.time()


def now_ksa() -> datetime:
    return datetime.now(TZ_KSA)


def iso_week_key(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts or now_ts(), TZ_KSA)
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def weekly_stats_key(source_bot: str, week_key: Optional[str] = None) -> str:
    return f"{REDIS_PREFIX}:weekly:{week_key or iso_week_key()}:{source_bot}"


def safe_float(value, default=0.0) -> float:
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def pct_change(current: float, base: float) -> float:
    if base <= 0:
        return 0.0
    return ((current - base) / base) * 100.0


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram-Sim] {text}", flush=True)
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"❌ Telegram error: {e}", flush=True)


def append_json_list(key: str, record: dict, max_records: int):
    try:
        redis_client.rpush(
            key,
            json.dumps(
                record,
                ensure_ascii=False,
                default=str
            )
        )

        redis_client.ltrim(
            key,
            -max_records,
            -1
        )

    except Exception as e:
        print(
            f"⚠️ Redis append error {key}: {e}",
            flush=True
        )

def save_event(event_type: str, trade: dict, extra: Optional[dict] = None):
    record = {
        "event_type": event_type,
        "ts": now_ts(),
        "time_ksa": now_ksa().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_id": trade.get("trade_id"),
        "symbol": trade.get("symbol"),
        "source_bot": trade.get("source_bot"),
    }
    if extra:
        record.update(extra)
    append_json_list(KEY_EVENTS, record, MAX_EVENT_RECORDS)


def hincr(source_bot: str, field: str, amount=1, week_key: Optional[str] = None):
    try:
        redis_client.hincrby(weekly_stats_key(source_bot, week_key), field, int(amount))
    except Exception as e:
        print(f"⚠️ Weekly stat increment failed {source_bot}/{field}: {e}", flush=True)


def hincr_float(source_bot: str, field: str, amount: float, week_key: Optional[str] = None):
    try:
        redis_client.hincrbyfloat(
            weekly_stats_key(source_bot, week_key),
            field,
            float(amount),
        )
    except Exception as e:
        print(f"⚠️ Weekly float stat increment failed {source_bot}/{field}: {e}", flush=True)


def update_max_stat(source_bot: str, field: str, value: float, week_key: Optional[str] = None):
    key = weekly_stats_key(source_bot, week_key)
    try:
        current = safe_float(redis_client.hget(key, field), -999999.0)
        if value > current:
            redis_client.hset(key, field, value)
    except Exception as e:
        print(f"⚠️ Max stat update failed: {e}", flush=True)


def update_min_stat(source_bot: str, field: str, value: float, week_key: Optional[str] = None):
    key = weekly_stats_key(source_bot, week_key)
    try:
        raw = redis_client.hget(key, field)
        if raw is None or value < safe_float(raw, 999999.0):
            redis_client.hset(key, field, value)
    except Exception as e:
        print(f"⚠️ Min stat update failed: {e}", flush=True)


def persist_active(trade: dict):
    redis_client.hset(
        KEY_ACTIVE,
        trade["trade_id"],
        json.dumps(trade, ensure_ascii=False, default=str),
    )


def persist_post_exit(trade: dict):
    redis_client.hset(
        KEY_POST_EXIT,
        trade["trade_id"],
        json.dumps(trade, ensure_ascii=False, default=str),
    )


def delete_active(trade_id: str):
    redis_client.hdel(KEY_ACTIVE, trade_id)


def delete_post_exit(trade_id: str):
    redis_client.hdel(KEY_POST_EXIT, trade_id)


def get_hash_records(key: str) -> Dict[str, dict]:
    out = {}
    try:
        raw = redis_client.hgetall(key)
        for k, v in raw.items():
            try:
                obj = json.loads(v)
                if isinstance(obj, dict):
                    out[k] = obj
            except Exception:
                continue
    except Exception as e:
        print(f"⚠️ Redis read failed {key}: {e}", flush=True)
    return out


def market_session_open() -> bool:
    now_ny = datetime.now(TZ_NY)
    if now_ny.weekday() >= 5:
        return False
    start = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
    end = now_ny.replace(hour=20, minute=0, second=0, microsecond=0)
    return start <= now_ny <= end


# ------------------------------------------------------------------------------
# Incoming alert registration
# ------------------------------------------------------------------------------

def normalize_source_bot(raw: str) -> str:
    s = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "early_explosion_bot": "early_explosion",
        "early_explosion": "early_explosion",
        "hunter_direct_entry": "hunter_direct_entry",
        "direct_entry": "hunter_direct_entry",
        "market_radar_bot": "market_radar",
        "market_radar": "market_radar",
        "elite_explosion_bot": "elite_explosion",
        "elite_explosion": "elite_explosion",
        "elite_catalyst_radar": "elite_catalyst",
        "elite_catalyst": "elite_catalyst",
    }
    return aliases.get(s, s or "unknown")


def register_incoming_alert(payload: dict):
    symbol = str(payload.get("symbol") or "").strip().upper()
    entry_price = safe_float(payload.get("entry_price") or payload.get("price"))
    source_bot = normalize_source_bot(payload.get("source_bot"))

    if not symbol or entry_price <= 0:
        print(f"⚠️ Invalid incoming alert: {payload}", flush=True)
        return

    alert_ts = safe_float(payload.get("alert_ts") or payload.get("entry_ts"), now_ts())
    week_key = iso_week_key(alert_ts)

    hincr(source_bot, "alerts_received", 1, week_key)

    # Avoid two simultaneous manager instances for same symbol.
    current_active = get_hash_records(KEY_ACTIVE)
    for existing in current_active.values():
        if existing.get("symbol") == symbol:
            hincr(source_bot, "duplicate_ignored", 1, week_key)
            print(f"ℹ️ Duplicate active symbol ignored: {symbol}", flush=True)
            return

    trade_id = payload.get("trade_id") or f"{source_bot}:{symbol}:{int(alert_ts * 1000)}"

    trade = {
        "trade_id": trade_id,
        "symbol": symbol,
        "source_bot": source_bot,
        "week_key": week_key,

        "entry_price": entry_price,
        "entry_ts": alert_ts,
        "entry_time_ksa": datetime.fromtimestamp(alert_ts, TZ_KSA).strftime("%Y-%m-%d %H:%M:%S"),

        "initial_stop": safe_float(payload.get("stop") or payload.get("stop_loss")),
        "t1": safe_float(payload.get("t1") or payload.get("target1")),
        "t2": safe_float(payload.get("t2") or payload.get("target2")),
        "t3": safe_float(payload.get("t3") or payload.get("target3")),

        "entry_score": safe_float(payload.get("score")),
        "entry_rvol": safe_float(payload.get("rvol")),
        "breakout_level": safe_float(
            payload.get("breakout_level")
            or payload.get("resistance_20")
            or payload.get("resistance")
        ),

        "state": "HEALTHY",
        "previous_state": None,
        "state_changed_ts": now_ts(),

        "peak_price": entry_price,
        "peak_gain_pct": 0.0,
        "current_gain_pct": 0.0,
        "giveback_pct_points": 0.0,

        "h1_hit": False,
        "h2_hit": False,
        "h3_hit": False,

        "weak_count": 0,
        "failed_count": 0,
        "profit_risk_count": 0,
        "recovery_started_ts": None,

        "last_price": entry_price,
        "last_eval_ts": 0.0,
        "last_bar_refresh_ts": 0.0,

        "exit_reason": None,
        "exit_price": None,
        "exit_gain_pct": None,
        "exit_ts": None,

        "reentry_signal_sent": False,
        "reentry_signal_price": None,
        "reentry_signal_ts": None,

        "created_at": now_ts(),
    }

    persist_active(trade)
    hincr(source_bot, "trades_managed", 1, week_key)

    save_event("TRADE_REGISTERED", trade)

    send_telegram_message(
        f"🧠 *Live Trade Manager بدأ متابعة {symbol}*\n"
        f"• المصدر: `{source_bot}`\n"
        f"• Entry: `${entry_price:.4f}`\n"
        f"• المراقبة: كل `{MONITOR_INTERVAL_SEC:g}` ثوانٍ\n"
        f"• منع الخروج الكاذب: `Multi-Signal + Recovery Window`"
    )

    print(f"✅ Registered {trade_id}", flush=True)


def incoming_listener_loop():
    print(
        f"📥 Listening on Upstash REST list: {KEY_INCOMING}",
        flush=True
    )

    while True:
        try:
            raw = redis_client.lpop(KEY_INCOMING)

            if raw is None:
                time.sleep(1)
                continue

            try:
                payload = json.loads(raw)

            except Exception:
                print(
                    f"⚠️ Invalid incoming JSON: {raw}",
                    flush=True
                )
                continue

            if isinstance(payload, dict):
                register_incoming_alert(payload)

        except Exception as e:
            print(
                f"❌ Incoming listener error: {e}",
                flush=True
            )
            time.sleep(2)


# ------------------------------------------------------------------------------
# Market data / indicators
# ------------------------------------------------------------------------------

def fetch_live_price(symbol: str) -> Optional[float]:
    try:
        try:
            trade = api.get_latest_trade(symbol, feed="sip")
        except TypeError:
            trade = api.get_latest_trade(symbol)
        return safe_float(getattr(trade, "price", None), None)
    except Exception as e:
        print(f"⚠️ Latest trade failed {symbol}: {e}", flush=True)
        return None


def get_intraday_bars(symbol: str, force=False) -> Optional[pd.DataFrame]:
    ts = now_ts()

    with bar_cache_lock:
        cached = bar_cache.get(symbol)
        if (
            not force
            and cached
            and ts - cached.get("ts", 0) < BAR_REFRESH_SEC
        ):
            return cached.get("df")

    try:
        now_ny = datetime.now(TZ_NY)
        session_start = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)

        if now_ny < session_start:
            session_start = now_ny - timedelta(hours=2)

        try:
            df = api.get_bars(
                symbol,
                tradeapi.rest.TimeFrame.Minute,
                start=session_start.isoformat(),
                end=now_ny.isoformat(),
                limit=1000,
                adjustment="raw",
                feed="sip",
            ).df
        except Exception:
            df = api.get_bars(
                symbol,
                tradeapi.rest.TimeFrame.Minute,
                start=session_start.isoformat(),
                end=now_ny.isoformat(),
                limit=1000,
                adjustment="raw",
            ).df

        if df is None or df.empty:
            return None

        df = df.sort_index().copy()

        with bar_cache_lock:
            bar_cache[symbol] = {"ts": ts, "df": df}

        return df

    except Exception as e:
        print(f"⚠️ Bars failed {symbol}: {e}", flush=True)
        return None


def calculate_technical_state(df: Optional[pd.DataFrame], current_price: float) -> dict:
    result = {
        "ema9": None,
        "ema20": None,
        "vwap": None,

        "atr_1m": 0.0,
        "atr_1m_pct": 0.0,
        "avg_range_1m_pct": 0.0,

        "volume_accel": 0.0,
        "last_1m_vs_avg": 0.0,

        "lower_high": False,
        "lower_low": False,
        "sell_volume_pressure": False,

        "bar_count": 0,
    }

    if df is None or df.empty or len(df) < 21:
        return result

    try:
        closes = df["close"].astype(float)
        highs = df["high"].astype(float)
        lows = df["low"].astype(float)
        volumes = df["volume"].astype(float)

        prev_close = closes.shift(1)

        true_range = pd.concat(
            [
                highs - lows,
                (highs - prev_close).abs(),
                (lows - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        atr_1m = safe_float(
            true_range.tail(14).mean()
        )

        atr_1m_pct = (
            (atr_1m / current_price) * 100.0
            if current_price > 0
            else 0.0
        )

        recent_ranges_pct = (
            ((highs - lows) / closes.replace(0, float("nan")))
            * 100.0
        )

        avg_range_1m_pct = safe_float(
            recent_ranges_pct.tail(10).mean()
        )

        typical = (df["high"].astype(float) + df["low"].astype(float) + closes) / 3.0
        cum_vol = volumes.cumsum()
        vwap_series = (typical * volumes).cumsum() / cum_vol.replace(0, float("nan"))
        vwap = safe_float(vwap_series.iloc[-1], current_price)

        last_1m = safe_float(volumes.iloc[-1])
        prev_10 = safe_float(volumes.iloc[-11:-1].mean())
        last_1m_vs_avg = last_1m / prev_10 if prev_10 > 0 else 0.0

        last_3 = safe_float(volumes.iloc[-3:].mean())
        prev_7 = safe_float(volumes.iloc[-10:-3].mean())
        volume_accel = last_3 / prev_7 if prev_7 > 0 else 0.0

        # Require a small structure sequence, not one candle.
        lower_high = (
            len(highs) >= 4
            and highs.iloc[-1] < highs.iloc[-2]
            and highs.iloc[-2] <= highs.iloc[-3]
        )

        lower_low = (
            len(lows) >= 4
            and lows.iloc[-1] < lows.iloc[-2]
            and lows.iloc[-2] <= lows.iloc[-3]
        )

        last_open = safe_float(df["open"].iloc[-1])
        last_close = safe_float(df["close"].iloc[-1])
        sell_volume_pressure = (
            last_close < last_open
            and prev_10 > 0
            and last_1m >= prev_10 * 1.5
        )

        result.update({
            "ema9": ema9,
            "ema20": ema20,
            "vwap": vwap,
            "atr_1m": atr_1m,
            "atr_1m_pct": atr_1m_pct,
            "avg_range_1m_pct": avg_range_1m_pct,
            "volume_accel": volume_accel,
            "last_1m_vs_avg": last_1m_vs_avg,
            "lower_high": bool(lower_high),
            "lower_low": bool(lower_low),
            "sell_volume_pressure": bool(sell_volume_pressure),
            "bar_count": len(df),
        })

    except Exception as e:
        print(f"⚠️ Technical calculation error: {e}", flush=True)

    return result

def calculate_dynamic_protective_stop(
    trade: dict,
    current_price: float,
    tech: dict,
) -> dict:
    """
    Volatility-aware protective stop.

    BEFORE_T1:
        Keep the original stop.

    T1_LIGHT:
        Light protection with wider room for volatile stocks.

    T2_MEDIUM:
        Protect a meaningful part of the profit.

    T3_STRONG:
        Stronger trailing protection from the peak.

    The protective stop must never move downward.
    """

    entry = safe_float(
        trade.get("entry_price")
    )

    initial_stop = safe_float(
        trade.get("initial_stop")
    )

    peak_price = max(
        safe_float(
            trade.get("peak_price"),
            entry
        ),
        current_price
    )

    t1 = safe_float(
        trade.get("t1")
    )

    t2 = safe_float(
        trade.get("t2")
    )

    atr_1m_pct = safe_float(
        tech.get("atr_1m_pct")
    )

    avg_range_1m_pct = safe_float(
        tech.get("avg_range_1m_pct")
    )

    volatility_pct = max(
        atr_1m_pct,
        avg_range_1m_pct,
        0.40
    )

    protection = get_target_protection_profile(
        trade
    )

    stage = protection.get(
        "stage",
        "BEFORE_T1"
    )

    # ------------------------------------------------------
    # BEFORE T1
    # ------------------------------------------------------

    if stage == "BEFORE_T1":
        return {
            "stage": stage,
            "protective_stop": initial_stop,
            "previous_stop": safe_float(
                trade.get(
                    "protective_stop",
                    initial_stop
                )
            ),
            "trail_distance_pct": 0.0,
            "volatility_pct": volatility_pct,
            "raised": False,
        }

    # ------------------------------------------------------
    # Stage-specific trailing distance
    # ------------------------------------------------------

    if stage == "T1_LIGHT":
        trail_distance_pct = min(
            6.0,
            max(
                2.0,
                volatility_pct * 2.20
            )
        )

    elif stage == "T2_MEDIUM":
        trail_distance_pct = min(
            4.5,
            max(
                1.5,
                volatility_pct * 1.70
            )
        )

    else:
        # T3_STRONG
        trail_distance_pct = min(
            3.5,
            max(
                1.0,
                volatility_pct * 1.30
            )
        )

    trailing_stop = (
        peak_price
        * (
            1.0
            - trail_distance_pct / 100.0
        )
    )

    # ------------------------------------------------------
    # Minimum stop floor by target stage
    # ------------------------------------------------------

    if stage == "T1_LIGHT":
        # Give volatile stocks a little room around breakeven.
        stage_floor = (
            entry * 0.995
            if entry > 0
            else initial_stop
        )

    elif stage == "T2_MEDIUM":
        # After T2, try to preserve at least most of T1.
        if t1 > 0:
            stage_floor = t1 * 0.995
        else:
            stage_floor = entry * 1.005

    else:
        # After T3, protect approximately the T2 area.
        if t2 > 0:
            stage_floor = t2 * 0.995

        elif t1 > 0:
            stage_floor = t1

        else:
            stage_floor = entry * 1.01

    proposed_stop = max(
        initial_stop,
        stage_floor,
        trailing_stop
    )

    previous_stop = safe_float(
        trade.get(
            "protective_stop",
            initial_stop
        )
    )

    # The stop is one-way only: it may rise, never fall.
    final_stop = max(
        proposed_stop,
        previous_stop
    )

    raised = bool(
        final_stop > previous_stop + 0.0000001
    )

    return {
        "stage": stage,
        "protective_stop": final_stop,
        "previous_stop": previous_stop,
        "trail_distance_pct": trail_distance_pct,
        "volatility_pct": volatility_pct,
        "atr_1m_pct": atr_1m_pct,
        "avg_range_1m_pct": avg_range_1m_pct,
        "peak_price": peak_price,
        "stage_floor": stage_floor,
        "trailing_stop": trailing_stop,
        "raised": raised,
    }
    
# ------------------------------------------------------------------------------
# Core state machine
# ------------------------------------------------------------------------------

def calculate_profit_floor(peak_gain_pct: float) -> Optional[float]:
    """
    Returns a minimum acceptable profit percentage after a meaningful peak.
    It is NOT an automatic exit by itself; structural weakness is still required.
    """
    if peak_gain_pct < 3.0:
        return None
    if peak_gain_pct < 5.0:
        return max(0.8, peak_gain_pct - 2.4)
    if peak_gain_pct < 8.0:
        return max(2.0, peak_gain_pct - 2.6)
    if peak_gain_pct < 12.0:
        return max(4.5, peak_gain_pct - 3.0)
    if peak_gain_pct < 20.0:
        return max(7.0, peak_gain_pct - 3.5)
    return max(10.0, peak_gain_pct - 4.5)

def get_target_protection_profile(trade: dict) -> dict:
    """
    Dynamic protection becomes progressively stronger
    as the trade reaches T1, T2, and T3.

    BEFORE_T1:
        Maximum flexibility.

    T1_LIGHT:
        Light profit protection.

    T2_MEDIUM:
        Medium profit protection.

    T3_STRONG:
        Strong profit protection.
    """

    if trade.get("h3_hit", False):
        return {
            "stage": "T3_STRONG",
            "label": "STRONG",
            "recovery_window_sec": 6.0,
            "required_confirmations": 1,
            "min_weakness_score": 2,
            "peak_keep_ratio": 0.60,
            "max_giveback_pp": 2.2,
        }

    if trade.get("h2_hit", False):
        return {
            "stage": "T2_MEDIUM",
            "label": "MEDIUM",
            "recovery_window_sec": 9.0,
            "required_confirmations": 2,
            "min_weakness_score": 2,
            "peak_keep_ratio": 0.45,
            "max_giveback_pp": 3.0,
        }

    if trade.get("h1_hit", False):
        return {
            "stage": "T1_LIGHT",
            "label": "LIGHT",
            "recovery_window_sec": 15.0,
            "required_confirmations": 2,
            "min_weakness_score": 3,
            "peak_keep_ratio": 0.30,
            "max_giveback_pp": 4.0,
        }

    return {
        "stage": "BEFORE_T1",
        "label": "FLEXIBLE",
        "recovery_window_sec": RECOVERY_WINDOW_SEC,
        "required_confirmations": FAILED_CONFIRMATIONS,
        "min_weakness_score": 3,
        "peak_keep_ratio": 0.0,
        "max_giveback_pp": 6.0,
    }
    
def evaluate_signals(trade: dict, current_price: float, tech: dict) -> dict:
    entry = safe_float(trade.get("entry_price"))
    peak_price = max(safe_float(trade.get("peak_price"), entry), current_price)

    current_gain = pct_change(current_price, entry)
    peak_gain = pct_change(peak_price, entry)
    giveback = max(0.0, peak_gain - current_gain)

    ema9 = tech.get("ema9")
    ema20 = tech.get("ema20")
    vwap = tech.get("vwap")
    volume_accel = safe_float(tech.get("volume_accel"))
    lower_high = bool(tech.get("lower_high"))
    lower_low = bool(tech.get("lower_low"))
    sell_volume_pressure = bool(tech.get("sell_volume_pressure"))

    breakout_level = safe_float(trade.get("breakout_level"))
    breakout_failed = bool(
        breakout_level > 0
        and current_price < breakout_level * 0.995
        and peak_price >= breakout_level
    )

    below_ema9 = bool(ema9 and current_price < ema9 * 0.997)
    ema_bearish = bool(ema9 and ema20 and ema9 < ema20 * 0.998)
    below_vwap = bool(vwap and current_price < vwap * 0.997)

    near_peak = current_price >= peak_price * 0.985
    volume_healthy = volume_accel >= 1.10

    weakness_score = 0
    weakness_reasons = []

    # Peak giveback must be meaningful relative to the move.
    if peak_gain >= 3.0 and giveback >= max(1.2, peak_gain * 0.22):
        weakness_score += 1
        weakness_reasons.append(f"Giveback {giveback:.1f}pp")

    if peak_gain >= 5.0 and giveback >= 2.0:
        weakness_score += 1

    if peak_gain >= 8.0 and giveback >= 3.0:
        weakness_score += 1

    if below_ema9:
        weakness_score += 1
        weakness_reasons.append("فقد EMA9")

    if ema_bearish:
        weakness_score += 2
        weakness_reasons.append("EMA9 تحت EMA20")

    if below_vwap:
        weakness_score += 2
        weakness_reasons.append("فقد VWAP")

    if lower_high:
        weakness_score += 1
        weakness_reasons.append("Lower High")

    if lower_low:
        weakness_score += 1
        weakness_reasons.append("Lower Low")

    if breakout_failed:
        weakness_score += 2
        weakness_reasons.append("Breakout Failure")

    if sell_volume_pressure:
        weakness_score += 1
        weakness_reasons.append("ضغط بيع بحجم مرتفع")

    strength_score = 0
    strength_reasons = []

    if ema9 and current_price >= ema9:
        strength_score += 1
        strength_reasons.append("فوق EMA9")

    if ema9 and ema20 and ema9 >= ema20:
        strength_score += 1
        strength_reasons.append("EMA9 فوق EMA20")

    if vwap and current_price >= vwap:
        strength_score += 1
        strength_reasons.append("فوق VWAP")

    if volume_healthy:
        strength_score += 1
        strength_reasons.append("الحجم داعم")

    if near_peak:
        strength_score += 1
        strength_reasons.append("قريب من القمة")

    protection = get_target_protection_profile(
        trade
    )

    base_profit_floor = calculate_profit_floor(
        peak_gain
    )

    target_stage = protection.get(
        "stage",
        "BEFORE_T1"
    )

    peak_keep_ratio = safe_float(
        protection.get(
            "peak_keep_ratio",
            0.0
        )
    )

    stage_profit_floor = None

    if peak_gain > 0 and peak_keep_ratio > 0:
        stage_profit_floor = (
            peak_gain
            * peak_keep_ratio
        )

    profit_floor_candidates = []

    if base_profit_floor is not None:
        profit_floor_candidates.append(
            base_profit_floor
        )

    if stage_profit_floor is not None:
        profit_floor_candidates.append(
            stage_profit_floor
        )

    if profit_floor_candidates:
        profit_floor = max(
            profit_floor_candidates
        )
    else:
        profit_floor = None

    profit_floor_breached = bool(
        profit_floor is not None
        and current_gain <= profit_floor
    )

    max_giveback_pp = safe_float(
        protection.get(
            "max_giveback_pp",
            999.0
        )
    )

    excessive_stage_giveback = bool(
        peak_gain > 0
        and giveback >= max_giveback_pp
    )

    recovered_structure = bool(
        strength_score >= 4
        and not breakout_failed
        and not lower_low
        and current_price >= entry
    )

    return {
        "current_gain_pct": round(current_gain, 3),
        "peak_gain_pct": round(peak_gain, 3),
        "giveback_pct_points": round(giveback, 3),
        "peak_price": peak_price,

        "weakness_score": weakness_score,
        "weakness_reasons": weakness_reasons,
        "strength_score": strength_score,
        "strength_reasons": strength_reasons,

        "breakout_failed": breakout_failed,
        "below_ema9": below_ema9,
        "ema_bearish": ema_bearish,
        "below_vwap": below_vwap,
        "lower_high": lower_high,
        "lower_low": lower_low,
        "sell_volume_pressure": sell_volume_pressure,

        "profit_floor_pct": profit_floor,
        "profit_floor_breached": profit_floor_breached,
        "protection_stage": target_stage,
        "protection_label": protection.get(
            "label",
            "FLEXIBLE"
        ),
        "protection_recovery_window_sec": safe_float(
            protection.get(
                "recovery_window_sec",
                RECOVERY_WINDOW_SEC
            )
        ),
        "protection_required_confirmations": int(
            protection.get(
                "required_confirmations",
                FAILED_CONFIRMATIONS
            )
        ),
        "protection_min_weakness_score": int(
            protection.get(
                "min_weakness_score",
                3
            )
        ),
        "protection_max_giveback_pp": max_giveback_pp,
        "excessive_stage_giveback": excessive_stage_giveback,
        "recovered_structure": recovered_structure,
    }


def transition_state(trade: dict, new_state: str, current_price: float, signals: dict):
    old_state = trade.get("state")
    if old_state == new_state:
        return

    trade["previous_state"] = old_state
    trade["state"] = new_state
    trade["state_changed_ts"] = now_ts()

    save_event(
        "STATE_CHANGE",
        trade,
        {
            "from": old_state,
            "to": new_state,
            "price": current_price,
            "gain_pct": signals.get("current_gain_pct"),
            "peak_gain_pct": signals.get("peak_gain_pct"),
            "weakness_score": signals.get("weakness_score"),
            "reasons": signals.get("weakness_reasons"),
        },
    )

    # Only meaningful transitions are sent.
    if new_state == "STRONG":
        send_telegram_message(
            f"🟢 *[{trade['symbol']}] الزخم قوي*\n"
            f"• الحالي: `${current_price:.4f}` ({signals['current_gain_pct']:+.2f}%)\n"
            f"• أعلى ربح: `{signals['peak_gain_pct']:+.2f}%`\n"
            f"• الحالة: `STRONG / HOLD`"
        )

    elif new_state == "WEAKENING":
        send_telegram_message(
            f"🟡 *[{trade['symbol']}] ضعف أولي — لا يوجد خروج*\n"
            f"• الحالي: `{signals['current_gain_pct']:+.2f}%`\n"
            f"• أعلى ربح: `{signals['peak_gain_pct']:+.2f}%`\n"
            f"• التراجع من القمة: `{signals['giveback_pct_points']:.2f}` نقطة\n"
            f"• القرار: `مراقبة فقط + انتظار Recovery`"
        )

    elif new_state == "CONFIRMED_WEAKNESS":
        reasons = (
            " + ".join(
                signals.get(
                    "weakness_reasons",
                    []
                )[:4]
            )
            or "ضعف متعدد الإشارات"
        )

        recovery_window = safe_float(
            signals.get(
                "protection_recovery_window_sec",
                RECOVERY_WINDOW_SEC
            ),
            RECOVERY_WINDOW_SEC
        )

        send_telegram_message(
            f"🟠 *[{trade['symbol']}] ضعف مؤكد — نافذة استعادة مفعلة*\n"
            f"• الحالي: `{signals['current_gain_pct']:+.2f}%`\n"
            f"• Peak: `{signals['peak_gain_pct']:+.2f}%`\n"
            f"• الأسباب: `{reasons}`\n"
            f"• لا يوجد خروج الآن؛ نعطي السهم "
            f"`{recovery_window:g}` ثانية للاستعادة."
        )

    elif new_state == "PROFIT_AT_RISK":
        protection_stage = signals.get(
            "protection_stage",
            "BEFORE_T1"
        )

        protection_label = signals.get(
            "protection_label",
            "FLEXIBLE"
        )

        protection_names = {
            "T1_LIGHT": "حماية خفيفة",
            "T2_MEDIUM": "حماية متوسطة",
            "T3_STRONG": "حماية قوية",
        }

        protection_ar = protection_names.get(
            protection_stage,
            "حماية مرنة"
        )

        recovery_window = safe_float(
            signals.get(
                "protection_recovery_window_sec",
                RECOVERY_WINDOW_SEC
            )
        )

        required_confirmations = int(
            signals.get(
                "protection_required_confirmations",
                FAILED_CONFIRMATIONS
            )
        )

        send_telegram_message(
            f"🟠 *[{trade['symbol']}] الربح أصبح معرضًا للخطر*\n"
            f"• الحالي: `{signals['current_gain_pct']:+.2f}%`\n"
            f"• Peak: `{signals['peak_gain_pct']:+.2f}%`\n"
            f"• Profit Floor: `{safe_float(signals.get('profit_floor_pct')):+.2f}%`\n"
            f"• المرحلة: `{protection_stage}`\n"
            f"• الحماية: `{protection_label}` — {protection_ar}\n"
            f"• Recovery: `{recovery_window:g}` ثانية\n"
            f"• التأكيدات المطلوبة: `{required_confirmations}`\n"
            f"• لا يوجد خروج فوري؛ ننتظر تأكيد شروط الحماية."
        )


def hard_stop_hit(trade: dict, current_price: float) -> bool:
    stop = safe_float(trade.get("initial_stop"))
    return stop > 0 and current_price <= stop


def should_exit(trade: dict, signals: dict) -> Tuple[bool, Optional[str]]:
    """
    Exit logic with target-based progressive protection.

    BEFORE_T1:
        Flexible management. Do not create an aggressive
        profit-protection exit merely because price is below entry.

    T1:
        Light protection.

    T2:
        Medium protection.

    T3:
        Strong protection.

    Structural failure remains available independently when
    several bearish signals confirm a genuine reversal.
    """

    weakness = int(
        signals.get(
            "weakness_score",
            0
        )
    )

    current_gain = safe_float(
        signals.get(
            "current_gain_pct"
        )
    )

    profit_floor_breached = bool(
        signals.get(
            "profit_floor_breached"
        )
    )

    excessive_stage_giveback = bool(
        signals.get(
            "excessive_stage_giveback"
        )
    )

    protection_stage = signals.get(
        "protection_stage",
        "BEFORE_T1"
    )

    required_confirmations = int(
        signals.get(
            "protection_required_confirmations",
            FAILED_CONFIRMATIONS
        )
    )

    min_weakness_score = int(
        signals.get(
            "protection_min_weakness_score",
            3
        )
    )

    recovery_window = safe_float(
        signals.get(
            "protection_recovery_window_sec",
            RECOVERY_WINDOW_SEC
        ),
        RECOVERY_WINDOW_SEC
    )

    recovery_started = safe_float(
        trade.get("recovery_started_ts"),
        0
    )

    recovery_elapsed = bool(
        recovery_started > 0
        and now_ts() - recovery_started >= recovery_window
    )

    # ------------------------------------------------------------------
    # Progressive target protection
    # ------------------------------------------------------------------

    target_protection_active = protection_stage in (
        "T1_LIGHT",
        "T2_MEDIUM",
        "T3_STRONG",
    )
    protection_activated_ts = safe_float(
        trade.get("protection_activated_ts"),
        0
    )

    protection_activation_grace = bool(
        protection_activated_ts > 0
        and now_ts() - protection_activated_ts < MONITOR_INTERVAL_SEC
    )
    
    protection_condition = bool(
        target_protection_active
        and not protection_activation_grace
        and current_gain > 0
        and weakness >= min_weakness_score
        and (
            profit_floor_breached
            or excessive_stage_giveback
        )
    )

    if protection_condition:
        trade["profit_risk_count"] = (
            int(
                trade.get(
                    "profit_risk_count",
                    0
                )
            )
            + 1
        )
    else:
        trade["profit_risk_count"] = 0

    # ------------------------------------------------------------------
    # Broad momentum failure
    # ------------------------------------------------------------------

    if weakness >= 6:
        trade["failed_count"] = (
            int(
                trade.get(
                    "failed_count",
                    0
                )
            )
            + 1
        )
    else:
        trade["failed_count"] = 0

    # ------------------------------------------------------------------
    # T1 / T2 / T3 profit protection
    # ------------------------------------------------------------------

    if (
        protection_condition
        and trade["profit_risk_count"] >= required_confirmations
        and recovery_elapsed
    ):
        return True, f"PROFIT_PROTECTION_{protection_stage}"

    # ------------------------------------------------------------------
    # Genuine momentum failure
    # ------------------------------------------------------------------

    if (
        recovery_elapsed
        and trade["failed_count"] >= FAILED_CONFIRMATIONS
        and weakness >= 6
    ):
        return True, "MOMENTUM_FAILED"

    # ------------------------------------------------------------------
    # Severe structural reversal
    # ------------------------------------------------------------------

    severe_break = bool(
        signals.get("breakout_failed")
        and signals.get("below_ema9")
        and (
            signals.get("lower_low")
            or signals.get("below_vwap")
        )
        and weakness >= 6
    )

    if (
        severe_break
        and trade["failed_count"] >= FAILED_CONFIRMATIONS
    ):
        return True, "TRUE_REVERSAL"

    return False, None

def update_state_machine(trade: dict, current_price: float, signals: dict):
    weakness = int(
        signals.get(
            "weakness_score",
            0
        )
    )

    strength = int(
        signals.get(
            "strength_score",
            0
        )
    )

    recovered = bool(
        signals.get(
            "recovered_structure"
        )
    )

    protection_stage = signals.get(
        "protection_stage",
        "BEFORE_T1"
    )

    target_protection_active = protection_stage in (
        "T1_LIGHT",
        "T2_MEDIUM",
        "T3_STRONG",
    )

    min_weakness_score = int(
        signals.get(
            "protection_min_weakness_score",
            3
        )
    )

    profit_floor_breached = bool(
        signals.get(
            "profit_floor_breached"
        )
    )

    excessive_stage_giveback = bool(
        signals.get(
            "excessive_stage_giveback"
        )
    )

    # ------------------------------------------------------------------
    # Full structural recovery
    # ------------------------------------------------------------------

    if recovered:
        trade["weak_count"] = 0
        trade["failed_count"] = 0
        trade["profit_risk_count"] = 0
        trade["recovery_started_ts"] = None

        if strength >= 5:
            transition_state(
                trade,
                "STRONG",
                current_price,
                signals
            )
        else:
            transition_state(
                trade,
                "HEALTHY",
                current_price,
                signals
            )

        return

    # ------------------------------------------------------------------
    # Weakness confirmation counter
    # ------------------------------------------------------------------

    if weakness >= 3:
        trade["weak_count"] = (
            int(
                trade.get(
                    "weak_count",
                    0
                )
            )
            + 1
        )
    else:
        trade["weak_count"] = 0

    # ------------------------------------------------------------------
    # Target-based profit protection
    #
    # IMPORTANT:
    # Before T1, ordinary profit-floor logic must NOT activate
    # PROFIT_AT_RISK.
    # ------------------------------------------------------------------

    target_profit_at_risk = bool(
        target_protection_active
        and signals.get("current_gain_pct", 0) > 0
        and weakness >= min_weakness_score
        and (
            profit_floor_breached
            or excessive_stage_giveback
        )
    )

    if target_profit_at_risk:
        if not trade.get("recovery_started_ts"):
            trade["recovery_started_ts"] = now_ts()

        transition_state(
            trade,
            "PROFIT_AT_RISK",
            current_price,
            signals
        )

        return

    # ------------------------------------------------------------------
    # Confirmed structural weakness
    # ------------------------------------------------------------------

    if (
        weakness >= 5
        and trade["weak_count"] >= WEAKENING_CONFIRMATIONS
    ):
        if not trade.get("recovery_started_ts"):
            trade["recovery_started_ts"] = now_ts()

        transition_state(
            trade,
            "CONFIRMED_WEAKNESS",
            current_price,
            signals
        )

        return

    # ------------------------------------------------------------------
    # Initial weakness only
    # ------------------------------------------------------------------

    if weakness >= 3:
        transition_state(
            trade,
            "WEAKENING",
            current_price,
            signals
        )

        return

    
    if weakness < 3:
        trade["weak_count"] = 0

    if strength >= 5:
        transition_state(
            trade,
            "STRONG",
            current_price,
            signals
        )
    else:
        transition_state(
            trade,
            "HEALTHY",
            current_price,
            signals
        )

# ------------------------------------------------------------------------------
# Targets / closing / post-exit
# ------------------------------------------------------------------------------

def check_targets(trade: dict, current_price: float):
    changed = False

    target_configs = {
        1: {
            "hit_key": "h1_hit",
            "stage": "T1_LIGHT",
            "protection_label": "LIGHT",
            "protection_ar": "حماية خفيفة",
        },
        2: {
            "hit_key": "h2_hit",
            "stage": "T2_MEDIUM",
            "protection_label": "MEDIUM",
            "protection_ar": "حماية متوسطة",
        },
        3: {
            "hit_key": "h3_hit",
            "stage": "T3_STRONG",
            "protection_label": "STRONG",
            "protection_ar": "حماية قوية",
        },
    }

    for idx in (1, 2, 3):
        key = f"t{idx}"

        config = target_configs[idx]

        hit_key = config["hit_key"]

        target = safe_float(
            trade.get(key)
        )

        if (
            target > 0
            and current_price >= target
            and not trade.get(hit_key)
        ):
            trade[hit_key] = True
            changed = True

            trade["protection_stage"] = (
                config["stage"]
            )

            trade["protection_activated_ts"] = (
                now_ts()
            )

            trade["recovery_started_ts"] = None
            trade["profit_risk_count"] = 0
            trade["weak_count"] = 0
            trade["failed_count"] = 0

            hincr(
                trade["source_bot"],
                f"t{idx}_hit",
                1,
                trade["week_key"],
            )

            save_event(
                f"T{idx}_HIT",
                trade,
                {
                    "price": current_price,
                    "target": target,
                    "protection_stage": config["stage"],
                },
            )

            send_telegram_message(
                f"🎯 *[{trade['symbol']}] تحقق T{idx}*\n"
                f"• السعر: `${current_price:.4f}`\n"
                f"• المصدر: `{trade['source_bot']}`\n"
                f"• مستوى الحماية: "
                f"`{config['protection_label']}` "
                f"— {config['protection_ar']}\n"
                f"• المراقبة اللحظية مستمرة."
            )

    return changed    

def close_trade_to_post_exit(
    trade: dict,
    current_price: float,
    reason: str,
    signals: Optional[dict] = None,
):
    exit_gain = pct_change(current_price, safe_float(trade.get("entry_price")))

    trade["exit_reason"] = reason
    trade["exit_price"] = current_price
    trade["exit_gain_pct"] = round(exit_gain, 3)
    trade["exit_ts"] = now_ts()
    trade["post_exit_until_ts"] = now_ts() + (POST_EXIT_WATCH_MINUTES * 60)
    trade["post_exit_max_price"] = current_price
    trade["post_exit_min_price"] = current_price
    trade["post_exit_max_rebound_pct"] = 0.0
    trade["post_exit_max_drop_pct"] = 0.0
    trade["false_exit"] = False
    trade["true_reversal_after_exit"] = False
    trade["reentry_confirm_count"] = 0

    delete_active(trade["trade_id"])
    persist_post_exit(trade)

    if reason == "HARD_STOP":
        hincr(
            trade["source_bot"],
            "hard_stop",
            1,
            trade["week_key"],
        )

    elif reason.startswith("PROFIT_PROTECTION_"):
        hincr(
            trade["source_bot"],
            "profit_protection_exit",
            1,
            trade["week_key"],
        )

        if reason == "PROFIT_PROTECTION_T1_LIGHT":
            hincr(
                trade["source_bot"],
                "profit_protection_t1",
                1,
                trade["week_key"],
            )

        elif reason == "PROFIT_PROTECTION_T2_MEDIUM":
            hincr(
                trade["source_bot"],
                "profit_protection_t2",
                1,
                trade["week_key"],
            )

        elif reason == "PROFIT_PROTECTION_T3_STRONG":
            hincr(
                trade["source_bot"],
                "profit_protection_t3",
                1,
                trade["week_key"],
            )

    elif reason == "MOMENTUM_FAILED":
        hincr(
            trade["source_bot"],
            "momentum_failed",
            1,
            trade["week_key"],
        )

    elif reason == "TRUE_REVERSAL":
        hincr(
            trade["source_bot"],
            "true_reversal_exit",
            1,
            trade["week_key"],
        )

    reasons = ""
    if signals:
        reasons = " + ".join(signals.get("weakness_reasons", [])[:5])

    reason_labels = {
        "HARD_STOP": "ضرب الوقف",
        "PROFIT_PROTECTION_T1_LIGHT": "حماية ربح خفيفة بعد T1",
        "PROFIT_PROTECTION_T2_MEDIUM": "حماية ربح متوسطة بعد T2",
        "PROFIT_PROTECTION_T3_STRONG": "حماية ربح قوية بعد T3",
        "MOMENTUM_FAILED": "فشل الزخم",
        "TRUE_REVERSAL": "انعكاس مؤكد",
    }

    reason_label = reason_labels.get(
        reason,
        reason
    )
    
    emoji = "🛑" if reason == "HARD_STOP" else "🔴"

    send_telegram_message(
        f"{emoji} *[{trade['symbol']}] خروج مقترح — {reason_label}*\n"
        f"• السعر: `${current_price:.4f}`\n"
        f"• نتيجة من Entry: `{exit_gain:+.2f}%`\n"
        f"• أعلى ربح سابق: `{safe_float(trade.get('peak_gain_pct')):+.2f}%`\n"
        + (f"• الأسباب: `{reasons}`\n" if reasons else "")
        + f"• سيستمر `POST_EXIT_WATCH` لمدة `{POST_EXIT_WATCH_MINUTES}` دقيقة لاكتشاف الارتداد/إعادة الدخول."
    )

    save_event(
        "EXIT_SUGGESTED",
        trade,
        {
            "reason": reason,
            "exit_price": current_price,
            "exit_gain_pct": exit_gain,
            "peak_gain_pct": trade.get("peak_gain_pct"),
            "weakness_reasons": signals.get("weakness_reasons") if signals else [],
        },
    )


def evaluate_reentry(trade: dict, current_price: float, tech: dict) -> bool:
    ema9 = tech.get("ema9")
    ema20 = tech.get("ema20")
    vwap = tech.get("vwap")
    volume_accel = safe_float(tech.get("volume_accel"))

    if not ema9 or not ema20 or not vwap:
        return False

    exit_price = safe_float(trade.get("exit_price"))
    breakout_level = safe_float(trade.get("breakout_level"))

    reclaim_price = max(
        exit_price * 1.01,
        breakout_level if breakout_level > 0 else 0,
    )

    return bool(
        current_price >= reclaim_price
        and current_price >= ema9
        and ema9 >= ema20
        and current_price >= vwap
        and volume_accel >= 1.15
        and not tech.get("lower_low")
    )


def finalize_trade_history(trade: dict):
    source = trade["source_bot"]
    week_key = trade["week_key"]

    exit_gain = safe_float(trade.get("exit_gain_pct"))
    mfe = safe_float(trade.get("peak_gain_pct"))

    hincr(source, "completed_trades", 1, week_key)
    hincr_float(source, "sum_mfe_pct", mfe, week_key)
    hincr_float(source, "sum_exit_return_pct", exit_gain, week_key)

    if exit_gain > 0:
        hincr(source, "wins", 1, week_key)
    elif exit_gain < 0:
        hincr(source, "losses", 1, week_key)
    else:
        hincr(source, "flat", 1, week_key)

    update_max_stat(source, "max_mfe_pct", mfe, week_key)
    update_max_stat(source, "best_exit_return_pct", exit_gain, week_key)
    update_min_stat(source, "worst_exit_return_pct", exit_gain, week_key)

    if trade.get("false_exit"):
        hincr(source, "false_exit", 1, week_key)

    if trade.get("true_reversal_after_exit"):
        hincr(source, "true_reversal_after_exit", 1, week_key)

    if trade.get("reentry_signal_sent"):
        hincr(source, "reentry_possible", 1, week_key)

    if trade.get("reentry_success"):
        hincr(source, "reentry_success", 1, week_key)

    history_record = dict(trade)
    history_record["finalized_ts"] = now_ts()
    history_record["finalized_time_ksa"] = now_ksa().strftime("%Y-%m-%d %H:%M:%S")

    append_json_list(KEY_HISTORY, history_record, MAX_HISTORY_RECORDS)
    save_event("TRADE_FINALIZED", trade)

    delete_post_exit(trade["trade_id"])


# ------------------------------------------------------------------------------
# Monitoring loops
# ------------------------------------------------------------------------------

def evaluate_active_trade(trade: dict):
    symbol = trade["symbol"]

    current_price = fetch_live_price(symbol)
    if not current_price or current_price <= 0:
        return

    trade["last_price"] = current_price
    trade["last_eval_ts"] = now_ts()

    if current_price > safe_float(trade.get("peak_price"), trade["entry_price"]):
        trade["peak_price"] = current_price

    current_gain = pct_change(current_price, safe_float(trade.get("entry_price")))
    peak_gain = pct_change(
        safe_float(trade.get("peak_price")),
        safe_float(trade.get("entry_price")),
    )

    trade["current_gain_pct"] = round(current_gain, 3)
    trade["peak_gain_pct"] = round(peak_gain, 3)
    trade["giveback_pct_points"] = round(max(0.0, peak_gain - current_gain), 3)

    check_targets(trade, current_price)

    if hard_stop_hit(trade, current_price):
        close_trade_to_post_exit(trade, current_price, "HARD_STOP")
        return

    df = get_intraday_bars(symbol)
    tech = calculate_technical_state(df, current_price)
    signals = evaluate_signals(trade, current_price, tech)

    trade["peak_price"] = signals["peak_price"]
    trade["peak_gain_pct"] = signals["peak_gain_pct"]
    trade["current_gain_pct"] = signals["current_gain_pct"]
    trade["giveback_pct_points"] = signals["giveback_pct_points"]

    trade["last_technical_state"] = {
        "ema9": tech.get("ema9"),
        "ema20": tech.get("ema20"),
        "vwap": tech.get("vwap"),
        "volume_accel": tech.get("volume_accel"),
        "last_1m_vs_avg": tech.get("last_1m_vs_avg"),
        "lower_high": tech.get("lower_high"),
        "lower_low": tech.get("lower_low"),
        "sell_volume_pressure": tech.get("sell_volume_pressure"),
    }

    trade["last_signals"] = signals

    update_state_machine(trade, current_price, signals)

    exit_now, reason = should_exit(trade, signals)
    if exit_now:
        close_trade_to_post_exit(trade, current_price, reason, signals)
        return

    persist_active(trade)


def active_monitor_loop():
    print(f"⚡ Active monitor running every {MONITOR_INTERVAL_SEC:g}s", flush=True)

    while True:
        started = now_ts()

        try:
            active = get_hash_records(KEY_ACTIVE)

            for trade_id, trade in active.items():
                try:
                    evaluate_active_trade(trade)
                except Exception as e:
                    print(
                        f"❌ Active trade error {trade.get('symbol')}: {e}\n"
                        f"{traceback.format_exc()}",
                        flush=True,
                    )

        except Exception as e:
            print(f"❌ Active monitor loop error: {e}", flush=True)

        elapsed = now_ts() - started
        time.sleep(max(0.5, MONITOR_INTERVAL_SEC - elapsed))


def evaluate_post_exit_trade(trade: dict):
    symbol = trade["symbol"]
    current_price = fetch_live_price(symbol)
    if not current_price or current_price <= 0:
        return

    exit_price = safe_float(trade.get("exit_price"))
    if exit_price <= 0:
        finalize_trade_history(trade)
        return

    trade["post_exit_max_price"] = max(
        safe_float(trade.get("post_exit_max_price"), current_price),
        current_price,
    )
    trade["post_exit_min_price"] = min(
        safe_float(trade.get("post_exit_min_price"), current_price),
        current_price,
    )

    rebound_pct = pct_change(trade["post_exit_max_price"], exit_price)
    drop_pct = pct_change(trade["post_exit_min_price"], exit_price)

    trade["post_exit_max_rebound_pct"] = round(rebound_pct, 3)
    trade["post_exit_max_drop_pct"] = round(drop_pct, 3)

    # False exit metric: strong rebound after our exit suggestion.
    if rebound_pct >= FALSE_EXIT_REBOUND_PCT and not trade.get("false_exit"):
        trade["false_exit"] = True
        save_event(
            "FALSE_EXIT_DETECTED",
            trade,
            {"rebound_pct": rebound_pct},
        )

    # Evidence that the exit was followed by continued deterioration.
    if drop_pct <= -2.0:
        trade["true_reversal_after_exit"] = True

    df = get_intraday_bars(symbol)
    tech = calculate_technical_state(df, current_price)

    if evaluate_reentry(trade, current_price, tech):
        trade["reentry_confirm_count"] = int(trade.get("reentry_confirm_count", 0)) + 1
    else:
        trade["reentry_confirm_count"] = 0

    if (
        trade["reentry_confirm_count"] >= REENTRY_CONFIRMATIONS
        and not trade.get("reentry_signal_sent")
    ):
        trade["reentry_signal_sent"] = True
        trade["reentry_signal_price"] = current_price
        trade["reentry_signal_ts"] = now_ts()

        send_telegram_message(
            f"🔄 *[{symbol}] REENTRY_POSSIBLE — استعادة قوية بعد الخروج*\n"
            f"• السعر: `${current_price:.4f}`\n"
            f"• فوق EMA9 / EMA20 / VWAP\n"
            f"• الحجم عاد للدعم\n"
            f"• هذه `Reclaim` مؤكدة بعد `{REENTRY_CONFIRMATIONS}` فحوص، وليست ارتدادًا لحظيًا فقط."
        )

        save_event(
            "REENTRY_POSSIBLE",
            trade,
            {"price": current_price},
        )

    if trade.get("reentry_signal_sent") and not trade.get("reentry_success"):
        reentry_price = safe_float(trade.get("reentry_signal_price"))
        if reentry_price > 0 and pct_change(current_price, reentry_price) >= REENTRY_SUCCESS_PCT:
            trade["reentry_success"] = True
            save_event(
                "REENTRY_SUCCESS",
                trade,
                {
                    "signal_price": reentry_price,
                    "current_price": current_price,
                },
            )

    persist_post_exit(trade)

    if now_ts() >= safe_float(trade.get("post_exit_until_ts")):
        finalize_trade_history(trade)


def post_exit_monitor_loop():
    print(
        f"👀 Post-exit watcher enabled for {POST_EXIT_WATCH_MINUTES} minutes",
        flush=True,
    )

    while True:
        started = now_ts()

        try:
            records = get_hash_records(KEY_POST_EXIT)

            for trade_id, trade in records.items():
                try:
                    evaluate_post_exit_trade(trade)
                except Exception as e:
                    print(
                        f"❌ Post-exit error {trade.get('symbol')}: {e}",
                        flush=True,
                    )

        except Exception as e:
            print(f"❌ Post-exit loop error: {e}", flush=True)

        elapsed = now_ts() - started
        time.sleep(max(1.0, MONITOR_INTERVAL_SEC - elapsed))


# ------------------------------------------------------------------------------
# Weekly report
# ------------------------------------------------------------------------------

def get_week_stats(source_bot: str, week_key: str) -> dict:
    try:
        raw = redis_client.hgetall(weekly_stats_key(source_bot, week_key))
    except Exception:
        raw = {}

    def iv(field):
        return int(safe_float(raw.get(field), 0))

    def fv(field):
        return safe_float(raw.get(field), 0.0)

    completed = iv("completed_trades")
    alerts = iv("alerts_received")
    managed = iv("trades_managed")

    t1 = iv("t1_hit")
    t2 = iv("t2_hit")
    t3 = iv("t3_hit")
    hard_stop = iv("hard_stop")
    momentum_failed = iv("momentum_failed")
    true_reversal_exit = iv("true_reversal_exit")
    profit_exit = iv("profit_protection_exit")
    false_exit = iv("false_exit")
    reentry_possible = iv("reentry_possible")
    reentry_success = iv("reentry_success")
    wins = iv("wins")
    losses = iv("losses")

    avg_mfe = fv("sum_mfe_pct") / completed if completed else 0.0
    avg_return = fv("sum_exit_return_pct") / completed if completed else 0.0
    success_rate = (wins / completed * 100.0) if completed else 0.0

    # Quality score intentionally penalizes false exits and hard stops.
    quality = (
        (success_rate * 0.30)
        + ((t1 / completed * 100.0) if completed else 0.0) * 0.20
        + ((t2 / completed * 100.0) if completed else 0.0) * 0.10
        + min(max(avg_mfe, 0.0) * 4.0, 100.0) * 0.20
        + min(max(avg_return + 10.0, 0.0) * 5.0, 100.0) * 0.20
        - ((hard_stop / completed * 100.0) if completed else 0.0) * 0.15
        - ((false_exit / completed * 100.0) if completed else 0.0) * 0.10
    )

    return {
        "source_bot": source_bot,
        "alerts": alerts,
        "managed": managed,
        "completed": completed,
        "t1": t1,
        "t2": t2,
        "t3": t3,
        "hard_stop": hard_stop,
        "momentum_failed": momentum_failed,
        "true_reversal_exit": true_reversal_exit,
        "profit_exit": profit_exit,
        "false_exit": false_exit,
        "reentry_possible": reentry_possible,
        "reentry_success": reentry_success,
        "wins": wins,
        "losses": losses,
        "avg_mfe": avg_mfe,
        "max_mfe": fv("max_mfe_pct"),
        "avg_return": avg_return,
        "best_return": fv("best_exit_return_pct"),
        "worst_return": fv("worst_exit_return_pct"),
        "success_rate": success_rate,
        "quality": quality,
    }


def pct_rate(num, den):
    return (num / den * 100.0) if den else 0.0


def build_weekly_report(week_key: str) -> str:
    stats = [get_week_stats(bot, week_key) for bot in SOURCE_BOTS]
    stats = [s for s in stats if s["alerts"] > 0 or s["managed"] > 0 or s["completed"] > 0]

    if not stats:
        return (
            f"📊 *تقرير Live Trade Manager الأسبوعي*\n"
            f"الأسبوع: `{week_key}`\n\n"
            f"لا توجد تنبيهات مسجلة خلال هذا الأسبوع."
        )

    lines = [
        "📊 *تقرير Live Trade Manager الأسبوعي*",
        f"الأسبوع: `{week_key}`",
        "",
    ]

    for s in stats:
        c = s["completed"]
        lines.extend([
            f"🤖 *{s['source_bot']}*",
            f"• التنبيهات المستلمة: `{s['alerts']}` | تمت إدارتها: `{s['managed']}` | المكتملة: `{c}`",
            f"• T1: `{s['t1']}` ({pct_rate(s['t1'], c):.1f}%) | "
            f"T2: `{s['t2']}` ({pct_rate(s['t2'], c):.1f}%) | "
            f"T3: `{s['t3']}` ({pct_rate(s['t3'], c):.1f}%)",
            f"• متوسط أعلى ارتفاع MFE: `{s['avg_mfe']:+.2f}%` | الأعلى: `{s['max_mfe']:+.2f}%`",
            f"• متوسط نتيجة الخروج: `{s['avg_return']:+.2f}%` | نجاح: `{s['success_rate']:.1f}%`",
            f"• Hard Stop: `{s['hard_stop']}` | Momentum Failed: `{s['momentum_failed']}` | "
            f"True Reversal Exit: `{s['true_reversal_exit']}`",
            f"• Profit Protection: `{s['profit_exit']}` | False Exit: `{s['false_exit']}`",
            f"• Re-entry Possible: `{s['reentry_possible']}` | ناجحة لاحقًا: `{s['reentry_success']}`",
            f"• أفضل نتيجة: `{s['best_return']:+.2f}%` | أسوأ نتيجة: `{s['worst_return']:+.2f}%`",
            "",
        ])

    best_overall = max(stats, key=lambda x: x["quality"])
    best_targets = max(
        stats,
        key=lambda x: (
            pct_rate(x["t1"], x["completed"])
            + pct_rate(x["t2"], x["completed"])
            + pct_rate(x["t3"], x["completed"])
        ),
    )
    best_mfe = max(stats, key=lambda x: x["avg_mfe"])
    least_stops = min(
        stats,
        key=lambda x: pct_rate(x["hard_stop"], x["completed"]) if x["completed"] else 999,
    )
    most_failures = max(
        stats,
        key=lambda x: pct_rate(
            x["momentum_failed"] + x["true_reversal_exit"],
            x["completed"],
        ),
    )

    lines.extend([
        "🏆 *الخلاصة الأسبوعية*",
        f"• الأفضل إجمالًا: `{best_overall['source_bot']}`",
        f"• الأفضل في الوصول للأهداف: `{best_targets['source_bot']}`",
        f"• الأعلى بمتوسط MFE: `{best_mfe['source_bot']}` ({best_mfe['avg_mfe']:+.2f}%)",
        f"• الأقل ضربًا للوقف: `{least_stops['source_bot']}`",
        f"• الأكثر فشل زخم/انعكاس: `{most_failures['source_bot']}`",
        "",
        "_التقييم الإجمالي يراعي الربح والمخاطرة والـMFE والوقف والـFalse Exit، وليس عدد التنبيهات فقط._",
    ])

    return "\n".join(lines)


def weekly_report_loop():
    while True:
        try:
            dt = now_ksa()

            if (
                dt.weekday() == WEEKLY_REPORT_WEEKDAY
                and dt.hour == WEEKLY_REPORT_HOUR_KSA
                and dt.minute >= WEEKLY_REPORT_MINUTE_KSA
                and dt.minute < WEEKLY_REPORT_MINUTE_KSA + 10
            ):
                week_key = iso_week_key()
                sent_key = f"{REDIS_PREFIX}:weekly_report_sent:{week_key}"

                if redis_client.set(sent_key, "1", nx=True, ex=14 * 24 * 3600):
                    report = build_weekly_report(week_key)
                    send_telegram_message(report)
                    print(f"✅ Weekly report sent: {week_key}", flush=True)

            time.sleep(60)

        except Exception as e:
            print(f"❌ Weekly report loop error: {e}", flush=True)
            time.sleep(60)


# ------------------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------------------

def startup_message():
    active_count = redis_client.hlen(KEY_ACTIVE)
    post_count = redis_client.hlen(KEY_POST_EXIT)

    msg = (
        "🧠 *Unified Live Trade Manager Started*\n"
        f"• Active trades: `{active_count}`\n"
        f"• Post-exit watch: `{post_count}`\n"
        f"• Monitor interval: `{MONITOR_INTERVAL_SEC:g}s`\n"
        f"• Recovery window: `{RECOVERY_WINDOW_SEC:g}s`\n"
        f"• False-exit protection: `ACTIVE`\n"
        f"• Weekly report: `ACTIVE`"
    )
    send_telegram_message(msg)

def main():
    redis_client.ping()

    threading.Thread(
        target=run_flask,
        daemon=True
    ).start()

    threading.Thread(
        target=incoming_listener_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=active_monitor_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=post_exit_monitor_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=weekly_report_loop,
        daemon=True
    ).start()

    startup_message()

    print(
        "✅ Unified Live Trade Manager Web Service is running.",
        flush=True
    )

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
