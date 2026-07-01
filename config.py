# config.py
# -*- coding: utf-8 -*-

# =========================================================
# BOT INFO
# =========================================================
BOT_NAME = "🚀 Elite Explosion Entry Bot"

TIMEZONE_KSA = "Asia/Riyadh"
TIMEZONE_NY = "America/New_York"


# =========================================================
# DAILY SCHEDULE - KSA
# =========================================================
WORK_START_KSA = "11:00"
WORK_END_KSA = "03:00"

FLOAT_LOAD_TIME_KSA = "11:00"
UNIVERSE_BUILD_TIME_KSA = "11:00"

RESTORE_STATE_ON_STARTUP = True
RESTORE_ACTIVE_MONITORING = True


# =========================================================
# INTERVALS
# =========================================================
MAIN_SCAN_INTERVAL = 60
MONITOR_INTERVAL = 30
NEWS_QUEUE_INTERVAL = 60
FULL_UNIVERSE_REFRESH_MIN = 30


# =========================================================
# PRICE / LIQUIDITY
# =========================================================
PRICE_MIN = 0.30
PRICE_MAX = 25.00

MIN_AVG_VOLUME = 50_000
MIN_DOLLAR_VOLUME = 500_000
MAX_SPREAD_PCT = 2.5


# =========================================================
# ENTRY SCORE
# =========================================================
ENTRY_MIN_SCORE = 90
REPEAT_BLOCK_HOURS = 12

SCORE_WEIGHTS = {
    "rvol": 25,
    "volume_acceleration": 25,
    "price_change": 15,
    "breakout": 15,
    "obv": 10,
    "float_quality": 5,
    "liquidity": 5,
}


# =========================================================
# SESSION PROFILES
# =========================================================
SESSION_PROFILES = {
    "PREMARKET": {
        "min_score": 90,
        "min_rvol": 3.0,
        "min_price_change_pct": 5.0,
        "min_volume_acceleration": 1.8,
        "max_spread_pct": 2.5,
        "min_dollar_volume": 500_000,
        "require_above_vwap": True,
        "reject_one_candle_spike": True,
    },

    "REGULAR": {
        "min_score": 90,
        "min_rvol": 3.0,
        "min_price_change_pct": 5.0,
        "min_volume_acceleration": 1.6,
        "max_spread_pct": 2.0,
        "min_dollar_volume": 750_000,
        "require_above_vwap": True,
        "reject_one_candle_spike": True,
    },

    "POWER_HOUR": {
        "min_score": 95,
        "min_rvol": 3.8,
        "min_price_change_pct": 6.0,
        "min_volume_acceleration": 2.0,
        "max_spread_pct": 1.5,
        "min_dollar_volume": 1_000_000,
        "require_above_vwap": True,
        "reject_one_candle_spike": True,
        "require_sustained_breakout": True,
    },

    "AFTER_HOURS": {
        "min_score": 94,
        "min_rvol": 3.5,
        "min_price_change_pct": 6.0,
        "min_volume_acceleration": 2.0,
        "max_spread_pct": 1.5,
        "min_dollar_volume": 750_000,
        "require_above_vwap": True,
        "reject_one_candle_spike": True,
    },
}


# =========================================================
# FLOAT
# =========================================================
FLOAT_CACHE_URL = ""

FLOAT_BONUS_TIERS = [
    (5_000_000, 20),
    (15_000_000, 15),
    (30_000_000, 10),
    (60_000_000, 5),
]

MISSING_FLOAT_BONUS = 0
MISSING_FLOAT_REJECT = False


# =========================================================
# NEWS
# =========================================================
NEWS_LOOKBACK_HOURS = 12
NEWS_CACHE_TTL = 60 * 60

SERIOUS_NEGATIVE_REJECT_HOURS = 72
POSITIVE_NEWS_BONUS_HOURS = 12

NEWS_REQUESTS_PER_MINUTE = 40
NEWS_ACTIVE_QUEUE_CAP = 500
NEWS_ACTIVE_MONITOR_REFRESH_MINUTES = 10

SERIOUS_NEGATIVE_KEYWORDS = [
    "reverse split",
    "offering",
    "direct offering",
    "registered direct",
    "atm offering",
    "delisting",
    "nasdaq notice",
    "bankruptcy",
    "investigation",
    "sec subpoena",
    "going concern",
    "dilution",
]

MEDIUM_NEGATIVE_KEYWORDS = [
    "lawsuit",
    "class action",
    "termination",
    "resignation",
    "delay",
    "withdrawal",
    "non-compliance",
]

POSITIVE_CATALYST_KEYWORDS = [
    "fda approval",
    "approval",
    "contract",
    "purchase order",
    "merger",
    "acquisition",
    "buyout",
    "partnership",
    "earnings beat",
    "positive data",
    "phase 3",
    "government contract",
]


# =========================================================
# ACTIVITY FILTER BEFORE NEWS
# =========================================================
ACTIVITY_FILTER = {
    "min_rvol": 2.0,
    "min_price_change_pct": 3.0,
    "min_dollar_volume": 300_000,
    "min_activity_score": 60,
    "cap": 500,
}


# =========================================================
# MONITORING / TARGETS
# =========================================================
ATR_PERIOD = 14
STOP_ATR_MULTIPLIER = 1.5

TARGETS = {
    "T1_ATR": 1.0,
    "T2_ATR": 2.0,
    "T3_ATR": 3.0,
}

MAX_MONITOR_MINUTES = 120
TRAILING_STOP_PCT = 1.5

EXIT_ON_LOSE_VWAP = True
EXIT_ON_OBV_WEAKNESS = True
EXIT_ON_VOLUME_COLLAPSE = True


# =========================================================
# REDIS
# =========================================================
REDIS_PREFIX = "elite_explosion:"

REDIS_KEYS = {
    "state": REDIS_PREFIX + "state",
    "active_monitoring": REDIS_PREFIX + "active_monitoring",
    "sent_alerts": REDIS_PREFIX + "sent_alerts",
    "priority_universe": REDIS_PREFIX + "priority_universe",
    "score_history": REDIS_PREFIX + "score_history",
    "news_cache": REDIS_PREFIX + "news_cache",
    "runtime_stats": REDIS_PREFIX + "runtime_stats",
    "daily_statistics": REDIS_PREFIX + "daily_statistics",
}


# =========================================================
# BLACKLIST / SHARIA
# =========================================================
SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "PNC", "TFC",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR",
    "BUD", "TAP", "STZ", "DEO", "PM", "MO", "BTI",
    "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL",
}

BAD_NAME_KEYWORDS = [
    "bank",
    "bancorp",
    "financial",
    "finance",
    "insurance",
    "credit",
    "loan",
    "mortgage",
    "casino",
    "gaming",
    "betting",
    "alcohol",
    "tobacco",
    "cannabis",
    "marijuana",
    "reit",
    "fund",
    "etf",
    "trust",
    "warrant",
    "unit",
    "right",
    "acquisition",
    "blank check",
    "spac",
    "preferred",
    "note",
]


# =========================================================
# SYMBOL CLEANING
# =========================================================
MAX_SYMBOL_LENGTH = 5
ALLOW_ONLY_ALPHA_SYMBOLS = True

BAD_SYMBOL_CHARS = [".", "-", "^", "/"]

BAD_SYMBOL_SUFFIXES = [
    "W", "U", "R", "P", "Q", "Z"
]


# =========================================================
# TELEGRAM ALERT TITLES
# =========================================================
TELEGRAM_ENTRY_TITLE = "🚀 دخول انفجار مبكر"
TELEGRAM_T1_TITLE = "🎯 تحقق الهدف الأول"
TELEGRAM_T2_TITLE = "🎯 تحقق الهدف الثاني"
TELEGRAM_T3_TITLE = "🎯 تحقق الهدف الثالث"
TELEGRAM_MOMENTUM_TITLE = "🔥 استمرار الزخم"
TELEGRAM_EXIT_TITLE = "🛑 خروج / ضعف الحركة"


# =========================================================
# DEPLOYMENT
# =========================================================
RUN_AS_BACKGROUND_WORKER = True
ENABLE_FLASK_STATUS_PAGE = False
