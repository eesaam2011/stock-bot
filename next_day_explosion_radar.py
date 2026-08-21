# ==============================================================================
# Next-Day Explosion Radar
# Version : 0.5.0
# Build   : DRYRUN-READY-2026-08-21-D
# File    : next_day_explosion_radar.py
#
# Deployment:
#   Render Web Service
#
# Start Command:
#   python next_day_explosion_radar.py
#
# Architecture:
#   1) Discovery Engine:
#        SIP After-Hours -> BOATS Overnight -> SIP Pre-Market
#   2) Shared news consumer:
#        Redis Hash: market_radar:news
#        Producer remains Elite Catalyst Radar.
#   3) Shared float consumer:
#        Existing Gist float_cache.json
#   4) Explainable scoring:
#        Opportunity 0-100
#        Structural Risk LOW/MODERATE/HIGH/CRITICAL
#        Dynamic Failure Pressure 0-100
#   5) State machine:
#        STEALTH -> AWAKENING -> ACCEPTED -> BUILDING -> BREAKOUT_READY
#        -> ELITE_CONTINUATION
#        or CROWDED -> EXHAUSTED / FAILED
#   6) Entry Confirmation Engine:
#        Runs inside THIS bot. Discovery is not an entry.
#   7) Optional Unified Live Trade Manager handoff after confirmed entry.
#
# Notes:
#   - No Finnhub calls. News comes from the existing Central News Hub.
#   - No live float calls. Float comes from the existing shared Gist cache.
#   - Core thresholds are intentionally configurable because Pilot/Backtest
#     calibration is still required.
# ==============================================================================

from __future__ import annotations

import os
import re
import json
import math
import time
import html
import threading
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone, time as dtime
from enum import Enum
from statistics import median, pstdev
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify

# ==============================================================================
# Identity / Timezones
# ==============================================================================

BOT_NAME = "Next-Day Explosion Radar"
BOT_NAME_AR = "رادار انفجار اليوم التالي"
VERSION = "0.5.0"
BUILD = "DRYRUN-READY-2026-08-21-D"

NY_TZ = ZoneInfo("America/New_York")
KSA_TZ = ZoneInfo("Asia/Riyadh")
UTC_TZ = timezone.utc

# ==============================================================================
# Environment
# ==============================================================================

ALPACA_API_KEY = (
    os.getenv("ALPACA_API_KEY")
    or os.getenv("APCA_API_KEY_ID")
    or ""
)
ALPACA_SECRET_KEY = (
    os.getenv("ALPACA_SECRET_KEY")
    or os.getenv("APCA_API_SECRET_KEY")
    or ""
)
ALPACA_BASE_URL = os.getenv(
    "ALPACA_BASE_URL",
    os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
).rstrip("/")
ALPACA_DATA_URL = os.getenv(
    "ALPACA_DATA_URL",
    "https://data.alpaca.markets",
).rstrip("/")

TELEGRAM_BOT_TOKEN = (
    os.getenv("NEXT_DAY_TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or ""
)
TELEGRAM_CHAT_ID = (
    os.getenv("NEXT_DAY_TELEGRAM_CHAT_ID")
    or os.getenv("TELEGRAM_CHAT_ID")
    or ""
)

UPSTASH_REDIS_REST_URL = (
    os.getenv("UPSTASH_REDIS_REST_URL")
    or os.getenv("REDIS_REST_URL")
    or ""
)
UPSTASH_REDIS_REST_TOKEN = (
    os.getenv("UPSTASH_REDIS_REST_TOKEN")
    or os.getenv("REDIS_REST_TOKEN")
    or ""
)

GIST_ID = os.getenv("GIST_ID", "")
GIST_TOKEN = (
    os.getenv("GIST_TOKEN")
    or os.getenv("GITHUB_TOKEN")
    or ""
)
FLOAT_CACHE_FILENAME = os.getenv("FLOAT_CACHE_FILENAME", "float_cache.json")

PORT = int(os.getenv("PORT", "10000"))

# ==============================================================================
# Shared infrastructure
# ==============================================================================

SHARED_NEWS_HASH_KEY = "market_radar:news"
LTM_INCOMING_KEY = "live_trade_manager:incoming"
LTM_ACTIVE_KEY = "live_trade_manager:active_trades"

REDIS_PREFIX = "next_day_explosion_radar"
REDIS_KEYS = {
    "state": f"{REDIS_PREFIX}:state",
    "candidates": f"{REDIS_PREFIX}:candidates",
    "universe": f"{REDIS_PREFIX}:universe",
    "sent_state_alerts": f"{REDIS_PREFIX}:sent_state_alerts",
    "sent_entry_alerts": f"{REDIS_PREFIX}:sent_entry_alerts",
    "runtime_stats": f"{REDIS_PREFIX}:runtime_stats",
    "research_history": f"{REDIS_PREFIX}:research_history",
}

# ==============================================================================
# Tunable configuration
# ==============================================================================

PRICE_MIN = float(os.getenv("NDR_PRICE_MIN", "0.50"))
PRICE_MAX = float(os.getenv("NDR_PRICE_MAX", "25.00"))
MAX_FLOAT = float(os.getenv("NDR_MAX_FLOAT", "100000000"))

UNIVERSE_REFRESH_SEC = int(os.getenv("NDR_UNIVERSE_REFRESH_SEC", str(4 * 3600)))
FLOAT_REFRESH_SEC = int(os.getenv("NDR_FLOAT_REFRESH_SEC", str(6 * 3600)))
SHARED_NEWS_REFRESH_SEC = int(os.getenv("NDR_SHARED_NEWS_REFRESH_SEC", "60"))

DISCOVERY_INTERVAL_SEC = float(os.getenv("NDR_DISCOVERY_INTERVAL_SEC", "45"))
WATCH_INTERVAL_SEC = float(os.getenv("NDR_WATCH_INTERVAL_SEC", "10"))
ENTRY_INTERVAL_SEC = float(os.getenv("NDR_ENTRY_INTERVAL_SEC", "5"))
STATE_SAVE_INTERVAL_SEC = float(os.getenv("NDR_STATE_SAVE_INTERVAL_SEC", "30"))

SNAPSHOT_BATCH_SIZE = int(os.getenv("NDR_SNAPSHOT_BATCH_SIZE", "400"))
DISCOVERY_MIN_CHANGE_PCT = float(os.getenv("NDR_DISCOVERY_MIN_CHANGE_PCT", "2.0"))
DISCOVERY_MIN_PRICE = float(os.getenv("NDR_DISCOVERY_MIN_PRICE", str(PRICE_MIN)))
DISCOVERY_MAX_CANDIDATES_PER_CYCLE = int(
    os.getenv("NDR_DISCOVERY_MAX_CANDIDATES_PER_CYCLE", "60")
)
CANDIDATE_MAX_AGE_HOURS = float(os.getenv("NDR_CANDIDATE_MAX_AGE_HOURS", "36"))
MAX_HISTORY_POINTS = int(os.getenv("NDR_MAX_HISTORY_POINTS", "500"))

# Provisional research weights. Keep explainable; calibrate later.
GROUP_WEIGHTS = {
    "catalyst": 0.15,
    "participation": 0.20,
    "demand_quality": 0.25,
    "persistence": 0.15,
    "liquidity": 0.10,
    "historical_context": 0.15,
}

# State thresholds remain provisional until empirical calibration.
STATE_THRESHOLDS = {
    "awakening": float(os.getenv("NDR_STATE_AWAKENING", "58")),
    "accepted": float(os.getenv("NDR_STATE_ACCEPTED", "70")),
    "building": float(os.getenv("NDR_STATE_BUILDING", "80")),
    "breakout_ready": float(os.getenv("NDR_STATE_BREAKOUT_READY", "88")),
    "elite_continuation": float(os.getenv("NDR_STATE_ELITE", "93")),
}

ENTRY_MIN_OPPORTUNITY = float(os.getenv("NDR_ENTRY_MIN_OPPORTUNITY", "88"))
ENTRY_MAX_FAILURE_PRESSURE = float(
    os.getenv("NDR_ENTRY_MAX_FAILURE_PRESSURE", "35")
)
ENTRY_MAX_SPREAD_PCT = float(os.getenv("NDR_ENTRY_MAX_SPREAD_PCT", "2.0"))
ENTRY_MAX_STOP_PCT = float(os.getenv("NDR_ENTRY_MAX_STOP_PCT", "6.0"))
ENTRY_MIN_RR_T1 = float(os.getenv("NDR_ENTRY_MIN_RR_T1", "1.4"))
ALLOW_PREMARKET_ENTRY = os.getenv("NDR_ALLOW_PREMARKET_ENTRY", "false").lower() in {
    "1", "true", "yes", "y",
}
SEND_DISCOVERY_ALERTS = os.getenv("NDR_SEND_DISCOVERY_ALERTS", "true").lower() in {
    "1", "true", "yes", "y",
}
SEND_TO_LTM = os.getenv("NDR_SEND_TO_LTM", "true").lower() in {
    "1", "true", "yes", "y",
}

DRY_RUN = os.getenv("NDR_DRY_RUN", "false").lower() in {
    "1", "true", "yes", "y",
}
STARTUP_SELF_TEST = os.getenv("NDR_STARTUP_SELF_TEST", "true").lower() in {
    "1", "true", "yes", "y",
}
MAX_DEEP_EVAL_PER_MINUTE = int(os.getenv("NDR_MAX_DEEP_EVAL_PER_MINUTE", "30"))
API_FAILURE_BACKOFF_SEC = float(os.getenv("NDR_API_FAILURE_BACKOFF_SEC", "2"))
STALE_CANDIDATE_WARN_MINUTES = int(os.getenv("NDR_STALE_CANDIDATE_WARN_MINUTES", "10"))
RESEARCH_HISTORY_MAX = int(os.getenv("NDR_RESEARCH_HISTORY_MAX", "5000"))
RESEARCH_FINALIZE_AFTER_HOURS = float(os.getenv("NDR_RESEARCH_FINALIZE_AFTER_HOURS", "36"))
ENABLE_OVERNIGHT_BOATS = os.getenv("NDR_ENABLE_OVERNIGHT_BOATS", "true").lower() in {
    "1", "true", "yes", "y",
}
ENABLE_LIVE_ENTRY_ALERTS = os.getenv("NDR_ENABLE_LIVE_ENTRY_ALERTS", "false").lower() in {
    "1", "true", "yes", "y",
}
SESSION_HANDOFF_GRACE_MINUTES = int(os.getenv("NDR_SESSION_HANDOFF_GRACE_MINUTES", "20"))

# Historical feature lookbacks.
RVOL_LOOKBACK_DAYS = int(os.getenv("NDR_RVOL_LOOKBACK_DAYS", "10"))
RUNNER_LOOKBACK_DAYS = int(os.getenv("NDR_RUNNER_LOOKBACK_DAYS", "120"))
DETAIL_BAR_LOOKBACK_HOURS = int(os.getenv("NDR_DETAIL_BAR_LOOKBACK_HOURS", "8"))
DETAIL_QUOTE_LOOKBACK_MIN = int(os.getenv("NDR_DETAIL_QUOTE_LOOKBACK_MIN", "30"))
DETAIL_TRADE_LOOKBACK_MIN = int(os.getenv("NDR_DETAIL_TRADE_LOOKBACK_MIN", "30"))

# ==============================================================================
# Exclusions
# ==============================================================================

BAD_NAME_KEYWORDS = (
    "ETF", "ETN", "FUND", "TRUST", "INDEX", "WARRANT", "UNIT", "RIGHT",
    "ACQUISITION", "BLANK CHECK", "PREFERRED", "NOTE", "BOND", "SPAC",
    "DEPOSITARY", "REIT",
)
BAD_SYMBOL_SUFFIXES = ("Q",)
MANUAL_BLACKLIST = {
    x.strip().upper()
    for x in os.getenv("NDR_MANUAL_BLACKLIST", "").split(",")
    if x.strip()
}
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")

DILUTION_KEYWORDS = (
    "offering", "registered direct", "public offering", "private placement",
    "at-the-market", " at m ", "atm offering", "shelf", "424b5", "424b3",
    "warrant", "pre-funded", "convertible", "pipe financing", "equity line",
)
HARD_NEGATIVE_KEYWORDS = (
    "bankruptcy", "chapter 11", "delisting", "going concern",
    "sec investigation", "fraud", "default",
)

# ==============================================================================
# HTTP / Locks / Runtime
# ==============================================================================

http = requests.Session()
http.headers.update({"User-Agent": "Next-Day-Explosion-Radar/0.2"})

universe_lock = threading.RLock()
float_lock = threading.RLock()
news_lock = threading.RLock()
candidate_lock = threading.RLock()
sent_lock = threading.RLock()
stats_lock = threading.RLock()

universe: Dict[str, Dict[str, Any]] = {}
float_cache: Dict[str, Any] = {}
shared_news_cache: Dict[str, Dict[str, Any]] = {}
candidates: Dict[str, "Candidate"] = {}
sent_state_alerts: Dict[str, List[str]] = {}
sent_entry_alerts: Dict[str, Dict[str, Any]] = {}

runtime_stats: Dict[str, Any] = {
    "started_at": None,
    "last_loop": None,
    "last_discovery_scan": None,
    "last_watch_scan": None,
    "last_entry_scan": None,
    "last_universe_refresh": None,
    "last_float_refresh": None,
    "last_news_refresh": None,
    "last_state_save": None,
    "last_error": None,
    "session": None,
    "feed": None,
    "universe_count": 0,
    "float_count": 0,
    "shared_news_count": 0,
    "candidate_count": 0,
    "awakening_count": 0,
    "accepted_count": 0,
    "building_count": 0,
    "breakout_ready_count": 0,
    "alerts_sent": 0,
    "entries_sent": 0,
    "discovery_scans": 0,
    "deep_evaluations": 0,
    "deep_eval_rate_limited": 0,
    "alpaca_requests": 0,
    "alpaca_errors": 0,
    "redis_requests": 0,
    "redis_errors": 0,
    "self_test": {},
    "last_successful_market_data": None,
    "last_successful_redis": None,
    "rejections": {},
}

# Broad market breadth from current discovery cycle.
last_runtime_phase: Optional[SessionPhase] = None
last_runtime_phase_changed_at: Optional[str] = None

market_context: Dict[str, Any] = {
    "scanned": 0,
    "up_5": 0,
    "up_10": 0,
    "up_20": 0,
    "heat_score": 0.0,
    "updated_at": None,
}


# API-load protection. Slow/history features are cached because the Radar may
# run beside several other Alpaca consumers.
feature_cache_lock = threading.RLock()
feature_cache: Dict[str, Dict[str, Any]] = {}

SESSION_BARS_CACHE_TTL = float(os.getenv("NDR_SESSION_BARS_CACHE_TTL", "7"))
MICROSTRUCTURE_CACHE_TTL = float(os.getenv("NDR_MICROSTRUCTURE_CACHE_TTL", "15"))
RUNNER_CACHE_TTL = float(os.getenv("NDR_RUNNER_CACHE_TTL", str(6 * 3600)))
RVOL_CACHE_TTL = float(os.getenv("NDR_RVOL_CACHE_TTL", str(15 * 60)))

deep_eval_rate_lock = threading.RLock()
deep_eval_timestamps: List[float] = []


def allow_deep_evaluation() -> bool:
    now_ts = time.time()
    with deep_eval_rate_lock:
        cutoff = now_ts - 60.0
        while deep_eval_timestamps and deep_eval_timestamps[0] < cutoff:
            deep_eval_timestamps.pop(0)
        if len(deep_eval_timestamps) >= MAX_DEEP_EVAL_PER_MINUTE:
            with stats_lock:
                runtime_stats["deep_eval_rate_limited"] += 1
            return False
        deep_eval_timestamps.append(now_ts)
        return True


def bump_rejection(reason: str) -> None:
    with stats_lock:
        d = runtime_stats.setdefault("rejections", {})
        d[reason] = safe_int(d.get(reason)) + 1


def feature_cache_get(key: str, ttl: float) -> Any:
    with feature_cache_lock:
        item = feature_cache.get(key)
        if not item:
            return None
        if time.time() - safe_float(item.get("ts")) > ttl:
            feature_cache.pop(key, None)
            return None
        return item.get("value")


def feature_cache_set(key: str, value: Any) -> Any:
    with feature_cache_lock:
        feature_cache[key] = {"ts": time.time(), "value": value}
    return value

# ==============================================================================
# Enums / Dataclasses
# ==============================================================================

class SessionPhase(str, Enum):
    CLOSED = "CLOSED"
    AFTER_HOURS = "AFTER_HOURS"
    OVERNIGHT = "OVERNIGHT"
    PREMARKET_EARLY = "PREMARKET_EARLY"
    PREMARKET_LATE = "PREMARKET_LATE"
    REGULAR = "REGULAR"


class CandidateState(str, Enum):
    STEALTH = "STEALTH"
    AWAKENING = "AWAKENING"
    ACCEPTED = "ACCEPTED"
    BUILDING = "BUILDING"
    BREAKOUT_READY = "BREAKOUT_READY"
    ELITE_CONTINUATION = "ELITE_CONTINUATION"
    CROWDED = "CROWDED"
    EXHAUSTED = "EXHAUSTED"
    FAILED = "FAILED"


class StructuralRisk(str, Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


RISK_ORDER = {
    StructuralRisk.LOW: 0,
    StructuralRisk.MODERATE: 1,
    StructuralRisk.HIGH: 2,
    StructuralRisk.CRITICAL: 3,
}


@dataclass
class FeatureGroups:
    catalyst: float = 0.0
    participation: float = 0.0
    demand_quality: float = 0.0
    persistence: float = 0.0
    liquidity: float = 0.0
    historical_context: float = 0.0

    def normalized(self) -> "FeatureGroups":
        return FeatureGroups(**{
            k: clamp(v) for k, v in asdict(self).items()
        })

    def opportunity(self) -> float:
        n = self.normalized()
        total = sum(
            getattr(n, key) * weight
            for key, weight in GROUP_WEIGHTS.items()
        )
        return round(clamp(total), 2)


@dataclass
class RiskSnapshot:
    structural: StructuralRisk = StructuralRisk.LOW
    failure_pressure: float = 0.0
    reasons: List[str] = field(default_factory=list)


@dataclass
class Candidate:
    symbol: str
    created_at: str
    updated_at: str

    state: CandidateState = CandidateState.STEALTH
    previous_state: CandidateState = CandidateState.STEALTH
    phase: SessionPhase = SessionPhase.CLOSED

    opportunity_score: float = 0.0
    feature_groups: FeatureGroups = field(default_factory=FeatureGroups)
    structural_risk: StructuralRisk = StructuralRisk.LOW
    failure_pressure: float = 0.0

    price: float = 0.0
    reference_price: float = 0.0
    change_pct: float = 0.0
    high: float = 0.0
    low: float = 0.0
    vwap: float = 0.0
    resistance: float = 0.0
    spread_pct: float = 99.0

    float_shares: Optional[float] = None
    session_volume: float = 0.0
    float_rotation: float = 0.0
    rvol: float = 0.0
    rvol_percentile_proxy: float = 0.0
    volume_acceleration: float = 0.0
    trade_continuity: float = 0.0

    demand_efficiency: float = 0.0
    price_acceptance: float = 0.0
    pullback_quality: float = 0.0
    reclaim_structure: float = 0.0

    cross_session_persistence: float = 0.0
    trajectory_score: float = 0.0
    spread_quality: float = 0.0
    liquidity_evolution: float = 0.0

    runner_personality: float = 0.0
    runner_fatigue: float = 0.0
    extension_risk: float = 0.0
    evidence_convergence: float = 0.0

    catalyst_strength: float = 0.0
    unpriced_catalyst: float = 0.0
    catalyst_market_response: float = 0.0
    catalyst_headline: str = ""
    catalyst_category: str = ""
    catalyst_ts: int = 0

    # Live research / calibration fields.
    discovery_price: float = 0.0
    discovery_time: str = ""
    max_price_after_discovery: float = 0.0
    min_price_after_discovery: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    time_to_mfe_minutes: float = 0.0
    first_awakening_at: str = ""
    first_accepted_at: str = ""
    first_building_at: str = ""
    first_breakout_ready_at: str = ""
    first_elite_at: str = ""
    research_finalized: bool = False

    evidence: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    risk_reasons: List[str] = field(default_factory=list)

    history: List[Dict[str, Any]] = field(default_factory=list)

    entry_eligible: bool = False
    entry_confirmed: bool = False
    entry_block_reasons: List[str] = field(default_factory=list)
    last_entry_plan: Optional[Dict[str, Any]] = None


# ==============================================================================
# Generic helpers
# ==============================================================================

def now_utc() -> datetime:
    return datetime.now(UTC_TZ)


def now_ny() -> datetime:
    return datetime.now(NY_TZ)


def now_ksa() -> datetime:
    return datetime.now(KSA_TZ)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or now_utc()).isoformat()


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


def clamp(x: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, safe_float(x)))


def pct_change(current: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return ((current / reference) - 1.0) * 100.0


def chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC_TZ)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC_TZ)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def percentile_proxy(value: float, baseline: Sequence[float]) -> float:
    clean = [safe_float(x) for x in baseline if safe_float(x) >= 0]
    if not clean:
        return 50.0
    less_equal = sum(1 for x in clean if x <= value)
    return clamp(100.0 * less_equal / len(clean))


def mean(values: Sequence[float]) -> float:
    vals = [safe_float(x) for x in values]
    return sum(vals) / len(vals) if vals else 0.0


def clean_symbol(symbol: str) -> bool:
    s = str(symbol or "").strip().upper()
    if not SYMBOL_RE.match(s):
        return False
    if s in MANUAL_BLACKLIST:
        return False
    if any(s.endswith(suffix) for suffix in BAD_SYMBOL_SUFFIXES):
        return False
    return True


def current_market_session(at: Optional[datetime] = None) -> SessionPhase:
    dt = at or now_ny()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY_TZ)
    else:
        dt = dt.astimezone(NY_TZ)

    wd = dt.weekday()  # Mon=0 ... Sun=6
    t = dt.time()

    # Saturday always closed.
    if wd == 5:
        return SessionPhase.CLOSED

    # Sunday opens for overnight at 20:00 ET.
    if wd == 6:
        return SessionPhase.OVERNIGHT if t >= dtime(20, 0) else SessionPhase.CLOSED

    # Friday closes after 20:00 ET.
    if wd == 4 and t >= dtime(20, 0):
        return SessionPhase.CLOSED

    if dtime(9, 30) <= t < dtime(16, 0):
        return SessionPhase.REGULAR
    if dtime(16, 0) <= t < dtime(20, 0):
        return SessionPhase.AFTER_HOURS
    if t >= dtime(20, 0) or t < dtime(4, 0):
        return SessionPhase.OVERNIGHT
    if dtime(4, 0) <= t < dtime(8, 0):
        return SessionPhase.PREMARKET_EARLY
    if dtime(8, 0) <= t < dtime(9, 30):
        return SessionPhase.PREMARKET_LATE
    return SessionPhase.CLOSED


def feed_for_phase(phase: SessionPhase) -> str:
    if phase == SessionPhase.OVERNIGHT:
        return "boats" if ENABLE_OVERNIGHT_BOATS else "sip"
    return "sip"


def session_start_et(dt: Optional[datetime] = None, phase: Optional[SessionPhase] = None) -> datetime:
    dt = (dt or now_ny()).astimezone(NY_TZ)
    phase = phase or current_market_session(dt)

    if phase == SessionPhase.AFTER_HOURS:
        return dt.replace(hour=16, minute=0, second=0, microsecond=0)
    if phase == SessionPhase.OVERNIGHT:
        if dt.time() >= dtime(20, 0):
            return dt.replace(hour=20, minute=0, second=0, microsecond=0)
        return (dt - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    if phase in (SessionPhase.PREMARKET_EARLY, SessionPhase.PREMARKET_LATE):
        return dt.replace(hour=4, minute=0, second=0, microsecond=0)
    if phase == SessionPhase.REGULAR:
        return dt.replace(hour=9, minute=30, second=0, microsecond=0)
    return dt


def bar_time(bar: Dict[str, Any]) -> Optional[datetime]:
    return parse_ts(bar.get("t") or bar.get("timestamp"))


def bar_close(bar: Dict[str, Any]) -> float:
    return safe_float(bar.get("c", bar.get("close")))


def bar_high(bar: Dict[str, Any]) -> float:
    return safe_float(bar.get("h", bar.get("high")))


def bar_low(bar: Dict[str, Any]) -> float:
    return safe_float(bar.get("l", bar.get("low")))


def bar_volume(bar: Dict[str, Any]) -> float:
    return safe_float(bar.get("v", bar.get("volume")))


def bar_vwap(bar: Dict[str, Any]) -> float:
    return safe_float(bar.get("vw", bar.get("vwap")))


# ==============================================================================
# Alpaca HTTP
# ==============================================================================

def alpaca_headers() -> Dict[str, str]:
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


def alpaca_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Any:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    with stats_lock:
        runtime_stats["alpaca_requests"] += 1
    try:
        r = http.get(url, headers=alpaca_headers(), params=params, timeout=timeout)
        if r.status_code == 429:
            with stats_lock:
                runtime_stats["alpaca_errors"] += 1
                runtime_stats["last_error"] = "Alpaca 429 rate limit"
            time.sleep(API_FAILURE_BACKOFF_SEC)
            return None
        if r.status_code != 200:
            with stats_lock:
                runtime_stats["alpaca_errors"] += 1
                runtime_stats["last_error"] = f"Alpaca {r.status_code}: {r.text[:250]}"
            return None
        with stats_lock:
            runtime_stats["last_successful_market_data"] = iso()
        return r.json()
    except Exception as e:
        with stats_lock:
            runtime_stats["alpaca_errors"] += 1
            runtime_stats["last_error"] = f"Alpaca exception: {e}"
        return None


def get_assets() -> List[Dict[str, Any]]:
    data = alpaca_get(
        f"{ALPACA_BASE_URL}/v2/assets",
        params={"status": "active", "asset_class": "us_equity"},
        timeout=30,
    )
    if isinstance(data, list):
        return data
    # requests .json may return list but type annotation above expects dict.
    if data is None:
        try:
            r = http.get(
                f"{ALPACA_BASE_URL}/v2/assets",
                headers=alpaca_headers(),
                params={"status": "active", "asset_class": "us_equity"},
                timeout=30,
            )
            if r.status_code == 200 and isinstance(r.json(), list):
                return r.json()
        except Exception:
            pass
    return []


def get_snapshots(symbols: Sequence[str], feed: str) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    data = alpaca_get(
        f"{ALPACA_DATA_URL}/v2/stocks/snapshots",
        params={"symbols": ",".join(symbols), "feed": feed},
        timeout=30,
    )
    if not isinstance(data, dict):
        return {}
    # API may return direct symbol map.
    return data.get("snapshots", data)


def get_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    feed: str,
    timeframe: str = "1Min",
    limit: int = 10000,
) -> List[Dict[str, Any]]:
    params = {
        "start": start.astimezone(UTC_TZ).isoformat().replace("+00:00", "Z"),
        "end": end.astimezone(UTC_TZ).isoformat().replace("+00:00", "Z"),
        "timeframe": timeframe,
        "feed": feed,
        "limit": limit,
        "adjustment": "raw",
        "sort": "asc",
    }
    data = alpaca_get(
        f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars",
        params=params,
        timeout=30,
    )
    if not data:
        return []
    bars = data.get("bars", [])
    return bars if isinstance(bars, list) else []


def get_daily_bars(symbol: str, days: int = RUNNER_LOOKBACK_DAYS) -> List[Dict[str, Any]]:
    end = now_utc()
    start = end - timedelta(days=max(days * 2, 180))
    data = alpaca_get(
        f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars",
        params={
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "timeframe": "1Day",
            "feed": "sip",
            "limit": max(days + 30, 200),
            "adjustment": "split",
            "sort": "asc",
        },
        timeout=30,
    )
    if not data:
        return []
    bars = data.get("bars", [])
    return bars[-days:] if isinstance(bars, list) else []


def get_quotes(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    feed: str,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    data = alpaca_get(
        f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/quotes",
        params={
            "start": start.astimezone(UTC_TZ).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(UTC_TZ).isoformat().replace("+00:00", "Z"),
            "feed": feed,
            "limit": limit,
            "sort": "asc",
        },
        timeout=20,
    )
    if not data:
        return []
    q = data.get("quotes", [])
    return q if isinstance(q, list) else []


def get_trades(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    feed: str,
    limit: int = 10000,
) -> List[Dict[str, Any]]:
    data = alpaca_get(
        f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/trades",
        params={
            "start": start.astimezone(UTC_TZ).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(UTC_TZ).isoformat().replace("+00:00", "Z"),
            "feed": feed,
            "limit": limit,
            "sort": "asc",
        },
        timeout=20,
    )
    if not data:
        return []
    trades = data.get("trades", [])
    return trades if isinstance(trades, list) else []


# ==============================================================================
# Redis / shared news
# ==============================================================================

def redis_cmd(*args: Any) -> Any:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    with stats_lock:
        runtime_stats["redis_requests"] += 1
    try:
        r = http.post(
            UPSTASH_REDIS_REST_URL.rstrip("/"),
            headers={
                "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
                "Content-Type": "application/json",
            },
            json=list(args),
            timeout=15,
        )
        if r.status_code != 200:
            with stats_lock:
                runtime_stats["redis_errors"] += 1
                runtime_stats["last_error"] = f"Redis HTTP {r.status_code}"
            return None
        with stats_lock:
            runtime_stats["last_successful_redis"] = iso()
        return r.json().get("result")
    except Exception as e:
        with stats_lock:
            runtime_stats["redis_errors"] += 1
            runtime_stats["last_error"] = f"Redis error: {e}"
        return None


def redis_get_json(key: str, default: Any = None) -> Any:
    raw = redis_cmd("GET", key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def redis_set_json(key: str, value: Any, ex_seconds: Optional[int] = None) -> None:
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if ex_seconds:
        redis_cmd("SET", key, raw, "EX", int(ex_seconds))
    else:
        redis_cmd("SET", key, raw)


def redis_hget_json(key: str, field: str) -> Optional[Dict[str, Any]]:
    raw = redis_cmd("HGET", key, field)
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def redis_hgetall_json(key: str) -> Dict[str, Dict[str, Any]]:
    raw = redis_cmd("HGETALL", key)
    if not isinstance(raw, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(raw) - 1, 2):
        field_name = str(raw[i]).upper()
        try:
            val = json.loads(raw[i + 1])
            if isinstance(val, dict):
                out[field_name] = val
        except Exception:
            continue
    return out


def refresh_shared_news_cache() -> None:
    global shared_news_cache
    data = redis_hgetall_json(SHARED_NEWS_HASH_KEY)
    if data:
        with news_lock:
            shared_news_cache = data
        with stats_lock:
            runtime_stats["shared_news_count"] = len(data)
            runtime_stats["last_news_refresh"] = iso()


def get_shared_news(symbol: str) -> Optional[Dict[str, Any]]:
    s = symbol.upper()
    with news_lock:
        cached = shared_news_cache.get(s)
    if cached:
        return cached
    return redis_hget_json(SHARED_NEWS_HASH_KEY, s)


# ==============================================================================
# Float cache
# ==============================================================================

def load_float_cache() -> None:
    global float_cache
    if not GIST_ID:
        return
    try:
        headers = {}
        if GIST_TOKEN:
            headers["Authorization"] = f"token {GIST_TOKEN}"
        r = http.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=headers,
            timeout=30,
        )
        if r.status_code != 200:
            return
        files = r.json().get("files", {})
        f = files.get(FLOAT_CACHE_FILENAME) or files.get("float_cache.json")
        if not f:
            return
        content = f.get("content")
        if content is None and f.get("raw_url"):
            rr = http.get(f["raw_url"], headers=headers, timeout=30)
            if rr.status_code == 200:
                content = rr.text
        if not content:
            return
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            with float_lock:
                float_cache = parsed
            with stats_lock:
                runtime_stats["float_count"] = len(parsed)
                runtime_stats["last_float_refresh"] = iso()
    except Exception as e:
        with stats_lock:
            runtime_stats["last_error"] = f"Float load: {e}"


def get_float(symbol: str) -> Optional[float]:
    with float_lock:
        item = float_cache.get(symbol.upper())
    if item is None:
        return None
    try:
        if isinstance(item, dict):
            for key in ("float", "floatShares", "shareFloat", "shares_float"):
                if item.get(key) is not None:
                    v = safe_float(item[key], -1)
                    return v if v > 0 else None
        v = safe_float(item, -1)
        return v if v > 0 else None
    except Exception:
        return None


# ==============================================================================
# Telegram
# ==============================================================================

def telegram_send(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram disabled]", text[:500], flush=True)
        return False
    try:
        r = http.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False

def state_emoji(state: CandidateState) -> str:
    return {
        CandidateState.STEALTH: "🕵️",
        CandidateState.AWAKENING: "👀",
        CandidateState.ACCEPTED: "🔥",
        CandidateState.BUILDING: "🚀",
        CandidateState.BREAKOUT_READY: "🔥",
        CandidateState.ELITE_CONTINUATION: "💎",
        CandidateState.CROWDED: "⚠️",
        CandidateState.EXHAUSTED: "🧯",
        CandidateState.FAILED: "❌",
    }.get(state, "📡")


def structural_risk_ar(risk: StructuralRisk) -> str:
    return {
        StructuralRisk.LOW: "منخفضة",
        StructuralRisk.MODERATE: "متوسطة",
        StructuralRisk.HIGH: "مرتفعة",
        StructuralRisk.CRITICAL: "حرجة",
    }.get(risk, "غير معروفة")


def discovery_alert(c: Candidate) -> str:
    risk_ar = structural_risk_ar(c.structural_risk)

    ev = " • ".join(c.evidence[:4]) if c.evidence else "الأدلة الإيجابية تتجمع"
    warns = " • ".join(c.warnings[:3]) if c.warnings else "لا توجد تحذيرات بارزة"

    return (
        f"🔥 <b>جاهز للاختراق — {BOT_NAME_AR}</b>\n\n"
        f"<b>{html.escape(c.symbol)}</b>\n"
        f"💵 السعر: <b>${c.price:.4f}</b> ({c.change_pct:+.1f}%)\n"
        f"🧠 قوة الفرصة: <b>{c.opportunity_score:.1f}/100</b>\n"
        f"🏗️ المخاطر الهيكلية: <b>{risk_ar}</b>\n"
        f"⚠️ ضغط الفشل: <b>{c.failure_pressure:.1f}/100</b>\n"
        f"📦 الفلوت: {format_float(c.float_shares)} | دوران الفلوت: {c.float_rotation:.2f}x\n"
        f"📈 RVOL: {c.rvol:.2f}x | تسارع الحجم: {c.volume_acceleration:.2f}x\n"
        f"💧 السبريد: {c.spread_pct:.2f}%\n"
        f"⚡ كفاءة الطلب: {c.demand_efficiency:.0f}/100\n"
        f"✅ القبول السعري: {c.price_acceptance:.0f}/100\n\n"
        f"📰 {html.escape(c.catalyst_headline[:180] or 'لا يوجد محفز إخباري مركزي حديث')}\n\n"
        f"✅ {html.escape(ev)}\n"
        f"⚠️ {html.escape(warns)}\n\n"
        f"🔥 <b>الحالة: جاهز للاختراق</b>\n"
        f"ℹ️ <b>مراقبة نهائية — ليست إشارة دخول بعد.</b>"
    )


def format_float(value: Optional[float]) -> str:
    if not value or value <= 0:
        return "N/A"
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value/1_000:.1f}K"
    return f"{value:.0f}"


# ==============================================================================
# Session handoff
# ==============================================================================

def handle_session_transition(new_phase: SessionPhase) -> None:
    global last_runtime_phase, last_runtime_phase_changed_at

    old_phase = last_runtime_phase
    if old_phase == new_phase:
        return

    last_runtime_phase = new_phase
    last_runtime_phase_changed_at = iso()

    with candidate_lock:
        for c in candidates.values():
            c.previous_state = c.state
            c.phase = new_phase
            c.session_volume = 0.0
            c.high = 0.0
            c.low = 0.0
            c.vwap = 0.0
            c.rvol = 0.0
            c.rvol_percentile_proxy = 0.0
            c.volume_acceleration = 0.0
            c.trade_continuity = 0.0
            c.spread_pct = 99.0

            c.history.append({
                "ts": iso(),
                "event": "session_transition",
                "from": old_phase.value if old_phase else None,
                "to": new_phase.value,
                "state": c.state.value,
            })
            if len(c.history) > MAX_HISTORY_POINTS:
                c.history = c.history[-MAX_HISTORY_POINTS:]

    print(
        f"[SESSION] {old_phase.value if old_phase else 'NONE'} -> {new_phase.value}",
        flush=True,
    )


# ==============================================================================
# Universe
# ==============================================================================

def asset_allowed(asset: Dict[str, Any]) -> bool:
    symbol = str(asset.get("symbol") or "").upper()
    name = str(asset.get("name") or "").upper()

    if not clean_symbol(symbol):
        return False
    if not asset.get("tradable", False):
        return False
    if str(asset.get("status", "")).lower() != "active":
        return False
    if any(k in name for k in BAD_NAME_KEYWORDS):
        return False
    return True


def rebuild_universe() -> None:
    global universe
    assets = get_assets()
    if not assets:
        return
    new_universe = {}
    for a in assets:
        if not asset_allowed(a):
            continue
        symbol = str(a.get("symbol")).upper()
        new_universe[symbol] = {
            "symbol": symbol,
            "name": a.get("name", ""),
            "exchange": a.get("exchange", ""),
            "tradable": bool(a.get("tradable")),
            "shortable": bool(a.get("shortable")),
        }
    if new_universe:
        with universe_lock:
            universe = new_universe
        with stats_lock:
            runtime_stats["universe_count"] = len(new_universe)
            runtime_stats["last_universe_refresh"] = iso()
        redis_set_json(REDIS_KEYS["universe"], new_universe)


# ==============================================================================
# Snapshot interpretation / fast discovery
# ==============================================================================

def snapshot_price(snapshot: Dict[str, Any]) -> float:
    lt = snapshot.get("latestTrade") or {}
    mb = snapshot.get("minuteBar") or {}
    db = snapshot.get("dailyBar") or {}
    return (
        safe_float(lt.get("p"))
        or safe_float(mb.get("c"))
        or safe_float(db.get("c"))
    )


def snapshot_reference_close(snapshot: Dict[str, Any], phase: SessionPhase) -> float:
    db = snapshot.get("dailyBar") or {}
    pdb = snapshot.get("prevDailyBar") or {}
    # In extended sessions the latest regular daily bar is normally the desired
    # reference. Fall back to previous daily close when unavailable.
    return safe_float(db.get("c")) or safe_float(pdb.get("c"))


def fast_discovery_candidates(phase: SessionPhase) -> List[Tuple[str, float]]:
    feed = feed_for_phase(phase)
    with universe_lock:
        symbols = list(universe.keys())

    scores: List[Tuple[str, float]] = []
    breadth_scanned = up5 = up10 = up20 = 0

    for batch in chunks(symbols, SNAPSHOT_BATCH_SIZE):
        snaps = get_snapshots(batch, feed)
        if not snaps:
            continue

        for symbol, snap in snaps.items():
            if not isinstance(snap, dict):
                continue
            price = snapshot_price(snap)
            if not (DISCOVERY_MIN_PRICE <= price <= PRICE_MAX):
                continue

            ref = snapshot_reference_close(snap, phase)
            change = pct_change(price, ref) if ref > 0 else 0.0
            breadth_scanned += 1
            if change >= 5:
                up5 += 1
            if change >= 10:
                up10 += 1
            if change >= 20:
                up20 += 1

            news = get_shared_news(symbol)
            analysis = (news or {}).get("analysis") or {}
            fresh_positive = bool(
                analysis.get("positive")
                or analysis.get("major_catalyst")
            ) and news_is_fresh(news, max_hours=18)

            if change >= DISCOVERY_MIN_CHANGE_PCT or fresh_positive:
                # Rank only for scan prioritization; not the Opportunity score.
                rank = max(change, 0.0)
                if fresh_positive:
                    rank += 12
                f = get_float(symbol)
                if f and f <= 10_000_000:
                    rank += 4
                scores.append((symbol, rank))

        time.sleep(0.05)

    heat = 0.0
    if breadth_scanned:
        heat = clamp(
            (up5 / breadth_scanned) * 500
            + (up10 / breadth_scanned) * 800
            + (up20 / breadth_scanned) * 1200
        )

    market_context.update({
        "scanned": breadth_scanned,
        "up_5": up5,
        "up_10": up10,
        "up_20": up20,
        "heat_score": heat,
        "updated_at": iso(),
    })

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:DISCOVERY_MAX_CANDIDATES_PER_CYCLE]


# ==============================================================================
# News features
# ==============================================================================

def news_is_fresh(news: Optional[Dict[str, Any]], max_hours: float = 18.0) -> bool:
    if not news:
        return False
    analysis = news.get("analysis") or {}
    ts = safe_int(analysis.get("datetime"))
    if ts <= 0:
        ts = safe_int(news.get("fetched_at"))
    if ts <= 0:
        return False
    age_hours = (time.time() - ts) / 3600.0
    return -1 <= age_hours <= max_hours


def news_text(news: Optional[Dict[str, Any]]) -> str:
    if not news:
        return ""
    parts = []
    analysis = news.get("analysis") or {}
    parts.append(str(analysis.get("headline") or ""))
    for a in news.get("articles") or []:
        if isinstance(a, dict):
            parts.append(str(a.get("headline") or ""))
            parts.append(str(a.get("summary") or ""))
    return " ".join(parts).lower()


def score_catalyst(
    news: Optional[Dict[str, Any]],
    change_pct_value: float,
    volume_accel: float,
    acceptance: float,
) -> Tuple[float, float, float, str, str, int, List[str]]:
    if not news:
        return 15.0, 5.0, 20.0, "", "", 0, []

    analysis = news.get("analysis") or {}
    headline = str(analysis.get("headline") or "")
    category = str(analysis.get("category") or "neutral")
    ts = safe_int(analysis.get("datetime"))
    major = bool(analysis.get("major_catalyst"))
    positive = bool(analysis.get("positive"))
    serious_negative = bool(analysis.get("serious_negative"))
    central_score = safe_float(analysis.get("score"))

    if serious_negative:
        strength = 0.0
    elif major:
        strength = clamp(82 + min(18, max(0, central_score)))
    elif positive:
        strength = clamp(58 + min(25, max(0, central_score)))
    else:
        strength = clamp(30 + central_score)

    age_hours = 999.0
    if ts > 0:
        age_hours = max(0.0, (time.time() - ts) / 3600.0)

    # Freshness / unpriced potential: decays by age and by already-extreme repricing.
    if age_hours <= 1:
        freshness = 95
    elif age_hours <= 3:
        freshness = 85
    elif age_hours <= 6:
        freshness = 72
    elif age_hours <= 12:
        freshness = 58
    elif age_hours <= 24:
        freshness = 40
    else:
        freshness = 20

    repricing_penalty = clamp(max(0.0, change_pct_value - 50.0) * 0.8, 0, 45)
    unpriced = clamp(freshness - repricing_penalty)

    # Market response is deliberately separate from fundamental quality.
    response = clamp(
        30
        + min(30, max(0, change_pct_value) * 0.8)
        + min(20, max(0, volume_accel - 1) * 10)
        + (acceptance - 50) * 0.25
    )

    flags = []
    text = news_text(news)
    if any(k in text for k in DILUTION_KEYWORDS):
        flags.append("dilution_language")
    if any(k in text for k in HARD_NEGATIVE_KEYWORDS):
        flags.append("hard_negative_language")

    return strength, unpriced, response, headline, category, ts, flags


# ==============================================================================
# Core feature calculations
# ==============================================================================

def session_bars(symbol: str, phase: SessionPhase, now_et: Optional[datetime] = None) -> List[Dict[str, Any]]:
    now_et = now_et or now_ny()
    cache_key = f"bars:{symbol}:{phase.value}"
    cached = feature_cache_get(cache_key, SESSION_BARS_CACHE_TTL)
    if isinstance(cached, list):
        return cached
    start = session_start_et(now_et, phase)
    bars = get_bars(symbol, start, now_et, feed=feed_for_phase(phase), timeframe="1Min")
    return feature_cache_set(cache_key, bars)


def volume_acceleration_score(bars: List[Dict[str, Any]]) -> Tuple[float, float]:
    vols = [bar_volume(b) for b in bars if bar_volume(b) >= 0]
    if len(vols) < 8:
        return 40.0, 1.0

    last1 = vols[-1]
    prev7 = mean(vols[-8:-1]) or 1.0
    last3 = mean(vols[-3:])
    prior5 = mean(vols[-8:-3]) or 1.0

    ratio1 = last1 / prev7
    ratio3 = last3 / prior5
    accel = max(ratio1, ratio3)

    score = clamp(
        35
        + (ratio3 - 1) * 25
        + (ratio1 - 1) * 15
    )
    return score, accel


def compute_vwap(bars: List[Dict[str, Any]]) -> float:
    pv = vv = 0.0
    for b in bars:
        v = bar_volume(b)
        if v <= 0:
            continue
        p = bar_vwap(b)
        if p <= 0:
            p = (bar_high(b) + bar_low(b) + bar_close(b)) / 3.0
        pv += p * v
        vv += v
    return pv / vv if vv > 0 else 0.0


def price_acceptance_score(bars: List[Dict[str, Any]], reference_price: float) -> Tuple[float, Dict[str, float]]:
    if len(bars) < 3:
        return 40.0, {}

    closes = [bar_close(b) for b in bars if bar_close(b) > 0]
    highs = [bar_high(b) for b in bars if bar_high(b) > 0]
    lows = [bar_low(b) for b in bars if bar_low(b) > 0]
    if not closes or not highs or not lows:
        return 40.0, {}

    cur = closes[-1]
    hi = max(highs)
    lo = min(lows)
    range_pos = (cur - lo) / max(hi - lo, 1e-9)

    peak_gain = pct_change(hi, reference_price) if reference_price > 0 else 0
    current_gain = pct_change(cur, reference_price) if reference_price > 0 else 0
    retention = current_gain / peak_gain if peak_gain > 0 else range_pos
    retention = max(0.0, min(1.2, retention))

    upper_threshold = lo + 0.75 * (hi - lo)
    time_upper = sum(1 for c in closes if c >= upper_threshold) / len(closes)

    score = clamp(
        15
        + 35 * range_pos
        + 30 * min(retention, 1)
        + 20 * time_upper
    )
    return score, {
        "range_position": range_pos,
        "peak_retention": retention,
        "time_upper_quartile": time_upper,
    }


def pullback_quality_score(bars: List[Dict[str, Any]]) -> Tuple[float, Dict[str, float]]:
    if len(bars) < 10:
        return 45.0, {}

    recent = bars[-20:]
    closes = [bar_close(b) for b in recent]
    lows = [bar_low(b) for b in recent]
    vols = [bar_volume(b) for b in recent]

    # Higher-low persistence on 4 rolling blocks.
    block = max(2, len(recent) // 4)
    block_lows = []
    for i in range(0, len(recent), block):
        part = lows[i:i + block]
        if part:
            block_lows.append(min(part))
    hl_hits = sum(
        1 for i in range(1, len(block_lows))
        if block_lows[i] > block_lows[i - 1]
    )
    hl_ratio = hl_hits / max(1, len(block_lows) - 1)

    # Up/down volume asymmetry.
    up_vol = down_vol = 0.0
    for i in range(1, len(recent)):
        if closes[i] >= closes[i - 1]:
            up_vol += vols[i]
        else:
            down_vol += vols[i]
    asym = up_vol / max(down_vol, 1.0)

    # Recovery from recent drawdown.
    prior_high = max(closes[:-1])
    low_after = min(closes[max(0, closes.index(prior_high)):]) if prior_high in closes else min(closes)
    cur = closes[-1]
    recovery = 0.5
    if prior_high > low_after:
        recovery = (cur - low_after) / (prior_high - low_after)
    recovery = max(0.0, min(1.2, recovery))

    score = clamp(
        25
        + 30 * hl_ratio
        + 25 * min(1.0, recovery)
        + 20 * min(1.5, asym) / 1.5
    )
    return score, {
        "higher_low_ratio": hl_ratio,
        "up_down_volume_ratio": asym,
        "recovery_ratio": recovery,
    }


def reclaim_structure_score(bars: List[Dict[str, Any]], vwap: float) -> Tuple[float, float]:
    if len(bars) < 8:
        return 40.0, 0.0
    closes = [bar_close(b) for b in bars]
    highs = [bar_high(b) for b in bars]
    current = closes[-1]
    resistance = max(highs[:-2]) if len(highs) > 2 else max(highs)
    above_vwap = current >= vwap if vwap > 0 else False
    dist = pct_change(current, resistance) if resistance > 0 else 0.0

    recent_closes = closes[-3:]
    closes_above = sum(1 for c in recent_closes if resistance > 0 and c >= resistance)
    score = 35.0
    if above_vwap:
        score += 20
    if resistance > 0 and current >= resistance:
        score += 25
    elif resistance > 0 and current >= resistance * 0.99:
        score += 12
    score += closes_above * 6
    return clamp(score), resistance


def demand_efficiency_score(
    bars: List[Dict[str, Any]],
    float_shares: Optional[float],
    reference_price: float,
) -> Tuple[float, Dict[str, float]]:
    if len(bars) < 5:
        return 40.0, {}

    first = bar_close(bars[0]) or reference_price
    cur = bar_close(bars[-1])
    hi = max(bar_high(b) for b in bars)
    total_vol = sum(bar_volume(b) for b in bars)

    progress = max(0.0, pct_change(cur, first))
    peak_progress = max(0.0, pct_change(hi, first))
    retention = progress / peak_progress if peak_progress > 0 else 0.5

    rotation = total_vol / float_shares if float_shares and float_shares > 0 else 0.0

    # Marginal efficiency: recent price progress relative to recent volume share.
    half = max(2, len(bars) // 2)
    early = bars[:-half] if len(bars) > half else bars[:half]
    recent = bars[-half:]
    early_price = pct_change(bar_close(early[-1]), bar_close(early[0])) if len(early) >= 2 else 0
    recent_price = pct_change(bar_close(recent[-1]), bar_close(recent[0])) if len(recent) >= 2 else 0
    early_vol = sum(bar_volume(b) for b in early) or 1.0
    recent_vol = sum(bar_volume(b) for b in recent) or 1.0
    marginal_ratio = (recent_price / max(recent_vol, 1.0)) / (
        abs(early_price) / max(early_vol, 1.0) + 1e-9
    )
    marginal_ratio = max(-5.0, min(5.0, marginal_ratio))

    # Reward useful price progress and retention; penalize extreme turnover that
    # is no longer producing price progress.
    score = 35.0
    score += min(25, progress * 1.2)
    score += 25 * max(0.0, min(1.0, retention))
    if recent_price > 0:
        score += min(15, recent_price * 1.5)
    elif recent_price < -2:
        score -= min(25, abs(recent_price) * 2)

    if rotation > 5 and progress < 8:
        score -= min(30, (rotation - 5) * 3)
    if rotation > 10 and retention < 0.5:
        score -= 20

    return clamp(score), {
        "price_progress_pct": progress,
        "peak_progress_pct": peak_progress,
        "retention": retention,
        "rotation": rotation,
        "marginal_efficiency_proxy": marginal_ratio,
    }


def spread_metrics(quotes: List[Dict[str, Any]], current_price: float) -> Tuple[float, float, float]:
    spreads = []
    for q in quotes:
        bid = safe_float(q.get("bp"))
        ask = safe_float(q.get("ap"))
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        if mid > 0 and ask >= bid:
            spreads.append((ask - bid) / mid * 100.0)

    if not spreads:
        return 99.0, 0.0, 0.0

    current = median(spreads[-min(20, len(spreads)):])
    med = median(spreads)

    half = max(1, len(spreads) // 2)
    old = median(spreads[:half])
    new = median(spreads[-half:])
    compression = (old - new) / old if old > 0 else 0.0

    spread_quality = clamp(100 - current * 25)
    liquidity_evolution = clamp(50 + compression * 80)

    return current, spread_quality, liquidity_evolution


def trade_continuity_score(trades: List[Dict[str, Any]], start: datetime, end: datetime) -> float:
    elapsed_min = max(1.0, (end - start).total_seconds() / 60.0)
    if not trades:
        return 10.0
    active_minutes = set()
    for t in trades:
        dt = parse_ts(t.get("t"))
        if dt:
            active_minutes.add(dt.replace(second=0, microsecond=0))
    active_ratio = len(active_minutes) / elapsed_min
    trades_per_min = len(trades) / elapsed_min
    return clamp(
        25
        + min(40, active_ratio * 50)
        + min(35, math.log1p(trades_per_min) * 10)
    )


def historical_session_rvol(
    symbol: str,
    phase: SessionPhase,
    current_volume: float,
    elapsed_minutes: int,
) -> Tuple[float, float]:
    """
    Same-session / same-elapsed-time baseline.
    Computed only for deep candidates to keep API load reasonable.
    """
    if current_volume <= 0 or elapsed_minutes <= 0:
        return 0.0, 50.0

    feed = feed_for_phase(phase)
    now_et = now_ny()
    daily_totals = []

    # Query one calendar window, then bucket by ET date/session.
    end = now_et - timedelta(days=1)
    start = end - timedelta(days=RVOL_LOOKBACK_DAYS * 2 + 5)

    bars = get_bars(symbol, start, end, feed=feed, timeframe="1Min", limit=10000)
    by_day: Dict[str, List[Dict[str, Any]]] = {}

    for b in bars:
        dt = bar_time(b)
        if not dt:
            continue
        et = dt.astimezone(NY_TZ)
        key = et.date().isoformat()
        # Keep bars from the same logical session only.
        if current_market_session(et) != phase:
            continue
        s0 = session_start_et(et, phase)
        mins = int((et - s0).total_seconds() / 60)
        if 0 <= mins <= elapsed_minutes:
            by_day.setdefault(key, []).append(b)

    for day_bars in by_day.values():
        total = sum(bar_volume(b) for b in day_bars)
        if total > 0:
            daily_totals.append(total)

    daily_totals = daily_totals[-RVOL_LOOKBACK_DAYS:]
    if not daily_totals:
        return 1.0, 50.0

    baseline = median(daily_totals) or 1.0
    rvol = current_volume / baseline
    pctile = percentile_proxy(current_volume, daily_totals + [current_volume])
    return rvol, pctile


def runner_personality_scores(symbol: str) -> Tuple[float, float]:
    bars = get_daily_bars(symbol)
    if len(bars) < 20:
        return 40.0, 0.0

    excursions = []
    recent_excursions = []
    for i, b in enumerate(bars):
        o = safe_float(b.get("o"))
        h = safe_float(b.get("h"))
        c = safe_float(b.get("c"))
        if o <= 0:
            continue
        mfe = pct_change(h, o)
        excursions.append(mfe)
        if i >= len(bars) - 10:
            recent_excursions.append(mfe)

    if not excursions:
        return 40.0, 0.0

    hits20 = sum(1 for x in excursions if x >= 20)
    hits50 = sum(1 for x in excursions if x >= 50)
    hits100 = sum(1 for x in excursions if x >= 100)
    max_mfe = max(excursions)

    personality = clamp(
        25
        + min(25, hits20 * 4)
        + min(20, hits50 * 6)
        + min(15, hits100 * 8)
        + min(15, max_mfe / 10)
    )

    recent_big = sum(1 for x in recent_excursions if x >= 20)
    fatigue = clamp(recent_big * 18 + max(0, max(recent_excursions or [0]) - 80) * 0.2)
    return personality, fatigue


def extension_saturation_score(change_pct_value: float, rotation: float, price: float, vwap: float) -> float:
    risk = 0.0
    if change_pct_value > 40:
        risk += min(30, (change_pct_value - 40) * 0.35)
    if change_pct_value > 100:
        risk += 15
    if rotation > 4:
        risk += min(30, (rotation - 4) * 4)
    if vwap > 0:
        vwap_extension = pct_change(price, vwap)
        if vwap_extension > 12:
            risk += min(25, (vwap_extension - 12) * 1.2)
    return clamp(risk)


def trajectory_from_history(c: Candidate) -> Tuple[float, float]:
    hist = c.history[-8:]
    if len(hist) < 3:
        return 50.0, 0.0
    scores = [safe_float(x.get("opportunity_score")) for x in hist]
    failures = [safe_float(x.get("failure_pressure")) for x in hist]
    slope = (scores[-1] - scores[0]) / max(1, len(scores) - 1)
    failure_slope = (failures[-1] - failures[0]) / max(1, len(failures) - 1)
    score = clamp(50 + slope * 8 - max(0, failure_slope) * 5)
    return score, slope


def cross_session_persistence_score(c: Candidate, current_acceptance: float) -> float:
    phases = []
    positive_hits = 0
    for h in c.history:
        ph = h.get("phase")
        if ph and ph not in phases:
            phases.append(ph)
        if safe_float(h.get("opportunity_score")) >= 65:
            positive_hits += 1

    phase_bonus = min(35, len(phases) * 10)
    consistency = min(30, positive_hits * 4)
    acceptance_bonus = current_acceptance * 0.35
    return clamp(phase_bonus + consistency + acceptance_bonus)


def evidence_convergence_score(groups: FeatureGroups) -> float:
    vals = list(asdict(groups.normalized()).values())
    avg = mean(vals)
    dispersion = pstdev(vals) if len(vals) > 1 else 0.0
    return clamp(avg - dispersion * 0.8 + 15)


# ==============================================================================
# Risk / Failure Pressure
# ==============================================================================

def structural_risk(
    symbol: str,
    news: Optional[Dict[str, Any]],
    float_shares: Optional[float],
    spread_pct: float,
    extension_risk: float,
) -> Tuple[StructuralRisk, List[str]]:
    reasons = []
    level = StructuralRisk.LOW

    analysis = (news or {}).get("analysis") or {}
    if analysis.get("serious_negative"):
        level = StructuralRisk.CRITICAL
        reasons.append("serious_negative_news")

    text = news_text(news)
    if any(k in text for k in HARD_NEGATIVE_KEYWORDS):
        level = StructuralRisk.CRITICAL
        reasons.append("hard_negative_language")

    if any(k in text for k in DILUTION_KEYWORDS):
        if RISK_ORDER[level] < RISK_ORDER[StructuralRisk.HIGH]:
            level = StructuralRisk.HIGH
        reasons.append("dilution_capacity_or_immediacy")

    if float_shares is None:
        if RISK_ORDER[level] < RISK_ORDER[StructuralRisk.MODERATE]:
            level = StructuralRisk.MODERATE
        reasons.append("float_unknown")
    elif float_shares < 750_000:
        if RISK_ORDER[level] < RISK_ORDER[StructuralRisk.MODERATE]:
            level = StructuralRisk.MODERATE
        reasons.append("micro_float_supply_risk")
    elif float_shares > MAX_FLOAT:
        if RISK_ORDER[level] < RISK_ORDER[StructuralRisk.MODERATE]:
            level = StructuralRisk.MODERATE
        reasons.append("large_float")

    if spread_pct > 4:
        if RISK_ORDER[level] < RISK_ORDER[StructuralRisk.HIGH]:
            level = StructuralRisk.HIGH
        reasons.append("very_wide_spread")
    elif spread_pct > 2:
        if RISK_ORDER[level] < RISK_ORDER[StructuralRisk.MODERATE]:
            level = StructuralRisk.MODERATE
        reasons.append("wide_spread")

    if extension_risk >= 80 and RISK_ORDER[level] < RISK_ORDER[StructuralRisk.MODERATE]:
        level = StructuralRisk.MODERATE
        reasons.append("extreme_extension")

    return level, reasons


def failure_pressure_score(
    demand_eff: float,
    acceptance: float,
    pullback: float,
    reclaim: float,
    spread_quality: float,
    trajectory: float,
    bars: List[Dict[str, Any]],
) -> Tuple[float, List[str]]:
    pressure = 0.0
    reasons = []

    if demand_eff < 45:
        pressure += (45 - demand_eff) * 0.8
        reasons.append("demand_efficiency_deteriorating")
    if acceptance < 45:
        pressure += (45 - acceptance) * 0.7
        reasons.append("acceptance_deteriorating")
    if pullback < 40:
        pressure += (40 - pullback) * 0.6
        reasons.append("weak_pullback_recovery")
    if reclaim < 40:
        pressure += (40 - reclaim) * 0.4
        reasons.append("failed_high_reclaim")
    if spread_quality < 40:
        pressure += (40 - spread_quality) * 0.5
        reasons.append("liquidity_deterioration")
    if trajectory < 40:
        pressure += (40 - trajectory) * 0.8
        reasons.append("evidence_trajectory_down")

    # Sell-volume dominance on recent bars.
    if len(bars) >= 8:
        recent = bars[-12:]
        closes = [bar_close(b) for b in recent]
        vols = [bar_volume(b) for b in recent]
        upv = dnv = 0.0
        for i in range(1, len(recent)):
            if closes[i] >= closes[i - 1]:
                upv += vols[i]
            else:
                dnv += vols[i]
        if dnv > upv * 1.5:
            pressure += 18
            reasons.append("sell_volume_dominance")

        # Repeated failed highs proxy.
        hi = max(bar_high(b) for b in recent[:-1])
        failures = sum(
            1 for b in recent[-6:]
            if bar_high(b) >= hi * 0.995 and bar_close(b) < hi * 0.985
        )
        if failures >= 2:
            pressure += 12
            reasons.append("repeated_failed_highs")

    return clamp(pressure), reasons


# ==============================================================================
# Live research / calibration logger
# ==============================================================================

def update_live_research_metrics(c: Candidate) -> None:
    if c.price <= 0:
        return

    now_iso = iso()
    if c.discovery_price <= 0:
        c.discovery_price = c.price
        c.discovery_time = now_iso
        c.max_price_after_discovery = c.price
        c.min_price_after_discovery = c.price

    if c.price > c.max_price_after_discovery:
        c.max_price_after_discovery = c.price
        if c.discovery_price > 0:
            c.mfe_pct = pct_change(c.max_price_after_discovery, c.discovery_price)
        start = parse_ts(c.discovery_time)
        if start:
            c.time_to_mfe_minutes = max(
                0.0,
                (now_utc() - start.astimezone(UTC_TZ)).total_seconds() / 60.0,
            )

    if c.min_price_after_discovery <= 0 or c.price < c.min_price_after_discovery:
        c.min_price_after_discovery = c.price
        if c.discovery_price > 0:
            c.mae_pct = pct_change(c.min_price_after_discovery, c.discovery_price)


def record_first_state_time(c: Candidate) -> None:
    now_iso = iso()
    if c.state == CandidateState.AWAKENING and not c.first_awakening_at:
        c.first_awakening_at = now_iso
    elif c.state == CandidateState.ACCEPTED and not c.first_accepted_at:
        c.first_accepted_at = now_iso
    elif c.state == CandidateState.BUILDING and not c.first_building_at:
        c.first_building_at = now_iso
    elif c.state == CandidateState.BREAKOUT_READY and not c.first_breakout_ready_at:
        c.first_breakout_ready_at = now_iso
    elif c.state == CandidateState.ELITE_CONTINUATION and not c.first_elite_at:
        c.first_elite_at = now_iso


def research_summary(c: Candidate, reason: str = "finalized") -> Dict[str, Any]:
    return {
        "symbol": c.symbol,
        "reason": reason,
        "discovery_time": c.discovery_time,
        "discovery_price": c.discovery_price,
        "finalized_at": iso(),
        "last_price": c.price,
        "max_price_after_discovery": c.max_price_after_discovery,
        "min_price_after_discovery": c.min_price_after_discovery,
        "mfe_pct": round(c.mfe_pct, 3),
        "mae_pct": round(c.mae_pct, 3),
        "time_to_mfe_minutes": round(c.time_to_mfe_minutes, 1),
        "highest_state": highest_state_reached(c),
        "first_awakening_at": c.first_awakening_at,
        "first_accepted_at": c.first_accepted_at,
        "first_building_at": c.first_building_at,
        "first_breakout_ready_at": c.first_breakout_ready_at,
        "first_elite_at": c.first_elite_at,
        "last_opportunity_score": c.opportunity_score,
        "structural_risk": c.structural_risk.value,
        "failure_pressure": c.failure_pressure,
        "entry_confirmed": c.entry_confirmed,
        "entry_plan": c.last_entry_plan,
        "catalyst_headline": c.catalyst_headline,
        "feature_groups": asdict(c.feature_groups),
        "history_points": len(c.history),
    }


def highest_state_reached(c: Candidate) -> str:
    order = {
        "STEALTH": 0,
        "AWAKENING": 1,
        "ACCEPTED": 2,
        "BUILDING": 3,
        "BREAKOUT_READY": 4,
        "ELITE_CONTINUATION": 5,
        "CROWDED": 3,
        "EXHAUSTED": 2,
        "FAILED": 0,
    }
    states = [str(h.get("state") or "STEALTH") for h in c.history] + [c.state.value]
    return max(states, key=lambda s: order.get(s, -1)) if states else c.state.value


def push_research_history(record: Dict[str, Any]) -> None:
    payload = json.dumps(record, ensure_ascii=False, default=str)
    redis_cmd("LPUSH", REDIS_KEYS["research_history"], payload)
    redis_cmd("LTRIM", REDIS_KEYS["research_history"], 0, RESEARCH_HISTORY_MAX - 1)


def finalize_research_candidate(c: Candidate, reason: str) -> None:
    if c.research_finalized:
        return
    push_research_history(research_summary(c, reason))
    c.research_finalized = True


# ==============================================================================
# State machine
# ==============================================================================

def infer_state(c: Candidate) -> CandidateState:
    score = c.opportunity_score
    fp = c.failure_pressure

    if c.structural_risk == StructuralRisk.CRITICAL:
        return CandidateState.FAILED
    if fp >= 80:
        return CandidateState.EXHAUSTED
    if fp >= 60 or c.extension_risk >= 85:
        return CandidateState.CROWDED

    # The state machine uses more than the total score.
    if (
        score >= STATE_THRESHOLDS["elite_continuation"]
        and c.demand_efficiency >= 70
        and c.price_acceptance >= 70
    ):
        return CandidateState.ELITE_CONTINUATION

    if (
        score >= STATE_THRESHOLDS["breakout_ready"]
        and c.reclaim_structure >= 65
        and c.demand_efficiency >= 65
    ):
        return CandidateState.BREAKOUT_READY

    if (
        score >= STATE_THRESHOLDS["building"]
        and c.price_acceptance >= 65
        and c.pullback_quality >= 55
    ):
        return CandidateState.BUILDING

    if (
        score >= STATE_THRESHOLDS["accepted"]
        and c.price_acceptance >= 58
    ):
        return CandidateState.ACCEPTED

    if score >= STATE_THRESHOLDS["awakening"]:
        return CandidateState.AWAKENING

    return CandidateState.STEALTH


def cached_micro_quotes(
    symbol: str,
    start: datetime,
    end: datetime,
    feed: str,
) -> List[Dict[str, Any]]:
    key = f"quotes:{symbol}:{feed}"
    cached = feature_cache_get(key, MICROSTRUCTURE_CACHE_TTL)
    if isinstance(cached, list):
        return cached
    return feature_cache_set(
        key,
        get_quotes(symbol, start, end, feed=feed, limit=1200),
    )


def cached_micro_trades(
    symbol: str,
    start: datetime,
    end: datetime,
    feed: str,
) -> List[Dict[str, Any]]:
    key = f"trades:{symbol}:{feed}"
    cached = feature_cache_get(key, MICROSTRUCTURE_CACHE_TTL)
    if isinstance(cached, list):
        return cached
    return feature_cache_set(
        key,
        get_trades(symbol, start, end, feed=feed, limit=10000),
    )


def cached_runner_personality_scores(symbol: str) -> Tuple[float, float]:
    key = f"runner:{symbol}"
    cached = feature_cache_get(key, RUNNER_CACHE_TTL)
    if isinstance(cached, (list, tuple)) and len(cached) == 2:
        return float(cached[0]), float(cached[1])
    value = runner_personality_scores(symbol)
    feature_cache_set(key, list(value))
    return value


def cached_historical_session_rvol(
    symbol: str,
    phase: SessionPhase,
    current_volume: float,
    elapsed_minutes: int,
) -> Tuple[float, float]:
    # Same-time baseline is refreshed by 15-minute bucket, not every watch tick.
    bucket = max(0, elapsed_minutes // 15)
    key = f"rvol:{symbol}:{phase.value}:{bucket}"
    cached = feature_cache_get(key, RVOL_CACHE_TTL)
    if isinstance(cached, (list, tuple)) and len(cached) == 2:
        baseline_rvol, pctile = float(cached[0]), float(cached[1])
        # The cached ratio was computed with a prior current volume; preserve the
        # percentile proxy but let the ratio scale approximately with new volume.
        return baseline_rvol, pctile
    value = historical_session_rvol(symbol, phase, current_volume, elapsed_minutes)
    feature_cache_set(key, list(value))
    return value


# ==============================================================================
# Deep candidate evaluator
# ==============================================================================

def deep_evaluate(symbol: str) -> Optional[Candidate]:
    if not allow_deep_evaluation():
        return None

    phase = current_market_session()
    if phase == SessionPhase.CLOSED:
        return None

    feed = feed_for_phase(phase)
    now_et = now_ny()
    sstart = session_start_et(now_et, phase)

    bars = session_bars(symbol, phase, now_et)
    if len(bars) < 3:
        return None

    price = bar_close(bars[-1])
    if price < PRICE_MIN or price > PRICE_MAX:
        return None

    # Use shared snapshot for reference price and current quote when available.
    snap = get_snapshots([symbol], feed).get(symbol, {})
    ref = snapshot_reference_close(snap, phase)
    if ref <= 0:
        ref = bar_close(bars[0])

    change = pct_change(price, ref)
    high = max(bar_high(b) for b in bars)
    low = min(bar_low(b) for b in bars)
    total_vol = sum(bar_volume(b) for b in bars)
    vwap = compute_vwap(bars)

    f = get_float(symbol)
    rotation = total_vol / f if f and f > 0 else 0.0

    # Detailed recent microstructure.
    q_start = now_et - timedelta(minutes=DETAIL_QUOTE_LOOKBACK_MIN)
    quotes = cached_micro_quotes(symbol, q_start, now_et, feed)
    spread_pct, spread_quality, liquidity_evolution = spread_metrics(quotes, price)

    t_start = now_et - timedelta(minutes=DETAIL_TRADE_LOOKBACK_MIN)
    trades = cached_micro_trades(symbol, t_start, now_et, feed)
    continuity = trade_continuity_score(trades, t_start, now_et)

    vol_accel_score, vol_accel_ratio = volume_acceleration_score(bars)
    acceptance, acceptance_meta = price_acceptance_score(bars, ref)
    pullback, pullback_meta = pullback_quality_score(bars)
    reclaim, resistance = reclaim_structure_score(bars, vwap)
    demand_eff, demand_meta = demand_efficiency_score(bars, f, ref)

    elapsed = max(1, int((now_et - sstart).total_seconds() / 60))
    rvol, rvol_pctile = cached_historical_session_rvol(
        symbol, phase, total_vol, elapsed
    )

    news = get_shared_news(symbol)
    cat_strength, unpriced, market_response, headline, category, cat_ts, news_flags = score_catalyst(
        news, change, vol_accel_ratio, acceptance
    )

    runner_personality, fatigue = cached_runner_personality_scores(symbol)
    extension = extension_saturation_score(change, rotation, price, vwap)

    with candidate_lock:
        existing = candidates.get(symbol)

    if existing is None:
        c = Candidate(
            symbol=symbol,
            created_at=iso(),
            updated_at=iso(),
        )
    else:
        c = existing

    # Temporarily update raw features before persistence/trajectory.
    c.previous_state = c.state
    c.phase = phase
    c.updated_at = iso()
    c.price = price
    c.reference_price = ref
    c.change_pct = change
    c.high = high
    c.low = low
    c.vwap = vwap
    c.resistance = resistance
    c.spread_pct = spread_pct
    c.float_shares = f
    c.session_volume = total_vol
    c.float_rotation = rotation
    c.rvol = rvol
    c.rvol_percentile_proxy = rvol_pctile
    c.volume_acceleration = vol_accel_ratio
    c.trade_continuity = continuity

    c.demand_efficiency = demand_eff
    c.price_acceptance = acceptance
    c.pullback_quality = pullback
    c.reclaim_structure = reclaim

    c.spread_quality = spread_quality
    c.liquidity_evolution = liquidity_evolution
    c.runner_personality = runner_personality
    c.runner_fatigue = fatigue
    c.extension_risk = extension

    c.catalyst_strength = cat_strength
    c.unpriced_catalyst = unpriced
    c.catalyst_market_response = market_response
    c.catalyst_headline = headline
    c.catalyst_category = category
    c.catalyst_ts = cat_ts

    persistence = cross_session_persistence_score(c, acceptance)
    trajectory, trajectory_slope = trajectory_from_history(c)
    c.cross_session_persistence = persistence
    c.trajectory_score = trajectory

    catalyst_group = clamp(mean([cat_strength, unpriced, market_response]))
    participation_group = clamp(mean([
        min(100, rvol_pctile),
        vol_accel_score,
        continuity,
        rotation_score(rotation),
    ]))
    demand_group = clamp(mean([demand_eff, acceptance, pullback, reclaim]))
    persistence_group = clamp(mean([persistence, trajectory]))
    liquidity_group = clamp(mean([spread_quality, liquidity_evolution]))

    context_base = mean([
        runner_personality,
        clamp(100 - fatigue),
        clamp(100 - extension),
        safe_float(market_context.get("heat_score"), 50),
    ])

    preliminary = FeatureGroups(
        catalyst=catalyst_group,
        participation=participation_group,
        demand_quality=demand_group,
        persistence=persistence_group,
        liquidity=liquidity_group,
        historical_context=context_base,
    )
    convergence = evidence_convergence_score(preliminary)
    c.evidence_convergence = convergence

    context_group = clamp(mean([
        runner_personality,
        clamp(100 - fatigue),
        clamp(100 - extension),
        safe_float(market_context.get("heat_score"), 50),
        convergence,
    ]))

    c.feature_groups = FeatureGroups(
        catalyst=catalyst_group,
        participation=participation_group,
        demand_quality=demand_group,
        persistence=persistence_group,
        liquidity=liquidity_group,
        historical_context=context_group,
    )
    c.opportunity_score = c.feature_groups.opportunity()

    s_risk, risk_reasons = structural_risk(
        symbol, news, f, spread_pct, extension
    )
    c.structural_risk = s_risk
    c.risk_reasons = risk_reasons + news_flags

    fp, fp_reasons = failure_pressure_score(
        demand_eff,
        acceptance,
        pullback,
        reclaim,
        spread_quality,
        trajectory,
        bars,
    )
    c.failure_pressure = fp
    c.state = infer_state(c)

    update_live_research_metrics(c)
    record_first_state_time(c)

    c.evidence = build_evidence(c)
    c.warnings = build_warnings(c, fp_reasons)

    c.entry_eligible, c.entry_block_reasons = entry_gate(c)

    c.history.append({
        "ts": iso(),
        "phase": phase.value,
        "price": round(price, 6),
        "opportunity_score": c.opportunity_score,
        "failure_pressure": c.failure_pressure,
        "state": c.state.value,
        "demand_efficiency": c.demand_efficiency,
        "price_acceptance": c.price_acceptance,
        "rvol": c.rvol,
        "rotation": c.float_rotation,
        "spread_pct": c.spread_pct,
        "trajectory_slope": trajectory_slope,
    })
    if len(c.history) > MAX_HISTORY_POINTS:
        c.history = c.history[-MAX_HISTORY_POINTS:]

    with candidate_lock:
        candidates[symbol] = c

    with stats_lock:
        runtime_stats["deep_evaluations"] += 1

    maybe_send_state_transition(c)
    return c


def rotation_score(rotation: float) -> float:
    """
    Non-monotonic by design:
      no participation -> low
      useful turnover -> higher
      extreme turnover -> saturation penalty
    """
    if rotation <= 0:
        return 35
    if rotation < 0.1:
        return 45
    if rotation < 0.5:
        return 60 + rotation * 30
    if rotation < 2.0:
        return 75 + min(20, (rotation - 0.5) * 12)
    if rotation < 5.0:
        return 92 - (rotation - 2.0) * 4
    if rotation < 10.0:
        return 80 - (rotation - 5.0) * 7
    return max(20, 45 - (rotation - 10) * 3)


def build_evidence(c: Candidate) -> List[str]:
    ev = []
    if c.catalyst_strength >= 70:
        ev.append("محفز قوي وحديث")
    if c.rvol >= 2:
        ev.append(f"RVOL غير طبيعي {c.rvol:.1f}x")
    if c.volume_acceleration >= 1.5:
        ev.append("الحجم يتسارع")
    if c.demand_efficiency >= 70:
        ev.append("كفاءة الطلب ممتازة")
    if c.price_acceptance >= 70:
        ev.append("قبول سعري قوي")
    if c.pullback_quality >= 65:
        ev.append("التراجعات تُشترى بجودة")
    if c.cross_session_persistence >= 70:
        ev.append("استمرار قوي عبر الجلسات")
    if c.liquidity_evolution >= 65:
        ev.append("السيولة تتحسن")
    if c.evidence_convergence >= 75:
        ev.append("توافق أدلة مرتفع")
    return ev[:8]


def build_warnings(c: Candidate, failure_reasons: List[str]) -> List[str]:
    w = []
    if c.extension_risk >= 65:
        w.append("تمدد/ازدحام مرتفع")
    if c.runner_fatigue >= 55:
        w.append("Runner fatigue حديث")
    if c.spread_pct > 2:
        w.append("سبريد واسع")
    if c.float_shares is None:
        w.append("الفلوت غير متوفر")
    if c.structural_risk in (StructuralRisk.HIGH, StructuralRisk.CRITICAL):
        w.append("مخاطر هيكلية مرتفعة")
    mapping = {
        "demand_efficiency_deteriorating": "كفاءة الطلب تتدهور",
        "acceptance_deteriorating": "القبول السعري يتدهور",
        "sell_volume_dominance": "سيطرة حجم البيع",
        "repeated_failed_highs": "فشل متكرر قرب القمة",
        "liquidity_deterioration": "السيولة تتدهور",
        "evidence_trajectory_down": "مسار الأدلة يتراجع",
    }
    for r in failure_reasons:
        if r in mapping:
            w.append(mapping[r])
    return list(dict.fromkeys(w))[:8]


# ==============================================================================
# Discovery alerts
# ==============================================================================

DISCOVERY_ALERT_STATES = {
    CandidateState.BREAKOUT_READY,
    CandidateState.ELITE_CONTINUATION,
}


def state_alert_key(c: Candidate) -> str:
    return f"{now_ny().date().isoformat()}:{c.symbol}"


def maybe_send_state_transition(c: Candidate) -> None:
    if not SEND_DISCOVERY_ALERTS:
        return

    if c.state not in DISCOVERY_ALERT_STATES:
        return

    key = state_alert_key(c)
    alert_token = "BREAKOUT_READY"

    with sent_lock:
        states = sent_state_alerts.setdefault(key, [])

        if alert_token in states:
            return

        states.append(alert_token)

    if telegram_send(discovery_alert(c)):
        with stats_lock:
            runtime_stats["alerts_sent"] += 1
            
# ==============================================================================
# Entry Confirmation Engine
# ==============================================================================

def entry_allowed_now(phase: SessionPhase) -> bool:
    if phase == SessionPhase.REGULAR:
        return True
    if ALLOW_PREMARKET_ENTRY and phase == SessionPhase.PREMARKET_LATE:
        return True
    return False


def entry_gate(c: Candidate) -> Tuple[bool, List[str]]:
    reasons = []
    if c.state not in (
        CandidateState.BREAKOUT_READY,
        CandidateState.ELITE_CONTINUATION,
    ):
        reasons.append(f"state:{c.state.value}")
    if c.opportunity_score < ENTRY_MIN_OPPORTUNITY:
        reasons.append(f"opportunity:{c.opportunity_score:.1f}")
    if RISK_ORDER[c.structural_risk] > RISK_ORDER[StructuralRisk.MODERATE]:
        reasons.append(f"structural:{c.structural_risk.value}")
    if c.failure_pressure > ENTRY_MAX_FAILURE_PRESSURE:
        reasons.append(f"failure_pressure:{c.failure_pressure:.1f}")
    if c.spread_pct > ENTRY_MAX_SPREAD_PCT:
        reasons.append(f"spread:{c.spread_pct:.2f}%")
    if c.vwap > 0 and c.price < c.vwap:
        reasons.append("below_vwap")
    if c.demand_efficiency < 65:
        reasons.append("demand_efficiency")
    if c.price_acceptance < 62:
        reasons.append("price_acceptance")
    if c.volume_acceleration < 1.0:
        reasons.append("volume_not_accelerating")
    for reason in reasons:
        bump_rejection(reason.split(":", 1)[0])
    return not reasons, reasons


def calculate_atr(bars: List[Dict[str, Any]], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    prev_close = bar_close(bars[0])
    for b in bars[1:]:
        h = bar_high(b)
        l = bar_low(b)
        c = bar_close(b)
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    recent = trs[-period:]
    return mean(recent) if recent else 0.0


def build_entry_plan(c: Candidate, bars: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    entry = c.price
    if entry <= 0 or len(bars) < 8:
        return None

    atr = calculate_atr(bars)
    recent_lows = [bar_low(b) for b in bars[-8:] if bar_low(b) > 0]
    swing_low = min(recent_lows) if recent_lows else entry * 0.97

    atr_stop = entry - max(atr * 1.3, entry * 0.015)
    stop = max(swing_low * 0.998, atr_stop)

    stop_pct = max(0.0, (entry - stop) / entry * 100)
    if stop >= entry or stop_pct <= 0:
        return None
    if stop_pct > ENTRY_MAX_STOP_PCT:
        return None

    risk = entry - stop
    t1 = entry + risk * max(ENTRY_MIN_RR_T1, 1.5)
    t2 = entry + risk * 2.5
    t3 = entry + risk * 4.0

    return {
        "symbol": c.symbol,
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "stop_pct": round(stop_pct, 2),
        "t1": round(t1, 4),
        "t2": round(t2, 4),
        "t3": round(t3, 4),
        "risk_per_share": round(risk, 4),
        "opportunity_score": round(c.opportunity_score, 2),
        "failure_pressure": round(c.failure_pressure, 2),
        "structural_risk": c.structural_risk.value,
        "state": c.state.value,
        "entry_ts": time.time(),
        "entry_time_ksa": now_ksa().isoformat(),
    }


def entry_alert_message(c: Candidate, plan: Dict[str, Any]) -> str:
    risk_ar = structural_risk_ar(c.structural_risk)

    return (
        f"🚨🔥 <b>دخول مؤكد — {BOT_NAME_AR}</b>\n\n"
        f"<b>{html.escape(c.symbol)}</b>\n"
        f"💵 سعر الدخول: <b>${plan['entry']:.4f}</b>\n"
        f"🛑 وقف الخسارة: <b>${plan['stop']:.4f}</b> ({plan['stop_pct']:.1f}%)\n"
        f"🎯 الهدف الأول: <b>${plan['t1']:.4f}</b>\n"
        f"🎯 الهدف الثاني: <b>${plan['t2']:.4f}</b>\n"
        f"🎯 الهدف الثالث: <b>${plan['t3']:.4f}</b>\n\n"
        f"🧠 قوة الفرصة: <b>{c.opportunity_score:.1f}/100</b>\n"
        f"🏗️ المخاطر الهيكلية: <b>{risk_ar}</b>\n"
        f"⚠️ ضغط الفشل: <b>{c.failure_pressure:.1f}/100</b>\n"
        f"⚡ كفاءة الطلب: <b>{c.demand_efficiency:.0f}/100</b>\n"
        f"✅ القبول السعري: <b>{c.price_acceptance:.0f}/100</b>\n"
        f"📦 دوران الفلوت: {c.float_rotation:.2f}x\n"
        f"📈 RVOL: {c.rvol:.2f}x\n"
        f"💧 السبريد: {c.spread_pct:.2f}%\n\n"
        f"📰 {html.escape(c.catalyst_headline[:180] or 'لا يوجد محفز إخباري مركزي حديث')}\n\n"
        f"✅ <b>تم تأكيد شروط الدخول النهائية</b>\n"
        f"🚨 <b>هذه إشارة دخول مؤكدة وليست تنبيه مراقبة.</b>\n\n"
        f"⚠️ ليست توصية مالية. التزم بإدارة رأس المال."
    )

def entry_alerted_today(symbol: str) -> bool:
    key = f"{now_ny().date().isoformat()}:{symbol.upper()}"
    with sent_lock:
        return key in sent_entry_alerts


def mark_entry_alerted(symbol: str, plan: Dict[str, Any]) -> None:
    key = f"{now_ny().date().isoformat()}:{symbol.upper()}"
    with sent_lock:
        sent_entry_alerts[key] = plan


def ltm_already_active(symbol: str) -> bool:
    raw = redis_cmd("HGET", LTM_ACTIVE_KEY, symbol.upper())
    return raw is not None


def send_to_ltm(c: Candidate, plan: Dict[str, Any]) -> bool:
    if not SEND_TO_LTM:
        return False
    if ltm_already_active(c.symbol):
        return True
    payload = {
        "source_bot": "next_day_explosion",
        "symbol": c.symbol,
        "entry_price": plan["entry"],
        "entry_ts": plan["entry_ts"],
        "stop": plan["stop"],
        "t1": plan["t1"],
        "t2": plan["t2"],
        "t3": plan["t3"],
        "score": c.opportunity_score,
        "confidence": max(50, min(99, c.opportunity_score - c.failure_pressure * 0.25)),
        "grade": c.state.value,
        "session": c.phase.value,
        "metadata": {
            "structural_risk": c.structural_risk.value,
            "failure_pressure": c.failure_pressure,
            "demand_efficiency": c.demand_efficiency,
            "price_acceptance": c.price_acceptance,
            "float_rotation": c.float_rotation,
            "catalyst_headline": c.catalyst_headline,
        },
    }
    result = redis_cmd(
        "LPUSH",
        LTM_INCOMING_KEY,
        json.dumps(payload, ensure_ascii=False),
    )
    return result is not None


def confirm_entry(c: Candidate) -> bool:
    phase = current_market_session()
    if not entry_allowed_now(phase):
        return False
    if entry_alerted_today(c.symbol):
        return False

    # Fresh deep evaluation immediately before the entry decision.
    fresh = deep_evaluate(c.symbol)
    if not fresh:
        return False
    c = fresh

    eligible, reasons = entry_gate(c)
    c.entry_eligible = eligible
    c.entry_block_reasons = reasons
    if not eligible:
        return False

    bars = session_bars(c.symbol, phase, now_ny())
    if len(bars) < 10:
        return False

    # Final breakout/reclaim confirmation: latest completed bars must be
    # holding near/above the precomputed resistance with healthy volume.
    closes = [bar_close(b) for b in bars[-3:]]
    if c.resistance > 0:
        hold_count = sum(1 for x in closes[-2:] if x >= c.resistance * 0.998)
        if hold_count < 2:
            c.entry_block_reasons = ["breakout_not_held"]
            return False

    plan = build_entry_plan(c, bars)
    if not plan:
        c.entry_block_reasons = ["trade_plan_invalid"]
        return False

    if DRY_RUN or not ENABLE_LIVE_ENTRY_ALERTS:
        print(
            f"[DRY RUN ENTRY] {c.symbol} entry={plan['entry']} "
            f"stop={plan['stop']} score={c.opportunity_score:.1f}",
            flush=True,
        )
        c.last_entry_plan = plan
        return True

    if telegram_send(entry_alert_message(c, plan)):
        c.entry_confirmed = True
        c.last_entry_plan = plan
        mark_entry_alerted(c.symbol, plan)
        send_to_ltm(c, plan)
        with stats_lock:
            runtime_stats["entries_sent"] += 1
        return True
    return False


# ==============================================================================
# Persistence
# ==============================================================================

def candidate_to_json(c: Candidate) -> Dict[str, Any]:
    d = asdict(c)
    d["state"] = c.state.value
    d["previous_state"] = c.previous_state.value
    d["phase"] = c.phase.value
    d["structural_risk"] = c.structural_risk.value
    return d


def candidate_from_json(d: Dict[str, Any]) -> Optional[Candidate]:
    try:
        fg = d.get("feature_groups") or {}
        d = dict(d)
        d["feature_groups"] = FeatureGroups(**{
            k: safe_float(v)
            for k, v in fg.items()
            if k in FeatureGroups.__dataclass_fields__
        })
        d["state"] = CandidateState(d.get("state", "STEALTH"))
        d["previous_state"] = CandidateState(
            d.get("previous_state", d["state"].value)
        )
        d["phase"] = SessionPhase(d.get("phase", "CLOSED"))
        d["structural_risk"] = StructuralRisk(
            d.get("structural_risk", "LOW")
        )
        allowed = Candidate.__dataclass_fields__.keys()
        d = {k: v for k, v in d.items() if k in allowed}
        return Candidate(**d)
    except Exception:
        return None


def save_state() -> None:
    with candidate_lock:
        cdata = {s: candidate_to_json(c) for s, c in candidates.items()}
    with sent_lock:
        ss = dict(sent_state_alerts)
        se = dict(sent_entry_alerts)

    redis_set_json(REDIS_KEYS["candidates"], cdata)
    redis_set_json(REDIS_KEYS["sent_state_alerts"], ss)
    redis_set_json(REDIS_KEYS["sent_entry_alerts"], se)
    redis_set_json(REDIS_KEYS["runtime_stats"], runtime_stats)
    with stats_lock:
        runtime_stats["last_state_save"] = iso()


def restore_state() -> None:
    global universe, candidates, sent_state_alerts, sent_entry_alerts

    u = redis_get_json(REDIS_KEYS["universe"], {})
    if isinstance(u, dict):
        with universe_lock:
            universe = u

    cdata = redis_get_json(REDIS_KEYS["candidates"], {})
    restored = {}
    if isinstance(cdata, dict):
        for s, raw in cdata.items():
            if isinstance(raw, dict):
                c = candidate_from_json(raw)
                if c:
                    restored[s] = c
    with candidate_lock:
        candidates = restored

    ss = redis_get_json(REDIS_KEYS["sent_state_alerts"], {})
    se = redis_get_json(REDIS_KEYS["sent_entry_alerts"], {})
    with sent_lock:
        sent_state_alerts = ss if isinstance(ss, dict) else {}
        sent_entry_alerts = se if isinstance(se, dict) else {}

    with stats_lock:
        runtime_stats["universe_count"] = len(universe)
        runtime_stats["candidate_count"] = len(candidates)


def prune_candidates() -> None:
    cutoff = now_utc() - timedelta(hours=CANDIDATE_MAX_AGE_HOURS)
    remove = []
    with candidate_lock:
        for s, c in candidates.items():
            dt = parse_ts(c.updated_at)
            if dt and dt < cutoff:
                remove.append(s)
        for s in remove:
            c = candidates.get(s)
            if c is not None:
                finalize_research_candidate(c, "candidate_expired")
            candidates.pop(s, None)

    # Keep only today's and yesterday's sent-state records.
    valid_dates = {
        now_ny().date().isoformat(),
        (now_ny().date() - timedelta(days=1)).isoformat(),
    }
    with sent_lock:
        for key in list(sent_state_alerts):
            if key.split(":", 1)[0] not in valid_dates:
                sent_state_alerts.pop(key, None)
        for key in list(sent_entry_alerts):
            if key.split(":", 1)[0] not in valid_dates:
                sent_entry_alerts.pop(key, None)


# ==============================================================================
# Runtime loops
# ==============================================================================

def discovery_cycle() -> None:
    phase = current_market_session()
    if phase == SessionPhase.CLOSED:
        return

    ranked = fast_discovery_candidates(phase)
    for symbol, _ in ranked:
        try:
            deep_evaluate(symbol)
        except Exception as e:
            with stats_lock:
                runtime_stats["last_error"] = f"deep_evaluate {symbol}: {e}"
        time.sleep(0.1)

    with stats_lock:
        runtime_stats["discovery_scans"] += 1
        runtime_stats["last_discovery_scan"] = iso()


def watch_cycle() -> None:
    with candidate_lock:
        items = sorted(
            candidates.values(),
            key=lambda c: c.opportunity_score,
            reverse=True,
        )

    for c in items[:60]:
        if c.state in (CandidateState.FAILED, CandidateState.EXHAUSTED):
            continue
        try:
            deep_evaluate(c.symbol)
        except Exception as e:
            with stats_lock:
                runtime_stats["last_error"] = f"watch {c.symbol}: {e}"
        time.sleep(0.08)

    with stats_lock:
        runtime_stats["last_watch_scan"] = iso()


def entry_cycle() -> None:
    phase = current_market_session()
    if not entry_allowed_now(phase):
        return

    with candidate_lock:
        items = [
            c for c in candidates.values()
            if c.entry_eligible
            or c.state in (
                CandidateState.BREAKOUT_READY,
                CandidateState.ELITE_CONTINUATION,
            )
        ]
    items.sort(key=lambda c: c.opportunity_score, reverse=True)

    for c in items[:30]:
        try:
            confirm_entry(c)
        except Exception as e:
            with stats_lock:
                runtime_stats["last_error"] = f"entry {c.symbol}: {e}"
        time.sleep(0.2)

    with stats_lock:
        runtime_stats["last_entry_scan"] = iso()


def update_runtime_counts() -> None:
    with candidate_lock:
        vals = list(candidates.values())
    counts = {
        "candidate_count": len(vals),
        "awakening_count": sum(c.state == CandidateState.AWAKENING for c in vals),
        "accepted_count": sum(c.state == CandidateState.ACCEPTED for c in vals),
        "building_count": sum(c.state == CandidateState.BUILDING for c in vals),
        "breakout_ready_count": sum(
            c.state in (CandidateState.BREAKOUT_READY, CandidateState.ELITE_CONTINUATION)
            for c in vals
        ),
    }
    with stats_lock:
        runtime_stats.update(counts)
        runtime_stats["last_loop"] = iso()
        runtime_stats["session"] = current_market_session().value
        runtime_stats["feed"] = feed_for_phase(current_market_session()) if current_market_session() != SessionPhase.CLOSED else "none"


def coordinator_loop() -> None:
    last_discovery = last_watch = last_entry = last_save = 0.0
    last_universe = last_float = last_news = 0.0

    while True:
        try:
            now_ts = time.time()

            if not universe or now_ts - last_universe >= UNIVERSE_REFRESH_SEC:
                rebuild_universe()
                last_universe = now_ts

            if not float_cache or now_ts - last_float >= FLOAT_REFRESH_SEC:
                load_float_cache()
                last_float = now_ts

            if now_ts - last_news >= SHARED_NEWS_REFRESH_SEC:
                refresh_shared_news_cache()
                last_news = now_ts

            phase = current_market_session()
            handle_session_transition(phase)

            if phase != SessionPhase.CLOSED:
                if now_ts - last_discovery >= DISCOVERY_INTERVAL_SEC:
                    discovery_cycle()
                    last_discovery = time.time()

                if now_ts - last_watch >= WATCH_INTERVAL_SEC:
                    watch_cycle()
                    last_watch = time.time()

                if now_ts - last_entry >= ENTRY_INTERVAL_SEC:
                    entry_cycle()
                    last_entry = time.time()

            prune_candidates()
            update_runtime_counts()

            if now_ts - last_save >= STATE_SAVE_INTERVAL_SEC:
                save_state()
                last_save = now_ts

            time.sleep(1.0)

        except Exception as e:
            with stats_lock:
                runtime_stats["last_error"] = f"Coordinator: {e}"
            traceback.print_exc()
            time.sleep(5)


# ==============================================================================
# Flask status page
# ==============================================================================

app = Flask(__name__)


@app.route("/")
def home():
    with candidate_lock:
        top = sorted(
            candidates.values(),
            key=lambda c: c.opportunity_score,
            reverse=True,
        )[:15]

    rows = "".join(
        f"<tr>"
        f"<td>{html.escape(c.symbol)}</td>"
        f"<td>{c.state.value}</td>"
        f"<td>{c.opportunity_score:.1f}</td>"
        f"<td>{c.structural_risk.value}</td>"
        f"<td>{c.failure_pressure:.1f}</td>"
        f"<td>${c.price:.4f}</td>"
        f"<td>{c.change_pct:+.1f}%</td>"
        f"</tr>"
        for c in top
    )

    return f"""
    <html>
    <head>
      <title>{BOT_NAME}</title>
      <meta http-equiv="refresh" content="20">
      <style>
        body {{ font-family: Arial, sans-serif; margin: 30px; background:#111; color:#eee; }}
        .ok {{ color:#7CFC98; }}
        .card {{ display:inline-block; padding:12px 18px; margin:6px; background:#222; border-radius:10px; }}
        table {{ border-collapse:collapse; width:100%; margin-top:20px; }}
        td,th {{ padding:8px; border-bottom:1px solid #333; text-align:left; }}
      </style>
    </head>
    <body>
      <h1>🚀 {BOT_NAME}</h1>
      <div class="ok">RUNNING — v{VERSION} / {BUILD}</div>
      <div class="card">Session: {runtime_stats.get('session')}</div>
      <div class="card">Feed: {runtime_stats.get('feed')}</div>
      <div class="card">Universe: {runtime_stats.get('universe_count')}</div>
      <div class="card">Float: {runtime_stats.get('float_count')}</div>
      <div class="card">Shared News: {runtime_stats.get('shared_news_count')}</div>
      <div class="card">Candidates: {runtime_stats.get('candidate_count')}</div>
      <div class="card">Accepted: {runtime_stats.get('accepted_count')}</div>
      <div class="card">Building: {runtime_stats.get('building_count')}</div>
      <div class="card">Breakout Ready: {runtime_stats.get('breakout_ready_count')}</div>
      <div class="card">Discovery Alerts: {runtime_stats.get('alerts_sent')}</div>
      <div class="card">Entry Alerts: {runtime_stats.get('entries_sent')}</div>
      <p>Last loop: {runtime_stats.get('last_loop')}</p>
      <p>Last error: {html.escape(str(runtime_stats.get('last_error') or 'None'))}</p>
      <h2>Top Candidates</h2>
      <table>
        <tr><th>Symbol</th><th>State</th><th>Opportunity</th><th>Risk</th><th>Failure</th><th>Price</th><th>Change</th></tr>
        {rows}
      </table>
    </body>
    </html>
    """


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "bot": BOT_NAME,
        "version": VERSION,
        "build": BUILD,
        "session": current_market_session().value,
        "stats": runtime_stats,
    })


@app.route("/ready")
def ready():
    tests = runtime_stats.get("self_test") or {}
    required = ("redis", "alpaca_sip", "shared_news", "float_cache")
    ok = all(bool((tests.get(k) or {}).get("ok")) for k in required)
    if ENABLE_OVERNIGHT_BOATS:
        ok = ok and bool((tests.get("alpaca_boats") or {}).get("ok"))
    return jsonify({
        "ok": ok,
        "required": {k: tests.get(k) for k in required},
        "alpaca_boats": tests.get("alpaca_boats"),
        "session": current_market_session().value,
        "feed": feed_for_phase(current_market_session()) if current_market_session() != SessionPhase.CLOSED else "none",
    }), (200 if ok else 503)


@app.route("/api/candidates")
def api_candidates():
    with candidate_lock:
        vals = sorted(
            candidates.values(),
            key=lambda c: c.opportunity_score,
            reverse=True,
        )
        payload = [candidate_to_json(c) for c in vals[:100]]
    return jsonify(payload)


@app.route("/api/config")
def api_config():
    return jsonify(config_snapshot())


@app.route("/api/diagnostics")
def api_diagnostics():
    with candidate_lock:
        vals = sorted(
            candidates.values(),
            key=lambda c: c.opportunity_score,
            reverse=True,
        )[:50]
        diagnostics = [candidate_diagnostic(c) for c in vals]
    return jsonify({
        "runtime": runtime_stats,
        "market_context": market_context,
        "candidates": diagnostics,
    })


@app.route("/api/candidate/<symbol>")
def api_candidate(symbol: str):
    symbol = symbol.upper().strip()
    with candidate_lock:
        c = candidates.get(symbol)
    if not c:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify(candidate_diagnostic(c))


@app.route("/api/research/recent")
def api_research_recent():
    raw = redis_cmd("LRANGE", REDIS_KEYS["research_history"], 0, 99)
    out = []
    if isinstance(raw, list):
        for item in raw:
            try:
                obj = json.loads(item)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
    return jsonify(out)


# ==============================================================================
# Operational self-test / diagnostics
# ==============================================================================

def test_redis_connection() -> Tuple[bool, str]:
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return False, "Redis env missing"
    value = redis_cmd("PING")
    if str(value).upper() == "PONG":
        return True, "PONG"
    return False, f"unexpected:{value}"


def test_alpaca_account_data() -> Tuple[bool, str]:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False, "Alpaca env missing"
    data = alpaca_get(f"{ALPACA_DATA_URL}/v2/stocks/AAPL/snapshot", params={"feed": "sip"})
    if isinstance(data, dict) and data:
        return True, "SIP snapshot OK"
    return False, "SIP snapshot unavailable"


def test_alpaca_boats() -> Tuple[bool, str]:
    if not ENABLE_OVERNIGHT_BOATS:
        return True, "BOATS disabled by config"
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False, "Alpaca env missing"
    end = now_utc()
    start = end - timedelta(days=5)
    data = alpaca_get(
        f"{ALPACA_DATA_URL}/v2/stocks/AAPL/bars",
        params={
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "timeframe": "1Min",
            "feed": "boats",
            "limit": 10,
            "sort": "desc",
        },
        timeout=20,
    )
    if isinstance(data, dict):
        return True, "BOATS endpoint reachable"
    return False, "BOATS endpoint unavailable"


def test_shared_news() -> Tuple[bool, str]:
    raw = redis_cmd("HLEN", SHARED_NEWS_HASH_KEY)
    count = safe_int(raw, -1)
    if count >= 0:
        return True, f"{count} records"
    return False, "HLEN unavailable"


def test_float_source() -> Tuple[bool, str]:
    if not GIST_ID:
        return False, "GIST_ID missing"
    with float_lock:
        count = len(float_cache)
    if count > 0:
        return True, f"{count} records"
    return False, "float cache empty"


def run_startup_self_test() -> Dict[str, Dict[str, Any]]:
    tests = {
        "redis": test_redis_connection,
        "alpaca_sip": test_alpaca_account_data,
        "alpaca_boats": test_alpaca_boats,
        "shared_news": test_shared_news,
        "float_cache": test_float_source,
    }
    results: Dict[str, Dict[str, Any]] = {}
    for name, fn in tests.items():
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, str(e)
        results[name] = {"ok": bool(ok), "detail": detail}
    with stats_lock:
        runtime_stats["self_test"] = results
    return results


def config_snapshot() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "build": BUILD,
        "price_min": PRICE_MIN,
        "price_max": PRICE_MAX,
        "max_float": MAX_FLOAT,
        "discovery_interval_sec": DISCOVERY_INTERVAL_SEC,
        "watch_interval_sec": WATCH_INTERVAL_SEC,
        "entry_interval_sec": ENTRY_INTERVAL_SEC,
        "max_deep_eval_per_minute": MAX_DEEP_EVAL_PER_MINUTE,
        "allow_premarket_entry": ALLOW_PREMARKET_ENTRY,
        "send_discovery_alerts": SEND_DISCOVERY_ALERTS,
        "send_to_ltm": SEND_TO_LTM,
        "dry_run": DRY_RUN,
        "enable_overnight_boats": ENABLE_OVERNIGHT_BOATS,
        "enable_live_entry_alerts": ENABLE_LIVE_ENTRY_ALERTS,
        "state_thresholds": dict(STATE_THRESHOLDS),
        "entry_min_opportunity": ENTRY_MIN_OPPORTUNITY,
        "entry_max_failure_pressure": ENTRY_MAX_FAILURE_PRESSURE,
        "entry_max_spread_pct": ENTRY_MAX_SPREAD_PCT,
        "group_weights": dict(GROUP_WEIGHTS),
        "shared_news_hash_key": SHARED_NEWS_HASH_KEY,
        "float_cache_filename": FLOAT_CACHE_FILENAME,
        "research_history_max": RESEARCH_HISTORY_MAX,
    }


def candidate_diagnostic(c: Candidate) -> Dict[str, Any]:
    age_min = 0.0
    dt = parse_ts(c.updated_at)
    if dt:
        age_min = max(0.0, (now_utc() - dt.astimezone(UTC_TZ)).total_seconds() / 60.0)
    return {
        "symbol": c.symbol,
        "state": c.state.value,
        "opportunity": c.opportunity_score,
        "structural_risk": c.structural_risk.value,
        "failure_pressure": c.failure_pressure,
        "age_minutes": round(age_min, 1),
        "entry_eligible": c.entry_eligible,
        "entry_block_reasons": list(c.entry_block_reasons),
        "evidence": list(c.evidence),
        "warnings": list(c.warnings),
        "research": {
            "discovery_time": c.discovery_time,
            "discovery_price": c.discovery_price,
            "mfe_pct": c.mfe_pct,
            "mae_pct": c.mae_pct,
            "time_to_mfe_minutes": c.time_to_mfe_minutes,
            "first_awakening_at": c.first_awakening_at,
            "first_accepted_at": c.first_accepted_at,
            "first_building_at": c.first_building_at,
            "first_breakout_ready_at": c.first_breakout_ready_at,
            "first_elite_at": c.first_elite_at,
        },
        "feature_groups": asdict(c.feature_groups),
        "core": {
            "rvol": c.rvol,
            "volume_acceleration": c.volume_acceleration,
            "float_rotation": c.float_rotation,
            "demand_efficiency": c.demand_efficiency,
            "price_acceptance": c.price_acceptance,
            "pullback_quality": c.pullback_quality,
            "reclaim_structure": c.reclaim_structure,
            "cross_session_persistence": c.cross_session_persistence,
            "trajectory_score": c.trajectory_score,
            "spread_pct": c.spread_pct,
            "liquidity_evolution": c.liquidity_evolution,
            "runner_personality": c.runner_personality,
            "runner_fatigue": c.runner_fatigue,
            "extension_risk": c.extension_risk,
            "evidence_convergence": c.evidence_convergence,
        },
    }


# ==============================================================================
# Startup
# ==============================================================================

def validate_environment() -> List[str]:
    missing = []
    if not ALPACA_API_KEY:
        missing.append("ALPACA_API_KEY / APCA_API_KEY_ID")
    if not ALPACA_SECRET_KEY:
        missing.append("ALPACA_SECRET_KEY / APCA_API_SECRET_KEY")
    if not UPSTASH_REDIS_REST_URL:
        missing.append("UPSTASH_REDIS_REST_URL")
    if not UPSTASH_REDIS_REST_TOKEN:
        missing.append("UPSTASH_REDIS_REST_TOKEN")
    if not GIST_ID:
        missing.append("GIST_ID")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        missing.append("Telegram env (alerts disabled)")
    return missing


def startup_message() -> str:
    missing = validate_environment()
    return (
        f"🚀 <b>{BOT_NAME_AR}</b>\n\n"
        f"✅ تم تشغيل البوت\n"
        f"🔧 Version: {VERSION}\n"
        f"🏗 Build: {BUILD}\n"
        f"🕒 {now_ksa().strftime('%Y-%m-%d %H:%M:%S')} KSA\n"
        f"🧭 Session: {current_market_session().value}\n"
        f"📡 Feed: {feed_for_phase(current_market_session()) if current_market_session() != SessionPhase.CLOSED else 'none'}\n"
        f"📰 News: Central Redis Consumer\n"
        f"📦 Float: Shared Gist Consumer\n"
        f"🎯 Entry Engine: {'Regular + Late PM' if ALLOW_PREMARKET_ENTRY else 'Regular only'}\n"
        f"🧪 Dry Run: {'ON' if DRY_RUN else 'OFF'}\n"
        f"🌙 BOATS: {'ON' if ENABLE_OVERNIGHT_BOATS else 'OFF'}\n"
        f"🚨 Live Entry Alerts: {'ON' if ENABLE_LIVE_ENTRY_ALERTS else 'OFF'}\n"
        + (f"\n⚠️ Missing env: {', '.join(missing)}" if missing else "")
    )


def main() -> None:
    with stats_lock:
        runtime_stats["started_at"] = iso()

    restore_state()
    load_float_cache()
    refresh_shared_news_cache()
    if not universe:
        rebuild_universe()

    update_runtime_counts()
    handle_session_transition(current_market_session())

    if STARTUP_SELF_TEST:
        results = run_startup_self_test()
        print("[SELF TEST]", json.dumps(results, ensure_ascii=False), flush=True)

    telegram_send(startup_message())

    worker = threading.Thread(
        target=coordinator_loop,
        name="ndr-coordinator",
        daemon=True,
    )
    worker.start()

    print(
        f"{BOT_NAME} v{VERSION} [{BUILD}] started | "
        f"session={current_market_session().value}",
        flush=True,
    )

    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
