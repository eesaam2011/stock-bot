import os
import time
import json
import requests
import pandas as pd
import alpaca_trade_api as tradeapi
from datetime import datetime, timedelta
import pytz

# ==========================================================
# ENV
# ==========================================================

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_FAST_CHAT_ID = os.getenv("TELEGRAM_FAST_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN_BOT2") or os.getenv("GITHUB_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)

saudi_tz = pytz.timezone("Asia/Riyadh")
ny_tz = pytz.timezone("America/New_York")

# ==========================================================
# SETTINGS
# ==========================================================

BOT_NAME_AR = "بوت التجميع الأسبوعي"

PRICE_MIN = 0.4
PRICE_MAX = 25.0

UNIVERSE_SIZE = 500
BATCH_SIZE = 200
SCAN_INTERVAL = 90

NEWS_REFRESH_INTERVAL = 600
NEWS_BATCH_LIMIT = 55

MIN_AVG_VOL = 150_000
MIN_DOLLAR_VOLUME = 500_000

ALERT_MIN_SCORE = 88
EXTENDED_TARGET_SCORE = 95

WEEKLY_REPORT_HOUR_ET = 20

# ==========================================================
# GIST FILES
# ==========================================================

WEEKLY_UNIVERSE_FILE = "weekly_universe.json"
INTERNAL_WATCHLIST_FILE = "internal_watchlist.json"
ACTIVE_TRADES_FILE = "active_trades.json"
SENT_ALERTS_FILE = "sent_alerts.json"
NEWS_CACHE_FILE = "news_cache.json"
HISTORICAL_WINNERS_FILE = "historical_winners.json"
MANUAL_BLACKLIST_FILE = "manual_blacklist.json"
MASTER_STATE_FILE = "master_state.json"
FLOAT_CACHE_FILE = "float_cache.json"

# ==========================================================
# GLOBAL STATE
# ==========================================================

weekly_universe = []
internal_watchlist = {}
active_trades = {}
sent_alerts = {}
news_cache = {}
historical_winners = {}
manual_blacklist = []
master_state = {}
float_cache = {}

# ==========================================================
# HELPERS
# ==========================================================

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def fmt_price(value):
    value = safe_float(value, 0)
    if value < 1:
        return f"{value:.4f}"
    if value < 10:
        return f"{value:.3f}"
    return f"{value:.2f}"


def now_saudi():
    return datetime.now(saudi_tz)


def now_ny():
    return datetime.now(ny_tz)


def today_key_sa():
    return now_saudi().strftime("%Y-%m-%d")


def today_key_ny():
    return now_ny().strftime("%Y-%m-%d")


def current_week_key():
    return now_saudi().strftime("%Y-W%U")


# ==========================================================
# TELEGRAM
# ==========================================================

def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_FAST_CHAT_ID:
        print("⚠️ Telegram env missing", flush=True)
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_FAST_CHAT_ID,
                "text": message
            },
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)


# ==========================================================
# GIST
# ==========================================================

def read_gist_file(filename, default):
    if not GIST_ID or not GITHUB_TOKEN:
        return default

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        r = requests.get(url, headers=headers, timeout=20)

        if r.status_code != 200:
            print(f"❌ Gist read failed {filename}: {r.status_code}", flush=True)
            return default

        data = r.json()
        file_data = data.get("files", {}).get(filename)

        if not file_data:
            return default

        content = file_data.get("content", "")
        if not content:
            return default

        return json.loads(content)

    except Exception as e:
        print(f"❌ Gist read error {filename}: {e}", flush=True)
        return default


def save_gist_file(filename, data):
    if not GIST_ID or not GITHUB_TOKEN:
        print("⚠️ Gist env missing", flush=True)
        return False

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        content = json.dumps(data, ensure_ascii=False)

        r = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    filename: {
                        "content": content
                    }
                }
            },
            timeout=25
        )

        if r.status_code not in [200, 201]:
            print(f"❌ Gist save failed {filename}: {r.text[:300]}", flush=True)
            return False

        print(f"✅ Saved {filename}", flush=True)
        return True

    except Exception as e:
        print(f"❌ Gist save error {filename}: {e}", flush=True)
        return False


def load_all_state():
    global weekly_universe, internal_watchlist, active_trades, sent_alerts
    global news_cache, historical_winners, manual_blacklist, master_state, float_cache

    weekly_universe = read_gist_file(WEEKLY_UNIVERSE_FILE, [])
    internal_watchlist = read_gist_file(INTERNAL_WATCHLIST_FILE, {})
    active_trades = read_gist_file(ACTIVE_TRADES_FILE, {})
    sent_alerts = read_gist_file(SENT_ALERTS_FILE, {})
    news_cache = read_gist_file(NEWS_CACHE_FILE, {})
    historical_winners = read_gist_file(HISTORICAL_WINNERS_FILE, {})
    manual_blacklist = read_gist_file(MANUAL_BLACKLIST_FILE, [])
    master_state = read_gist_file(MASTER_STATE_FILE, {})
    float_cache = read_gist_file(FLOAT_CACHE_FILE, {})

    if not isinstance(weekly_universe, list):
        weekly_universe = []
    if not isinstance(internal_watchlist, dict):
        internal_watchlist = {}
    if not isinstance(active_trades, dict):
        active_trades = {}
    if not isinstance(sent_alerts, dict):
        sent_alerts = {}
    if not isinstance(news_cache, dict):
        news_cache = {}
    if not isinstance(historical_winners, dict):
        historical_winners = {}
    if not isinstance(manual_blacklist, list):
        manual_blacklist = []
    if not isinstance(master_state, dict):
        master_state = {}
    if not isinstance(float_cache, dict):
        float_cache = {}

    print(
        f"✅ State loaded | Universe={len(weekly_universe)} | Active={len(active_trades)} | News={len(news_cache)}",
        flush=True
    )


def save_runtime_state():
    save_gist_file(INTERNAL_WATCHLIST_FILE, internal_watchlist)
    save_gist_file(ACTIVE_TRADES_FILE, active_trades)
    save_gist_file(SENT_ALERTS_FILE, sent_alerts)
    save_gist_file(MASTER_STATE_FILE, master_state)


# ==========================================================
# TIME RULES
# ==========================================================

def is_extended_market_time():
    n = now_ny()

    if n.weekday() >= 5:
        return False

    minutes = n.hour * 60 + n.minute
    return 4 * 60 <= minutes <= 20 * 60


def is_session_start_refresh_time():
    n = now_ny()

    if n.weekday() >= 5:
        return False

    minutes = n.hour * 60 + n.minute
    return 4 * 60 <= minutes <= 4 * 60 + 25


def is_weekend_build_day_saudi():
    n = now_saudi()

    # Saturday=5, Sunday=6
    return n.weekday() in [5, 6]


def is_friday_after_extended_close():
    n = now_ny()

    if n.weekday() != 4:
        return False

    minutes = n.hour * 60 + n.minute
    return minutes >= WEEKLY_REPORT_HOUR_ET * 60 + 5


# ==========================================================
# SYMBOL FILTERS
# ==========================================================

BAD_NAME_KEYWORDS = [
    "ETF", "ETN", "FUND", "TRUST", "INDEX",
    "WARRANT", "RIGHT", "UNIT", "PREFERRED",
    "NOTE", "BOND",
    "ACQUISITION", "BLANK CHECK", "SPAC",
    "ACQUISITION CORP"
]


def is_clean_symbol(symbol):
    if not symbol:
        return False

    symbol = str(symbol).upper().strip()

    if len(symbol) > 5:
        return False

    if not symbol.isalpha():
        return False

    if "." in symbol or "-" in symbol or "/" in symbol or "^" in symbol:
        return False

    if symbol in manual_blacklist:
        return False

    return True


def is_bad_asset_name(name):
    if not name:
        return False

    upper = str(name).upper()
    return any(k in upper for k in BAD_NAME_KEYWORDS)


# ==========================================================
# FLOAT
# ==========================================================

def get_float_shares(symbol):
    data = float_cache.get(symbol)

    if isinstance(data, dict):
        value = (
            data.get("float_shares")
            or data.get("float")
            or data.get("shares_float")
        )
    else:
        value = data

    value = safe_float(value, 0)

    if value <= 0:
        return None

    return value


def score_float(symbol):
    flt = get_float_shares(symbol)

    if flt is None:
        return 0, "غير معروف"

    if flt <= 5_000_000:
        return 10, f"{flt/1_000_000:.1f}M"
    if flt <= 15_000_000:
        return 8, f"{flt/1_000_000:.1f}M"
    if flt <= 30_000_000:
        return 5, f"{flt/1_000_000:.1f}M"

    return 0, f"{flt/1_000_000:.1f}M"


# ==========================================================
# NEWS
# ==========================================================

NEGATIVE_STRONG = [
    "reverse split", "stock split ratio", "offering",
    "public offering", "registered direct", "direct offering",
    "shelf offering", "atm offering", "bankruptcy",
    "chapter 11", "delisting", "nasdaq deficiency",
    "fda rejection", "complete response letter",
    "termination", "going concern"
]

NEGATIVE_MEDIUM = [
    "downgrade", "investigation", "lawsuit", "subpoena",
    "resignation", "delayed", "misses estimates",
    "withdraws guidance", "cuts guidance"
]

POSITIVE_STRONG = [
    "fda approval", "approval", "contract", "partnership",
    "merger", "acquisition", "strategic collaboration",
    "award", "grant", "positive results", "phase 2",
    "phase 3", "beats estimates", "record revenue"
]

POSITIVE_MEDIUM = [
    "launch", "expands", "agreement", "collaboration",
    "presentation", "patent", "update"
]


def classify_news(headline, age_hours):
    text = (headline or "").lower()

    if any(k in text for k in NEGATIVE_STRONG):
        return -15, "سلبي قوي"

    if any(k in text for k in NEGATIVE_MEDIUM):
        return -10, "سلبي متوسط"

    if any(k in text for k in POSITIVE_STRONG):
        if age_hours <= 1.5:
            return 15, "إيجابي حديث جدًا"
        return 10, "إيجابي قوي"

    if any(k in text for k in POSITIVE_MEDIUM):
        return 5, "إيجابي متوسط"

    return 0, "محايد"


def is_news_still_valid(news):
    try:
        published_ts = safe_float(news.get("published_ts"), 0)
        score = safe_float(news.get("news_score"), 0)

        if published_ts <= 0:
            return False

        age_hours = (time.time() - published_ts) / 3600

        if score < 0:
            return age_hours <= 72

        return age_hours <= 12

    except Exception:
        return False


def get_cached_news(symbol):
    item = news_cache.get(symbol)

    if not isinstance(item, dict):
        return {
            "news_score": 0,
            "news_type": "لا يوجد",
            "headline": "",
            "source": "",
            "age_hours": None
        }

    if not is_news_still_valid(item):
        return {
            "news_score": 0,
            "news_type": "منتهي",
            "headline": "",
            "source": "",
            "age_hours": None
        }

    return item


def refresh_news_cache():
    global news_cache, master_state

    if not FINNHUB_API_KEY:
        print("⚠️ FINNHUB_API_KEY missing", flush=True)
        return

    symbols = extract_symbols_from_universe()
    active_symbols = list(active_trades.keys())
    watch_symbols = list(internal_watchlist.keys())[:100]

    symbols = list(dict.fromkeys(active_symbols + watch_symbols + symbols))

    if not symbols:
        return

    last_index = int(master_state.get("news_refresh_index", 0) or 0)

    batch = symbols[last_index:last_index + NEWS_BATCH_LIMIT]

    next_index = last_index + NEWS_BATCH_LIMIT
    if next_index >= len(symbols):
        next_index = 0

    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=3)).isoformat()
    to_date = today.isoformat()

    updated = 0

    for symbol in batch:
        try:
            url = "https://finnhub.io/api/v1/company-news"
            params = {
                "symbol": symbol,
                "from": from_date,
                "to": to_date,
                "token": FINNHUB_API_KEY
            }

            r = requests.get(url, params=params, timeout=10)

            if r.status_code != 200:
                print(f"⚠️ Finnhub status {r.status_code} for {symbol}", flush=True)
                time.sleep(1.1)
                continue

            items = r.json()

            if not isinstance(items, list) or not items:
                time.sleep(1.1)
                continue

            best = None

            for item in items:
                headline = item.get("headline", "")
                source = item.get("source", "")
                dt = item.get("datetime", 0)

                if not dt:
                    continue

                age_hours = (time.time() - float(dt)) / 3600

                if age_hours < 0 or age_hours > 72:
                    continue

                score, news_type = classify_news(headline, age_hours)

                candidate = {
                    "symbol": symbol,
                    "headline": headline,
                    "source": source,
                    "url": item.get("url", ""),
                    "published_ts": float(dt),
                    "age_hours": round(age_hours, 2),
                    "news_score": score,
                    "news_type": news_type,
                    "updated_at": now_saudi().strftime("%Y-%m-%d %H:%M:%S")
                }

                if best is None:
                    best = candidate
                else:
                    if abs(score) > abs(best.get("news_score", 0)):
                        best = candidate
                    elif age_hours < best.get("age_hours", 999):
                        best = candidate

            if best:
                news_cache[symbol] = best
                updated += 1

            time.sleep(1.1)

        except Exception as e:
            print(f"News refresh error {symbol}: {e}", flush=True)
            time.sleep(1.1)
            continue

    master_state["news_refresh_index"] = next_index
    master_state["last_news_refresh"] = now_saudi().strftime("%Y-%m-%d %H:%M:%S")

    save_gist_file(NEWS_CACHE_FILE, news_cache)
    save_gist_file(MASTER_STATE_FILE, master_state)

    print(f"📰 News updated: {updated}/{len(batch)} | next={next_index}", flush=True)


# ==========================================================
# ALPACA BARS
# ==========================================================

def prepare_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

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

    return df[needed].dropna()


def normalize_bars_df(bars_df, symbols):
    result = {}

    if bars_df is None or bars_df.empty:
        return result

    try:
        df = bars_df.copy()

        if isinstance(df.index, pd.MultiIndex):
            if "symbol" in df.index.names:
                for symbol in symbols:
                    try:
                        sdf = df.xs(symbol, level="symbol").copy()
                        sdf = prepare_ohlcv(sdf)
                        if not sdf.empty:
                            result[symbol] = sdf
                    except Exception:
                        continue
                return result

        if "symbol" in df.columns:
            for symbol in symbols:
                try:
                    sdf = df[df["symbol"] == symbol].copy()
                    sdf = prepare_ohlcv(sdf)
                    if not sdf.empty:
                        result[symbol] = sdf
                except Exception:
                    continue
            return result

        if len(symbols) == 1:
            sdf = prepare_ohlcv(df)
            if not sdf.empty:
                result[symbols[0]] = sdf

    except Exception as e:
        print(f"normalize bars error: {e}", flush=True)

    return result


def get_bars_batch(symbols, timeframe, days_back=2, limit_tail=None):
    if not symbols:
        return {}

    try:
        end = datetime.now(pytz.UTC)
        start = end - timedelta(days=days_back)

        bars = api.get_bars(
            symbols,
            timeframe,
            start=start.isoformat(),
            end=end.isoformat(),
            adjustment="raw"
        ).df

        bars_map = normalize_bars_df(bars, symbols)

        if limit_tail:
            for s in list(bars_map.keys()):
                bars_map[s] = bars_map[s].tail(limit_tail)

        return bars_map

    except Exception as e:
        print(f"get_bars_batch error: {e}", flush=True)
        return {}


def get_minute_bars_for_symbols(symbols):
    all_bars = {}

    for i in range(0, len(symbols), BATCH_SIZE):
        chunk = symbols[i:i + BATCH_SIZE]

        bars_map = get_bars_batch(
            chunk,
            tradeapi.TimeFrame.Minute,
            days_back=2,
            limit_tail=1000
        )

        all_bars.update(bars_map)

        print(
            f"📦 Minute bars batch {i // BATCH_SIZE + 1}: {len(bars_map)}/{len(chunk)}",
            flush=True
        )

        time.sleep(1)

    return all_bars


def get_daily_bars_for_symbols(symbols):
    all_bars = {}

    for i in range(0, len(symbols), BATCH_SIZE):
        chunk = symbols[i:i + BATCH_SIZE]

        bars_map = get_bars_batch(
            chunk,
            tradeapi.TimeFrame.Day,
            days_back=120,
            limit_tail=90
        )

        all_bars.update(bars_map)

        print(
            f"📦 Daily bars batch {i // BATCH_SIZE + 1}: {len(bars_map)}/{len(chunk)}",
            flush=True
        )

        time.sleep(1)

    return all_bars


# ==========================================================
# UNIVERSE BUILD / REFRESH
# ==========================================================

def get_all_clean_assets():
    try:
        assets = api.list_assets(status="active")
    except Exception as e:
        print(f"Assets error: {e}", flush=True)
        return []

    rows = []

    for a in assets:
        try:
            symbol = str(a.symbol).upper().strip()
            name = getattr(a, "name", "") or ""

            if not getattr(a, "tradable", False):
                continue

            if not is_clean_symbol(symbol):
                continue

            if is_bad_asset_name(name):
                continue

            rows.append({
                "symbol": symbol,
                "name": name
            })

        except Exception:
            continue

    print(f"✅ Clean assets: {len(rows)}", flush=True)
    return rows


def calculate_universe_score(symbol, df):
    if df is None or df.empty or len(df) < 25:
        return None

    try:
        cp = float(df["Close"].iloc[-1])

        if not (PRICE_MIN <= cp <= PRICE_MAX):
            return None

        avg_vol_20 = float(df["Volume"].tail(20).mean())
        dollar_volume = cp * avg_vol_20

        if avg_vol_20 < MIN_AVG_VOL:
            return None

        if dollar_volume < MIN_DOLLAR_VOLUME:
            return None

        high_20 = float(df["High"].tail(20).max())
        low_20 = float(df["Low"].tail(20).min())

        if low_20 <= 0:
            return None

        historical_range_pct = ((high_20 - low_20) / low_20) * 100

        avg_vol_base = float(df["Volume"].tail(60).mean())
        recent_vol = float(df["Volume"].tail(5).mean())
        historical_rvol = recent_vol / max(avg_vol_base, 1)

        float_points, float_text = score_float(symbol)

        float_score = min(float_points * 3.5, 35)

        if historical_range_pct >= 100:
            range_score = 25
        elif historical_range_pct >= 60:
            range_score = 20
        elif historical_range_pct >= 35:
            range_score = 15
        elif historical_range_pct >= 20:
            range_score = 8
        else:
            range_score = 0

        if historical_rvol >= 4:
            rvol_score = 25
        elif historical_rvol >= 3:
            rvol_score = 20
        elif historical_rvol >= 2:
            rvol_score = 15
        elif historical_rvol >= 1.3:
            rvol_score = 8
        else:
            rvol_score = 0

        if dollar_volume >= 5_000_000:
            liquidity_score = 15
        elif dollar_volume >= 2_000_000:
            liquidity_score = 12
        elif dollar_volume >= 1_000_000:
            liquidity_score = 8
        else:
            liquidity_score = 4

        score = float_score + range_score + rvol_score + liquidity_score

        return {
            "symbol": symbol,
            "price": round(cp, 4),
            "float": float_text,
            "avg_vol_20": int(avg_vol_20),
            "dollar_volume": int(dollar_volume),
            "historical_range_pct": round(historical_range_pct, 2),
            "historical_rvol": round(historical_rvol, 2),
            "universe_score": round(score, 2),
            "updated_at": now_saudi().strftime("%Y-%m-%d %H:%M:%S")
        }

    except Exception as e:
        print(f"Universe score error {symbol}: {e}", flush=True)
        return None


def build_weekly_universe(reason):
    global weekly_universe, master_state

    print(f"🔄 Building universe | reason={reason}", flush=True)

    assets = get_all_clean_assets()
    symbols = [x["symbol"] for x in assets]

    if not symbols:
        print("⚠️ No clean symbols found", flush=True)
        return

    daily_bars = get_daily_bars_for_symbols(symbols)

    results = []

    for symbol in symbols:
        r = calculate_universe_score(symbol, daily_bars.get(symbol))
        if r:
            results.append(r)

    results = sorted(
        results,
        key=lambda x: x.get("universe_score", 0),
        reverse=True
    )

    weekly_universe = results[:UNIVERSE_SIZE]

    master_state["last_universe_build"] = now_saudi().strftime("%Y-%m-%d %H:%M:%S")
    master_state["last_universe_reason"] = reason
    master_state["week_key"] = current_week_key()
    master_state["universe_count"] = len(weekly_universe)

    save_gist_file(WEEKLY_UNIVERSE_FILE, weekly_universe)
    save_gist_file(MASTER_STATE_FILE, master_state)

    send_telegram_msg(
        f"✅ {BOT_NAME_AR}\n\n"
        f"تم تحديث قائمة الـ500.\n"
        f"عدد الأسهم: {len(weekly_universe)}\n"
        f"السبب: {reason}"
    )

    print(f"✅ Universe saved: {len(weekly_universe)}", flush=True)


def extract_symbols_from_universe():
    symbols = []

    for item in weekly_universe:
        if isinstance(item, str):
            symbol = item
        elif isinstance(item, dict):
            symbol = item.get("symbol")
        else:
            continue

        if symbol and is_clean_symbol(symbol):
            symbols.append(symbol.upper().strip())

    return list(dict.fromkeys(symbols))[:UNIVERSE_SIZE]


def maybe_build_or_refresh_universe():
    global master_state

    # 1) البناء الأسبوعي فقط السبت والأحد بتوقيت السعودية
    if is_weekend_build_day_saudi():
        key = f"weekend_build_{today_key_sa()}"

        if not master_state.get(key):
            build_weekly_universe("weekend_build_sat_sun")
            master_state[key] = True
            save_gist_file(MASTER_STATE_FILE, master_state)
        return

    # 2) Refresh فقط مع بداية التداول الممتد بتوقيت نيويورك
    if is_session_start_refresh_time():
        key = f"session_start_refresh_{today_key_ny()}"

        if not master_state.get(key):
            build_weekly_universe("session_start_refresh")
            master_state[key] = True
            save_gist_file(MASTER_STATE_FILE, master_state)
        return

    # 3) لا يوجد بناء عشوائي خارج السبت/الأحد أو بداية التداول الممتد
    if not weekly_universe:
        print(
            "⚠️ weekly_universe فارغ، لكن لن يتم بناؤه الآن لأن البناء محصور في السبت/الأحد أو بداية التداول الممتد.",
            flush=True
        )


# ==========================================================
# INDICATORS
# ==========================================================

def calculate_session_vwap(df):
    try:
        if df is None or df.empty:
            return None

        if not isinstance(df.index, pd.DatetimeIndex):
            print("⚠️ Session VWAP fallback: index is not DatetimeIndex", flush=True)
            return None

        idx = df.index

        if idx.tz is None:
            idx = idx.tz_localize("UTC")

        idx_ny = idx.tz_convert(ny_tz)

        session_start = datetime.now(ny_tz).replace(
            hour=4,
            minute=0,
            second=0,
            microsecond=0
        )

        session_df = df[idx_ny >= session_start]

        if session_df.empty:
            print("⚠️ Session VWAP fallback: no bars after 4AM NY", flush=True)
            return None

        vol_sum = session_df["Volume"].sum()

        if vol_sum <= 0:
            print("⚠️ Session VWAP fallback: zero volume", flush=True)
            return None

        return float(
            (session_df["Close"] * session_df["Volume"]).sum()
            / vol_sum
        )

    except Exception as e:
        print(f"⚠️ Session VWAP fallback: {e}", flush=True)
        return None
        
def calculate_obv(df):
    obv = [0]

    closes = df["Close"].tolist()
    volumes = df["Volume"].tolist()

    for i in range(1, len(df)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])

    return pd.Series(obv, index=df.index)


def calculate_atr(df, period=14):
    if df is None or df.empty or len(df) < period + 1:
        return 0

    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    value = tr.rolling(period).mean().iloc[-1]
    return safe_float(value, 0)


def candle_stats(df):
    last_open = float(df["Open"].iloc[-1])
    last_close = float(df["Close"].iloc[-1])
    last_high = float(df["High"].iloc[-1])
    last_low = float(df["Low"].iloc[-1])

    candle_range = last_high - last_low

    if candle_range <= 0:
        return 0.5, 0.5, 0

    close_position = (last_close - last_low) / candle_range
    upper_wick_pct = (last_high - last_close) / candle_range
    body_ratio = abs(last_close - last_open) / candle_range

    return close_position, upper_wick_pct, body_ratio


# ==========================================================
# TRADE PLAN / EXTENDED TARGET
# ==========================================================

def calculate_trade_plan(entry, atr, df):
    if not atr or atr <= 0:
        atr = entry * 0.04

    target_1 = max(entry + atr, entry * 1.06)
    target_2 = max(entry + atr * 2, entry * 1.12)
    target_3 = max(entry + atr * 3, entry * 1.20)

    stop_loss = min(entry - atr * 1.2, entry * 0.92)

    try:
        recent_low = float(df["Low"].tail(20).min())
        stop_loss = max(stop_loss, recent_low * 0.98)
    except Exception:
        pass

    if stop_loss >= entry:
        stop_loss = entry * 0.92

    return {
        "entry": round(entry, 4),
        "target_1": round(target_1, 4),
        "target_2": round(target_2, 4),
        "target_3": round(target_3, 4),
        "stop_loss": round(stop_loss, 4)
    }


def calculate_extended_target(
    symbol,
    price,
    score,
    news,
    instant_rvol,
    accel_value,
    real_breakout,
    df
):
    try:
        if score < EXTENDED_TARGET_SCORE:
            return None

        news_score = int(news.get("news_score", 0) or 0)

        if instant_rvol < 5:
            return None

        if accel_value < 2.5:
            return None

        if not real_breakout:
            return None

        if news_score < 0:
            return None

        recent_high = float(df["High"].tail(160).max())

        base_target = max(recent_high * 1.5, price * 2.5)

        if score >= 98 and instant_rvol >= 8 and accel_value >= 3:
            high_target = max(base_target, price * 6)
        else:
            high_target = max(base_target, price * 4)

        low_target = max(price * 2, high_target * 0.55)

        return {
            "low": round(low_target, 4),
            "high": round(high_target, 4),
            "reason": "Score عالي + RVOL قوي + تسارع فوليوم + اختراق حقيقي"
        }

    except Exception:
        return None


def evaluate_extended_target_during_monitor(symbol, trade, df):
    extended = trade.get("extended_target")

    if not isinstance(extended, dict):
        return ""

    try:
        cp = float(df["Close"].iloc[-1])
        highest = float(trade.get("highest_price", cp))

        instant_rvol = float(df["Volume"].tail(3).mean() / max(df["Volume"].mean(), 1))

        obv = calculate_obv(df)
        obv_ema = obv.ewm(span=10, adjust=False).mean()
        obv_positive = bool(obv.iloc[-1] > obv_ema.iloc[-1])

        if instant_rvol >= 3 and obv_positive and cp >= highest * 0.92:
            return (
                f"\n🔥 متابعة الهدف الممتد:\n"
                f"الزخم ما زال يدعم الهدف الممتد {fmt_price(extended.get('low'))} - {fmt_price(extended.get('high'))}\n"
            )

        return (
            f"\n⚠️ متابعة الهدف الممتد:\n"
            f"الزخم تراجع، الهدف الممتد يحتاج استمرار فوليوم أقوى.\n"
        )

    except Exception:
        return ""


# ==========================================================
# ALERT ANALYSIS
# ==========================================================

def analyze_symbol_for_alert(symbol, df):
    try:
        if df is None or df.empty or len(df) < 50:
            return None

        df = df.copy()

        cp = float(df["Close"].iloc[-1])

        if not (PRICE_MIN <= cp <= PRICE_MAX):
            return None

        vwap = calculate_session_vwap(df)

        if vwap is None:
            vwap = float(
                (df["Close"] * df["Volume"]).sum()
                / max(df["Volume"].sum(), 1)
            )
    
        df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
        df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

        ema9 = float(df["EMA9"].iloc[-1])
        ema20 = float(df["EMA20"].iloc[-1])

        obv = calculate_obv(df)
        obv_ema = obv.ewm(span=10, adjust=False).mean()
        obv_positive = bool(obv.iloc[-1] > obv_ema.iloc[-1])

        instant_rvol = float(df["Volume"].tail(3).mean() / max(df["Volume"].mean(), 1))

        last_1m_vs_avg = float(df["Volume"].iloc[-1] / max(df["Volume"].tail(30).mean(), 1))
        last_3m_vs_prev_7m = float(
            df["Volume"].tail(3).mean()
            / max(df["Volume"].tail(10).head(7).mean(), 1)
        )

        accel_value = max(last_1m_vs_avg, last_3m_vs_prev_7m)

        volume_acceleration = (
            last_1m_vs_avg >= 1.8
            or last_3m_vs_prev_7m >= 1.6
        )

        recent_move = float(((cp - df["Close"].iloc[-10]) / df["Close"].iloc[-10]) * 100)
        move_3m = float(((cp - df["Close"].iloc[-3]) / df["Close"].iloc[-3]) * 100)
        move_5m = float(((cp - df["Close"].iloc[-5]) / df["Close"].iloc[-5]) * 100)

        close_position, upper_wick_pct, body_ratio = candle_stats(df)

        recent_resistance = float(df["High"].iloc[:-1].tail(80).max())

        real_breakout = (
            float(df["Close"].iloc[-1]) > recent_resistance * 1.002
            and instant_rvol >= 2.2
        )

        near_breakout = cp >= recent_resistance * 0.995

        above_vwap = cp > vwap
        above_ema = cp > ema9 and ema9 >= ema20 * 0.995

        distribution_score = 0

        if upper_wick_pct >= 0.45 and close_position < 0.55:
            distribution_score += 15

        if instant_rvol >= 3 and recent_move < 0.5:
            distribution_score += 10

        if volume_acceleration and body_ratio < 0.25:
            distribution_score += 10

        overextended_bad = (
            recent_move >= 8
            and move_3m < 0.2
            and upper_wick_pct >= 0.35
        )

        # Hard Rejects - لا تشمل الفلوت ولا الأخبار
        if not above_vwap:
            return None

        if not obv_positive:
            return None

        if not volume_acceleration:
            return None

        if distribution_score >= 25:
            return None

        if overextended_bad:
            return None

        if close_position < 0.58:
            return None

        if upper_wick_pct > 0.45:
            return None

        if not (real_breakout or near_breakout):
            return None

        float_points, float_text = score_float(symbol)

        news = get_cached_news(symbol)
        news_score = int(news.get("news_score", 0) or 0)

        obv_score = 20 if obv_positive else 0

        if instant_rvol >= 5:
            rvol_score = 15
        elif instant_rvol >= 4:
            rvol_score = 12
        elif instant_rvol >= 3:
            rvol_score = 9
        elif instant_rvol >= 2:
            rvol_score = 5
        else:
            rvol_score = 0

        if accel_value >= 3:
            accel_score = 20
        elif accel_value >= 2.3:
            accel_score = 16
        elif accel_value >= 1.8:
            accel_score = 12
        elif accel_value >= 1.5:
            accel_score = 8
        else:
            accel_score = 0

        breakout_score = 10 if real_breakout else 5
        vwap_ema_score = 5 if above_vwap and above_ema else 3 if above_vwap else 0
        close_score = 5 if close_position >= 0.75 else 3 if close_position >= 0.65 else 0

        total_score = (
            float_points
            + news_score
            + obv_score
            + rvol_score
            + accel_score
            + breakout_score
            + vwap_ema_score
            + close_score
        )

        total_score = max(0, min(100, total_score))

        if total_score < ALERT_MIN_SCORE:
            internal_watchlist[symbol] = {
                "symbol": symbol,
                "score": round(total_score, 2),
                "price": round(cp, 4),
                "reason": "مرشح داخلي فقط",
                "updated_at": now_saudi().strftime("%Y-%m-%d %H:%M:%S")
            }
            return None

        atr = calculate_atr(df)
        plan = calculate_trade_plan(cp, atr, df)

        extended = calculate_extended_target(
            symbol=symbol,
            price=cp,
            score=total_score,
            news=news,
            instant_rvol=instant_rvol,
            accel_value=accel_value,
            real_breakout=real_breakout,
            df=df
        )

        return {
            "symbol": symbol,
            "price": round(cp, 4),
            "score": round(total_score, 2),
            "float": float_text,
            "float_score": float_points,
            "news_score": news_score,
            "news_type": news.get("news_type", "لا يوجد"),
            "headline": news.get("headline", ""),
            "news_age_hours": news.get("age_hours"),
            "obv_score": obv_score,
            "rvol_score": rvol_score,
            "accel_score": accel_score,
            "breakout_score": breakout_score,
            "vwap_ema_score": vwap_ema_score,
            "close_score": close_score,
            "instant_rvol": round(instant_rvol, 2),
            "acceleration": round(accel_value, 2),
            "recent_move": round(recent_move, 2),
            "move_3m": round(move_3m, 2),
            "move_5m": round(move_5m, 2),
            "vwap": round(vwap, 4),
            "ema9": round(ema9, 4),
            "ema20": round(ema20, 4),
            "close_position": round(close_position, 2),
            "upper_wick_pct": round(upper_wick_pct, 2),
            "distribution_score": distribution_score,
            "real_breakout": real_breakout,
            "near_breakout": near_breakout,
            "entry": plan["entry"],
            "target_1": plan["target_1"],
            "target_2": plan["target_2"],
            "target_3": plan["target_3"],
            "stop_loss": plan["stop_loss"],
            "extended_target": extended,
            "time": time.time(),
            "created_at": now_saudi().strftime("%Y-%m-%d %H:%M:%S")
        }

    except Exception as e:
        print(f"Analyze alert error {symbol}: {e}", flush=True)
        return None


# ==========================================================
# DUPLICATE / ALERT
# ==========================================================

def is_symbol_blocked(symbol):
    if symbol in active_trades:
        return True

    item = sent_alerts.get(symbol)

    if not isinstance(item, dict):
        return False

    cooldown_until = safe_float(item.get("cooldown_until"), 0)

    if time.time() < cooldown_until:
        return True

    return False


def send_entry_alert(signal):
    symbol = signal["symbol"]

    if is_symbol_blocked(symbol):
        return

    extended = signal.get("extended_target")

    extended_text = ""

    if isinstance(extended, dict):
        extended_text = (
            f"\n🔥 الهدف الممتد المحتمل:\n"
            f"النطاق: {fmt_price(extended.get('low'))} - {fmt_price(extended.get('high'))}\n"
            f"السبب: {extended.get('reason')}\n"
            f"⚠️ مشروط باستمرار الفوليوم وعدم كسر الوقف المتحرك.\n"
        )

    headline = signal.get("headline", "")

    if headline:
        news_line = f"{signal.get('news_type')} | {headline[:120]}"
    else:
        news_line = signal.get("news_type", "لا يوجد")

    msg = (
        f"🚀 {BOT_NAME_AR} - دخول مؤكد\n\n"
        f"🎫 السهم: {symbol}\n"
        f"💰 الدخول: {fmt_price(signal.get('entry'))}\n"
        f"📊 السكور: {signal.get('score'):.1f}/100\n\n"
        f"🎯 الهدف 1: {fmt_price(signal.get('target_1'))}\n"
        f"🎯 الهدف 2: {fmt_price(signal.get('target_2'))}\n"
        f"🎯 الهدف 3: {fmt_price(signal.get('target_3'))}\n"
        f"🛑 وقف الخسارة: {fmt_price(signal.get('stop_loss'))}\n"
        f"{extended_text}\n"
        f"📌 أهم الأسباب:\n"
        f"Float: {signal.get('float')} | نقاطه: {signal.get('float_score')}\n"
        f"الأخبار: {news_line} | نقاطها: {signal.get('news_score')}\n"
        f"OBV: إيجابي\n"
        f"RVOL: {signal.get('instant_rvol')}x\n"
        f"تسارع الفوليوم: {signal.get('acceleration')}x\n"
        f"اختراق حقيقي: {signal.get('real_breakout')}\n"
        f"Close Position: {signal.get('close_position')}\n"
        f"Distribution Score: {signal.get('distribution_score')}\n\n"
        f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
    )

    send_telegram_msg(msg)

    active_trades[symbol] = {
        "symbol": symbol,
        "entry": signal.get("entry"),
        "current_stop": signal.get("stop_loss"),
        "initial_stop": signal.get("stop_loss"),
        "target_1": signal.get("target_1"),
        "target_2": signal.get("target_2"),
        "target_3": signal.get("target_3"),
        "extended_target": signal.get("extended_target"),
        "highest_price": signal.get("entry"),
        "status": "ACTIVE",
        "t1_hit": False,
        "t2_hit": False,
        "t3_hit": False,
        "stop_alerted": False,
        "created_ts": time.time(),
        "created_at": now_saudi().strftime("%Y-%m-%d %H:%M:%S"),
        "signal": signal
    }

    sent_alerts[symbol] = {
        "last_alert_ts": time.time(),
        "cooldown_until": 0,
        "status": "ACTIVE"
    }

    save_runtime_state()

    print(f"📩 Entry alert sent: {symbol} | score={signal.get('score')}", flush=True)


# ==========================================================
# SCAN
# ==========================================================

def scan_weekly_universe():
    symbols = extract_symbols_from_universe()

    if not symbols:
        print("⚠️ Weekly universe empty", flush=True)
        return

    bars_map = get_minute_bars_for_symbols(symbols)

    found = 0

    for symbol in symbols:
        signal = analyze_symbol_for_alert(symbol, bars_map.get(symbol))

        if signal:
            found += 1
            send_entry_alert(signal)

    save_gist_file(INTERNAL_WATCHLIST_FILE, internal_watchlist)

    print(f"🔎 Scan done | symbols={len(symbols)} | alerts={found}", flush=True)


# ==========================================================
# MONITOR ACTIVE TRADES
# ==========================================================

def update_historical_winner(symbol, trade, highest, gain_pct):
    if gain_pct < 50:
        return

    level = "+50%"

    if gain_pct >= 200:
        level = "+200%"
    elif gain_pct >= 100:
        level = "+100%"

    historical_winners[symbol] = {
        "symbol": symbol,
        "level": level,
        "entry": trade.get("entry"),
        "highest_price": round(highest, 4),
        "gain_pct": round(gain_pct, 2),
        "float": trade.get("signal", {}).get("float"),
        "news_type": trade.get("signal", {}).get("news_type"),
        "headline": trade.get("signal", {}).get("headline"),
        "rvol": trade.get("signal", {}).get("instant_rvol"),
        "acceleration": trade.get("signal", {}).get("acceleration"),
        "score": trade.get("signal", {}).get("score"),
        "extended_target": trade.get("extended_target"),
        "updated_at": now_saudi().strftime("%Y-%m-%d %H:%M:%S")
    }


def monitor_active_trades():
    global active_trades, historical_winners

    symbols = list(active_trades.keys())

    if not symbols:
        return

    bars_map = get_minute_bars_for_symbols(symbols)

    for symbol in symbols:
        try:
            trade = active_trades.get(symbol)
            df = bars_map.get(symbol)

            if not trade or df is None or df.empty or len(df) < 20:
                continue

            cp = float(df["Close"].iloc[-1])
            entry = safe_float(trade.get("entry"), 0)
            current_stop = safe_float(trade.get("current_stop"), 0)
            t1 = safe_float(trade.get("target_1"), 0)
            t2 = safe_float(trade.get("target_2"), 0)
            t3 = safe_float(trade.get("target_3"), 0)

            if entry <= 0 or current_stop <= 0:
                continue

            highest = max(safe_float(trade.get("highest_price"), entry), cp)
            trade["highest_price"] = highest

            gain_pct = ((highest - entry) / entry) * 100

            df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
            ema9 = float(df["EMA9"].iloc[-1])

            vwap = float((df["Close"] * df["Volume"]).sum() / max(df["Volume"].sum(), 1))

            obv = calculate_obv(df)
            obv_ema = obv.ewm(span=10, adjust=False).mean()
            obv_positive = bool(obv.iloc[-1] > obv_ema.iloc[-1])

            instant_rvol = float(df["Volume"].tail(3).mean() / max(df["Volume"].mean(), 1))

            new_stop = current_stop
            stop_reason = ""

            if cp >= t1 and not trade.get("t1_hit"):
                trade["t1_hit"] = True
                new_stop = max(new_stop, entry)
                stop_reason = "بعد الهدف الأول: رفع الوقف إلى الدخول"

            if cp >= t2 and not trade.get("t2_hit"):
                trade["t2_hit"] = True
                recent_low = float(df["Low"].tail(10).min())
                new_stop = max(new_stop, recent_low)
                stop_reason = "بعد الهدف الثاني: رفع الوقف إلى آخر قاع"

            if cp >= t3:
                if not trade.get("t3_hit"):
                    trade["t3_hit"] = True

                trailing = highest * 0.94
                new_stop = max(new_stop, trailing)
                stop_reason = "بعد الهدف الثالث: تفعيل الوقف المتحرك"

            if new_stop > current_stop * 1.003:
                old_stop = current_stop
                trade["current_stop"] = round(new_stop, 4)

                extended_text = evaluate_extended_target_during_monitor(symbol, trade, df)

                msg = (
                    f"🔒 {BOT_NAME_AR} - رفع الوقف\n\n"
                    f"🎫 السهم: {symbol}\n"
                    f"💰 السعر الحالي: {fmt_price(cp)}\n"
                    f"🚀 الدخول: {fmt_price(entry)}\n"
                    f"📈 أعلى سعر: {fmt_price(highest)}\n"
                    f"📊 الربح من القمة: {gain_pct:.2f}%\n\n"
                    f"🛑 الوقف السابق: {fmt_price(old_stop)}\n"
                    f"✅ الوقف الجديد: {fmt_price(new_stop)}\n"
                    f"📌 السبب: {stop_reason}\n"
                    f"{extended_text}"
                )

                send_telegram_msg(msg)

            if cp <= safe_float(trade.get("current_stop"), current_stop) and not trade.get("stop_alerted"):
                trade["stop_alerted"] = True
                trade["status"] = "STOPPED_BUT_MONITORING"

                msg = (
                    f"🛑 {BOT_NAME_AR} - كسر الوقف\n\n"
                    f"🎫 السهم: {symbol}\n"
                    f"💰 السعر الحالي: {fmt_price(cp)}\n"
                    f"🚀 الدخول: {fmt_price(entry)}\n"
                    f"🛑 الوقف: {fmt_price(trade.get('current_stop'))}\n\n"
                    f"📌 سيبقى السهم تحت المراقبة حتى نهاية اليوم لاحتمال عودة الزخم."
                )

                send_telegram_msg(msg)

            update_historical_winner(symbol, trade, highest, gain_pct)

            trade["last_price"] = round(cp, 4)
            trade["last_rvol"] = round(instant_rvol, 2)
            trade["above_vwap"] = bool(cp > vwap)
            trade["above_ema9"] = bool(cp > ema9)
            trade["obv_positive"] = obv_positive
            trade["updated_at"] = now_saudi().strftime("%Y-%m-%d %H:%M:%S")

            active_trades[symbol] = trade

        except Exception as e:
            print(f"Monitor error {symbol}: {e}", flush=True)

    save_runtime_state()
    save_gist_file(HISTORICAL_WINNERS_FILE, historical_winners)


# ==========================================================
# END OF DAY / REPORT / CLEAR
# ==========================================================

def cleanup_end_of_day():
    global active_trades, sent_alerts

    if is_extended_market_time():
        return

    key = f"cleanup_{today_key_sa()}"

    if master_state.get(key):
        return

    for symbol, trade in list(active_trades.items()):
        keep = (
            safe_float(trade.get("last_rvol"), 0) >= 2
            and bool(trade.get("above_vwap", False))
            and bool(trade.get("obv_positive", False))
        )

        if keep:
            trade["carried_next_day"] = True
            active_trades[symbol] = trade
            continue

        active_trades.pop(symbol, None)

        sent_alerts[symbol] = {
            "last_alert_ts": time.time(),
            "cooldown_until": time.time() + 3 * 3600,
            "status": "COOLDOWN_AFTER_MONITOR_EXIT"
        }

    master_state[key] = True
    save_runtime_state()


def build_top_missed_movers_text():
    try:
        symbols = extract_symbols_from_universe()

        if not symbols:
            return "لا توجد قائمة أسبوعية لحساب الفوائت.\n"

        bars_map = get_daily_bars_for_symbols(symbols)

        missed = []
        alerted_symbols = set(sent_alerts.keys())

        for symbol, df in bars_map.items():
            if df is None or df.empty or len(df) < 5:
                continue

            week_low = float(df["Low"].tail(5).min())
            week_high = float(df["High"].tail(5).max())

            if week_low <= 0:
                continue

            move = ((week_high - week_low) / week_low) * 100

            if move >= 100 and symbol not in alerted_symbols:
                missed.append({
                    "symbol": symbol,
                    "move": round(move, 2),
                    "reason": "داخل قائمة 500 لكن لم يصل لشروط التنبيه"
                })

        missed = sorted(missed, key=lambda x: x["move"], reverse=True)[:10]

        if not missed:
            return "لا يوجد أسهم 100%+ فائتة داخل القائمة.\n"

        text = ""

        for m in missed:
            text += f"- {m['symbol']} | {m['move']}% | {m['reason']}\n"

        return text

    except Exception as e:
        print(f"Missed movers report error: {e}", flush=True)
        return "تعذر حساب الأسهم الفائتة هذا الأسبوع.\n"


def send_weekly_report_if_due():
    if not is_friday_after_extended_close():
        return

    key = f"weekly_report_{current_week_key()}"

    if master_state.get(key):
        return

    winners = list(historical_winners.values())

    winners = sorted(
        winners,
        key=lambda x: x.get("gain_pct", 0),
        reverse=True
    )

    top = winners[:10]

    top_text = ""

    for i, w in enumerate(top, start=1):
        top_text += (
            f"{i}) {w.get('symbol')} | {w.get('gain_pct')}% | "
            f"Score {w.get('score')} | {w.get('news_type')}\n"
        )

    if not top_text:
        top_text = "لا يوجد رابحين +50% مسجلين هذا الأسبوع.\n"

    missed_text = build_top_missed_movers_text()

    msg = (
        f"📊 تقرير {BOT_NAME_AR} الأسبوعي\n\n"
        f"🏆 أفضل الرابحين:\n"
        f"{top_text}\n"
        f"\n🔎 Top Missed Movers:\n"
        f"{missed_text}\n"
        f"\n📌 عدد الأسهم في القائمة الأسبوعية: {len(weekly_universe)}\n"
        f"📌 عدد الأسهم تحت المراقبة: {len(active_trades)}"
    )

    send_telegram_msg(msg)

    master_state[key] = True
    save_gist_file(MASTER_STATE_FILE, master_state)


def clear_weekly_universe_after_friday_close():
    global weekly_universe, internal_watchlist

    if not is_friday_after_extended_close():
        return

    key = f"friday_clear_{current_week_key()}"

    if master_state.get(key):
        return

    weekly_universe = []
    internal_watchlist = {}

    master_state[key] = True
    master_state["week_key"] = ""
    master_state["universe_count"] = 0
    master_state["last_clear"] = now_saudi().strftime("%Y-%m-%d %H:%M:%S")

    save_gist_file(WEEKLY_UNIVERSE_FILE, weekly_universe)
    save_gist_file(INTERNAL_WATCHLIST_FILE, internal_watchlist)
    save_gist_file(MASTER_STATE_FILE, master_state)

    send_telegram_msg(
        f"🧹 {BOT_NAME_AR}\n\n"
        f"تم حذف قائمة الأسبوع بعد نهاية يوم الجمعة.\n"
        f"سيتم بناء قائمة الأسبوع الجديد يوم السبت/الأحد."
    )


# ==========================================================
# MAIN
# ==========================================================

def main():
    load_all_state()

    send_telegram_msg(
        f"✅ تم تشغيل {BOT_NAME_AR}\n"
        f"الملف: weekly_radar_bot.py\n"
        f"الوضع: Background Worker"
    )

    last_news_refresh = 0

    while True:
        try:
            maybe_build_or_refresh_universe()

            if time.time() - last_news_refresh >= NEWS_REFRESH_INTERVAL:
                refresh_news_cache()
                last_news_refresh = time.time()

            cleanup_end_of_day()
            send_weekly_report_if_due()
            clear_weekly_universe_after_friday_close()

            if is_extended_market_time():
                scan_weekly_universe()
                monitor_active_trades()
            else:
                print("⏸️ خارج وقت التداول الممتد - انتظار", flush=True)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"Main loop error: {e}", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
