import os
import time
from datetime import datetime, timedelta
import threading
import requests
import alpaca_trade_api as tradeapi
from flask import Flask
import zoneinfo
import pytz

saudi_tz = zoneinfo.ZoneInfo("Asia/Riyadh")

app = Flask(__name__)

total_scans_performed = 0
last_scan_timestamp = "Never"

@app.route('/')
def home():
    global total_scans_performed, last_scan_timestamp
    status_msg = (
        f"⚡ Early Explosion Radar is Running Perfectly 24/7!<br>"
        f"📊 Total Market Scans: {total_scans_performed}<br>"
        f"⏱️ Last Scan Time: {last_scan_timestamp}"
    )
    return status_msg, 200

def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

ALPACA_API_KEY      = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET_KEY   = os.getenv("APCA_API_SECRET_KEY")
ALPACA_BASE_URL     = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_INVESTMENT_CHAT_ID = os.getenv(
    "TELEGRAM_INVESTMENT_CHAT_ID"
)

GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
GIST_ID             = os.getenv("GIST_ID")

PRICE_MIN          = 0.3
PRICE_MAX          = 25.0
MIN_AVG_VOL        = 50_000
MAX_AVG_VOL        = 5_000_000
MIN_DOLLAR_VOLUME  = 300_000

RVOL_MIN           = 1.8
MIN_PRICE_CHANGE   = 4.0

MOMENTUM_RVOL_MIN             = 1.2
MOMENTUM_PRICE_CHANGE_MIN     = 3.0
EXPLOSION_CANDIDATE_MIN_SCORE = 85

BATCH_SIZE         = 250
BATCH_DELAY_SEC    = 1.0

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

active_monitors = {}
sent_alerts = {}
radar_watchlist = {}
float_cache = {}
news_cache = {}
NEWS_CACHE_MINUTES = 10
NEWS_LOOKBACK_HOURS = 12

FLOAT_CACHE_HOUR = 8
FLOAT_CACHE_MINUTE = 15
FINNHUB_DELAY_SEC = 1.05
last_float_cache_date = None
float_cache_building = False
RADAR_TRIGGER_CHANGE_PCT = 4.0
RADAR_MIN_DOLLAR_VOLUME = 100_000
RADAR_EXPIRE_MINUTES = 30
gist_lock = threading.Lock()
session_closed_reports = []
report_lock = threading.Lock()
final_session_report_sent = False
last_session_date = None
reject_rvol = 0
reject_resistance = 0
reject_score = 0
reject_price_change = 0
reject_history = 0
reject_price = 0
reject_avg_vol = 0
reject_dollar_volume = 0
reject_blacklist = 0
reject_bad_name = 0
reject_bars = 0
reject_prev_bars = 0

SCAN_INTERVAL_SEC  = 180
TRACK_INTERVAL_SEC = 10
ALERT_COOLDOWN_SEC = 3600

FLOAT_CACHE_FILE = "float_cache.json"


def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_INVESTMENT_CHAT_ID:
        print(f"[Telegram-Sim] {text}", flush=True)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_INVESTMENT_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
    except Exception as e:
        print(f"❌ Telegram Error: {e}", flush=True)

def update_gist_state(symbol, data_dict):
    if not GITHUB_TOKEN or not GIST_ID:
        return

    with gist_lock:

        url = f"https://api.github.com/gists/{GIST_ID}"

        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }

        try:
            res = requests.get(
                url,
                headers=headers,
                timeout=10
            )

            current_content = {}

            if res.status_code == 200:
                files = res.json().get("files", {})

                if "bot_state.json" in files:
                    import json

                    try:
                        current_content = json.loads(
                            files["bot_state.json"]["content"]
                        )
                    except:
                        current_content = {}

            current_content[symbol] = data_dict

            import json

            payload = {
                "files": {
                    "bot_state.json": {
                        "content": json.dumps(
                            current_content,
                            indent=4
                        )
                    }
                }
            }

            requests.patch(
                url,
                headers=headers,
                json=payload,
                timeout=10
            )

        except Exception as e:
            print(
                f"❌ Gist Update Error: {e}",
                flush=True
            )

def update_gist_file(filename, content):
    if not GITHUB_TOKEN or not GIST_ID:
        return

    with gist_lock:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }

        try:
            payload = {
                "files": {
                    filename: {
                        "content": content
                    }
                }
            }

            requests.patch(
                url,
                headers=headers,
                json=payload,
                timeout=10
            )

        except Exception as e:
            print(
                f"❌ Gist File Update Error ({filename}): {e}",
                flush=True
            )
            
def is_scan_time_allowed():
    tz_ny = pytz.timezone("America/New_York")
    now_ny = datetime.now(tz_ny)

    if now_ny.weekday() >= 5:
        return False

    start_time = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
    end_time = now_ny.replace(hour=20, minute=0, second=0, microsecond=0)

    return start_time <= now_ny <= end_time

def get_float_tier(avg_vol_20):
    if avg_vol_20 <= 150_000:
        return "ULTRA_LOW_FLOAT"
    elif avg_vol_20 <= 500_000:
        return "VERY_LOW_FLOAT"
    elif avg_vol_20 <= 1_000_000:
        return "LOW_FLOAT"
    return "NORMAL_FLOAT"

def calculate_atr_14(df):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = (
        (high - low)
        .to_frame("hl")
        .join((high - close.shift(1)).abs().rename("hc"))
        .join((low - close.shift(1)).abs().rename("lc"))
    ).max(axis=1)

    return float(tr.tail(14).mean())

def calculate_obv_bonus(bars_1m):
    if bars_1m is None:
        return 0, "UNKNOWN_OBV"

    if bars_1m.empty:
        return 0, "UNKNOWN_OBV"

    if len(bars_1m) < 10:
        return 0, "UNKNOWN_OBV"

    bars_1m = bars_1m.sort_index()

    obv = 0
    obv_history = []

    closes = bars_1m["close"].tolist()
    volumes = bars_1m["volume"].tolist()

    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]

        obv_history.append(obv)

    if len(obv_history) < 5:
        return 0, "UNKNOWN_OBV"

    recent_obv = obv_history[-1]
    older_obv = obv_history[-5]

    obv_growth = recent_obv - older_obv

    if obv_growth > 0 and recent_obv > 0:
        return 10, "STRONG_OBV"

    if obv_growth > 0:
        return 5, "POSITIVE_OBV"

    return 0, "WEAK_OBV"
    
def fetch_finnhub_float(symbol):
    if not FINNHUB_API_KEY:
        return None

    url = "https://finnhub.io/api/v1/stock/profile2"

    try:
        res = requests.get(
            url,
            params={
                "symbol": symbol,
                "token": FINNHUB_API_KEY
            },
            timeout=10
        )

        if res.status_code != 200:
            return None

        data = res.json()

        floating_share = data.get("floatingShare")

        if floating_share is None:
            return None

        real_float = float(floating_share) * 1_000_000

        return real_float

    except Exception:
        return None

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

def load_float_cache():
    global float_cache
    global last_float_cache_date

    try:
        import json

        today_key = datetime.now(
            saudi_tz
        ).strftime("%Y-%m-%d")

        if GITHUB_TOKEN and GIST_ID:
            url = f"https://api.github.com/gists/{GIST_ID}"
            headers = {
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            }

            res = requests.get(
                url,
                headers=headers,
                timeout=10
            )

            if res.status_code == 200:
                files = res.json().get("files", {})

                if FLOAT_CACHE_FILE in files:
                    float_cache = json.loads(
                        files[FLOAT_CACHE_FILE]["content"]
                    )

                    if any(
                        data.get("updated") == today_key
                        for data in float_cache.values()
                        if isinstance(data, dict)
                    ):
                        last_float_cache_date = today_key

                    print(
                        f"🧬 Float cache loaded from Gist | Total={len(float_cache)}",
                        flush=True
                    )
                    return

        if not os.path.exists(FLOAT_CACHE_FILE):
            float_cache = {}
            return

        with open(FLOAT_CACHE_FILE, "r") as f:
            float_cache = json.load(f)

        if any(
            data.get("updated") == today_key
            for data in float_cache.values()
            if isinstance(data, dict)
        ):
            last_float_cache_date = today_key

        print(
            f"🧬 Float cache loaded locally | Total={len(float_cache)}",
            flush=True
        )

    except Exception as e:
        print(
            f"⚠️ Float cache load error: {e}",
            flush=True
        )
        float_cache = {}

def save_float_cache():
    try:
        import json

        content = json.dumps(
            float_cache,
            indent=4
        )

        with open(FLOAT_CACHE_FILE, "w") as f:
            f.write(content)

        update_gist_file(
            FLOAT_CACHE_FILE,
            content
        )

    except Exception as e:
        print(
            f"⚠️ Float cache save error: {e}",
            flush=True
        )
        
def analyze_news_sentiment(text):
    text = (text or "").lower()

    positive_keywords = [
        "approval", "approved", "fda", "contract", "partnership",
        "acquisition", "merger", "patent", "launch", "breakthrough",
        "grant", "agreement", "collaboration", "positive", "expands"
    ]

    negative_keywords = [
        "offering", "dilution", "bankruptcy", "delisting", "lawsuit",
        "investigation", "fraud", "warning", "delay", "halt",
        "chapter 11", "public offering", "registered direct"
    ]

    positive_hits = sum(1 for kw in positive_keywords if kw in text)
    negative_hits = sum(1 for kw in negative_keywords if kw in text)

    if negative_hits > positive_hits:
        return -10, "NEGATIVE"

    if positive_hits > negative_hits:
        return 10, "POSITIVE"

    return 0, "NEUTRAL"


def get_recent_finnhub_news(symbol):
    if not FINNHUB_API_KEY:
        return None

    now_ts = time.time()
    cached = news_cache.get(symbol)

    if cached and now_ts - cached.get("checked_at", 0) < NEWS_CACHE_MINUTES * 60:
        return cached.get("news")

    now_ksa = datetime.now(saudi_tz)
    from_date = (now_ksa - timedelta(hours=NEWS_LOOKBACK_HOURS)).strftime("%Y-%m-%d")
    to_date = now_ksa.strftime("%Y-%m-%d")

    try:
        res = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": from_date,
                "to": to_date,
                "token": FINNHUB_API_KEY
            },
            timeout=10
        )

        if res.status_code != 200:
            return None

        news_list = res.json()

        if not news_list:
            news_cache[symbol] = {
                "checked_at": now_ts,
                "news": None
            }
            return None

        newest = sorted(
            news_list,
            key=lambda x: x.get("datetime", 0),
            reverse=True
        )[0]

        news_time = newest.get("datetime", 0)
        age_hours = (now_ts - news_time) / 3600

        if age_hours > NEWS_LOOKBACK_HOURS:
            return None

        news_cache[symbol] = {
            "checked_at": now_ts,
            "news": newest
        }

        return newest

    except Exception:
        return None


def send_news_after_alert(alert):
    symbol = alert["symbol"]
    base_score = alert.get("score", 0)

    news = get_recent_finnhub_news(symbol)

    if not news:
        return

    headline = news.get("headline", "")
    summary = news.get("summary", "")
    url = news.get("url", "")
    source = news.get("source", "Unknown")
    news_time = news.get("datetime", 0)

    age_hours = round((time.time() - news_time) / 3600, 2)

    text_for_sentiment = f"{headline} {summary}"
    news_score, sentiment = analyze_news_sentiment(text_for_sentiment)

    adjusted_score = base_score + news_score

    if sentiment == "POSITIVE":
        sentiment_label = "🟢 إيجابي"
        score_label = f"➕ News Bonus: +{news_score}"
    elif sentiment == "NEGATIVE":
        sentiment_label = "🔴 سلبي"
        score_label = f"➖ News Penalty: {news_score}"
    else:
        sentiment_label = "⚪ محايد"
        score_label = "➕ News Bonus: 0"

    msg = (
        f"📰 *محفز إخباري بعد التنبيه*\n\n"
        f"🎫 *السهم:* `{symbol}`\n"
        f"🏷️ *المصدر:* `{source}`\n"
        f"⏰ *عمر الخبر:* `{age_hours}` ساعة\n"
        f"{sentiment_label}\n"
        f"{score_label}\n\n"
        f"📈 *Score قبل الخبر:* `{base_score}/100`\n"
        f"🚀 *Score بعد الخبر:* `{adjusted_score}/100`\n\n"
        f"📝 *العنوان:*\n{headline}\n\n"
        f"🔗 {url}"
    )

    send_telegram_message(msg)
    
def should_build_float_cache():
    global last_float_cache_date

    now_ksa = datetime.now(saudi_tz)
    today_key = now_ksa.strftime("%Y-%m-%d")

    if last_float_cache_date == today_key:
        return False

    if now_ksa.hour == FLOAT_CACHE_HOUR and now_ksa.minute >= FLOAT_CACHE_MINUTE:
        return True

    return False

def build_float_cache_for_assets(assets):
    global last_float_cache_date
    global float_cache_building

    if float_cache_building:
        return

    if not should_build_float_cache():
        return

    float_cache_building = True

    print("🧬 Starting Finnhub float cache build...", flush=True)
    
    loaded = 0
    skipped = 0

    for asset in assets:
        symbol = asset.symbol

        if symbol in float_cache:
            skipped += 1
            continue

        if symbol in SYMBOL_BLACKLIST:
            skipped += 1
            continue

        asset_name = getattr(asset, "name", "") or ""

        if any(kw in asset_name.lower() for kw in BAD_NAME_KEYWORDS):
            skipped += 1
            continue

        real_float = fetch_finnhub_float(symbol)

        if real_float is not None:
            float_cache[symbol] = {
                "float": real_float,
                "updated": datetime.now(saudi_tz).strftime("%Y-%m-%d")
            }
            loaded += 1

        save_float_cache()

        time.sleep(FINNHUB_DELAY_SEC)

    last_float_cache_date = datetime.now(
        saudi_tz
    ).strftime("%Y-%m-%d")

    float_cache_building = False

    print(
        f"✅ Float cache build finished | Loaded={loaded} | Skipped={skipped} | Total={len(float_cache)}",
        flush=True
    )
    
def update_radar_watchlist(symbol, current_price, prev_close, today_vol):
    
    now_ts = time.time()

    if prev_close <= 0:
        return False

    change_pct = ((current_price - prev_close) / prev_close) * 100
    
    dollar_volume = current_price * today_vol
    
    if change_pct < RADAR_TRIGGER_CHANGE_PCT:
        return False

    if dollar_volume < RADAR_MIN_DOLLAR_VOLUME:
        return False

    existing = radar_watchlist.get(symbol, {})

    previous_gain = existing.get(
        "last_gain",
        change_pct
    )

    gain_trend = (
        change_pct - previous_gain
    )

    radar_watchlist[symbol] = {
        "first_seen": existing.get(
            "first_seen",
            now_ts
        ),
        "last_seen": now_ts,
        "highest_gain": max(
            existing.get(
                "highest_gain",
                change_pct
            ),
            change_pct
        ),
        "highest_dollar_volume": max(
            existing.get(
                "highest_dollar_volume",
                dollar_volume
            ),
            dollar_volume
        ),
        "last_gain": change_pct,
        "gain_trend": gain_trend
    }

    return True

def clean_radar_watchlist():
    now_ts = time.time()
    expired = []

    for symbol, data in radar_watchlist.items():
        first_seen = data.get("first_seen", now_ts)

        if now_ts - first_seen > RADAR_EXPIRE_MINUTES * 60:
            expired.append(symbol)

    for symbol in expired:
        radar_watchlist.pop(symbol, None)

def quick_radar_check(api, symbol):
    try:
        snapshot = api.get_snapshot(symbol)

        if not snapshot:
            return False

        if snapshot.latest_trade:
            current_price = float(
                snapshot.latest_trade.price
            )
        else:
            trade = api.get_latest_trade(symbol)
            current_price = float(
                trade.price
            )

        prev_daily_bar = getattr(
            snapshot,
            "previous_daily_bar",
            None
        )

        if prev_daily_bar is None:
            prev_daily_bar = getattr(
                snapshot,
                "prev_daily_bar",
                None
            )

        if prev_daily_bar is None:
            return False

        prev_close = float(
            prev_daily_bar.close
        )

        if snapshot.daily_bar:
            today_vol = float(
                snapshot.daily_bar.volume
            )
        else:
            return False

        if not (PRICE_MIN <= current_price <= PRICE_MAX):
            return False

        return update_radar_watchlist(
            symbol,
            current_price,
            prev_close,
            today_vol
        )

    except Exception:
        return False
        
def check_explosion(api, symbol, asset_name):
    global reject_price_change
    global reject_rvol
    global reject_resistance
    global reject_score
    global reject_history
    global reject_price
    global reject_avg_vol
    global reject_dollar_volume
    global reject_blacklist
    global reject_bad_name
    global reject_bars
    global reject_prev_bars
    
    if symbol in SYMBOL_BLACKLIST:
        reject_blacklist += 1
        return None

    name_lower = asset_name.lower()
    if any(kw in name_lower for kw in BAD_NAME_KEYWORDS):
        reject_bad_name += 1
        return None

    try:
        tz_ny = pytz.timezone("America/New_York")
        end_dt = datetime.now(tz_ny)
        start_dt = end_dt - timedelta(days=120)

        bars = api.get_bars(
            symbol,
            tradeapi.rest.TimeFrame.Day,
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            limit=60,
            adjustment="raw",
            feed="iex"
        ).df

        if bars is None:
            reject_bars += 1
            return None

        if bars.empty:
            reject_bars += 1
            return None

        if len(bars) < 3:
            reject_prev_bars += 1
            return None

        bars = bars.sort_index()

        today_bar = bars.iloc[-1]
        previous_bars = bars.iloc[:-1]

        if len(previous_bars) < 2:
            reject_prev_bars += 1
            return None

        trade = api.get_latest_trade(
            symbol
        )

        snapshot = api.get_snapshot(
            symbol
        )

        current_price = float(
            trade.price
        )

        if snapshot and snapshot.daily_bar:
            today_vol = float(
                snapshot.daily_bar.volume
            )
        else:
            today_vol = float(
                today_bar["volume"]
            )

        if snapshot and snapshot.prev_daily_bar:
            prev_close = float(
                snapshot.prev_daily_bar.close
            )
        else:
            prev_close = float(
                previous_bars["close"].iloc[-1]
            )

        price_change_pct = (
            (current_price - prev_close)
            / prev_close
        ) * 100

        radar_data = radar_watchlist.get(
            symbol,
            {}
        )

        gain_trend = radar_data.get(
            "gain_trend",
            0
        )
        
        if len(previous_bars) < 20:
            reject_history += 1
            return None
            
        if not (PRICE_MIN <= current_price <= PRICE_MAX):
            reject_price += 1
            return None

        avg_vol_20 = float(previous_bars["volume"].tail(20).mean())

        float_info = float_cache.get(symbol)
        real_float = None

        if float_info:
            real_float = float_info.get("float")

        float_bonus, float_tier = get_float_bonus(real_float)
        

        if avg_vol_20 < MIN_AVG_VOL or avg_vol_20 > MAX_AVG_VOL:
            reject_avg_vol += 1
            return None

        resistance_20 = float(previous_bars["high"].tail(20).max())
        resistance_50 = float(previous_bars["high"].tail(50).max())

        atr_14 = calculate_atr_14(previous_bars)

        global reject_price_change

        if price_change_pct < MIN_PRICE_CHANGE:
            reject_price_change += 1
            return None

        rvol = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0

        global reject_rvol

        if rvol < RVOL_MIN:
            reject_rvol += 1
            return None

        dollar_volume = today_vol * current_price

        if dollar_volume < MIN_DOLLAR_VOLUME:
            reject_dollar_volume += 1
            return None

        global reject_resistance

        if current_price < resistance_20 * 0.99:
            reject_resistance += 1
            return None

        bars_1m = api.get_bars(
            symbol,
            tradeapi.rest.TimeFrame.Minute,
            limit=10,
            adjustment="raw"
        ).df

        vol_acceleration = 1.0

        if (
            bars_1m is not None
            and not bars_1m.empty
            and len(bars_1m) >= 8
        ):
            bars_1m = bars_1m.sort_index()

            recent_vol = float(bars_1m["volume"].tail(2).mean())
            previous_vol = float(bars_1m["volume"].iloc[-6:-2].mean())

            if previous_vol > 0:
                vol_acceleration = recent_vol / previous_vol

        obv_bonus, obv_tier = calculate_obv_bonus(
            bars_1m
        )

        score = 0

        score += float_bonus
        score += obv_bonus

        if rvol >= 3.0:
            score += 30
        elif rvol >= RVOL_MIN:
            score += 20

        if price_change_pct >= 15:
            score += 30
        elif price_change_pct >= 8:
            score += 20
        elif price_change_pct >= MIN_PRICE_CHANGE:
            score += 10

        if current_price >= resistance_20:
            score += 20

        elif current_price >= resistance_20 * 0.99:
            score += 10

        if vol_acceleration >= 3.0:
            score += 20
        elif vol_acceleration >= 2.0:
            score += 15
        elif vol_acceleration >= 1.5:
            score += 10
        elif vol_acceleration >= 1.0:
            score += 5

        if dollar_volume >= 1_000_000:
            score += 10
        elif dollar_volume >= MIN_DOLLAR_VOLUME:
            score += 5

        if gain_trend >= 1.0:
            score += 15

        elif gain_trend >= 0.5:
            score += 10

        elif gain_trend > 0:
            score += 5

        if gain_trend <= 0:
            reject_score += 1
            return None

        if vol_acceleration < 1.0 and obv_bonus == 0:
            reject_score += 1
            return None

        if score < EXPLOSION_CANDIDATE_MIN_SCORE:
            reject_score += 1
            return None


        digits = 4 if current_price < 1 else 2

        stop_loss = round(current_price * 0.93, digits)

        target1 = round(current_price + atr_14, digits)
        target2 = round(current_price + (atr_14 * 2), digits)
        target3 = round(max(resistance_50, current_price + (atr_14 * 3)), digits)

        return {
            "symbol": symbol,
            "price": round(current_price, digits),
            "rvol": round(rvol, 2),
            "change_pct": round(price_change_pct, 2),
            "score": score,
            "float_tier": float_tier,
            "real_float": round(real_float, 0) if real_float else None,
            "float_bonus": float_bonus,
            "obv_bonus": obv_bonus,
            "obv_tier": obv_tier,
            "resistance_20": round(resistance_20, digits),
            "atr_14": round(atr_14, digits),
            "resistance_50": round(resistance_50, digits),
            "vol_acceleration": round(vol_acceleration, 2),
            "dollar_volume": round(dollar_volume, 0),
            "target1": target1,
            "target2": target2,
            "target3": target3,
            "stop_loss": stop_loss,
            "explosion_candidate": True
        }

    except Exception as e:
        print(f"❌ check_explosion error {symbol}: {e}", flush=True)
        return None
        
def send_final_session_report_if_ready():
    global final_session_report_sent

    with report_lock:
        if final_session_report_sent:
            return

        if active_monitors:
            return

        if not session_closed_reports:
            return

        msg = "📋 *تقرير نهاية مراقبة الجلسة - Early Explosion*\n\n"

        for r in session_closed_reports:
            msg += (
                f"🎫 *{r['symbol']}*\n"
                f"• الدخول: ${r['entry_price']}\n"
                f"• أعلى ربح: {r['max_gain']}%\n"
                f"• T1: {'✅' if r['h1_hit'] else '❌'} | "
                f"T2: {'✅' if r['h2_hit'] else '❌'} | "
                f"T3: {'✅' if r['h3_hit'] else '❌'}\n"
                f"• زخم قوي: {'✅' if r['strong_momentum_sent'] else '❌'}\n"
                f"• ضعف زخم: {'⚠️' if r['weak_momentum_sent'] else '❌'}\n\n"
            )

        send_telegram_message(msg)
        final_session_report_sent = True
        
def dedicated_ticker_tracker(symbol, entry_price, t1, t2, t3, sl):
    print(f"🎯 [بدء المراقبة اللحظية الشرسة] خيط مستقل انطلق لملاحقة سهم: {symbol}", flush=True)

    api = tradeapi.REST(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
        ALPACA_BASE_URL,
        api_version='v2'
    )

    h1_hit, h2_hit, h3_hit = False, False, False
    strong_momentum_sent = False
    weak_momentum_sent = False
    max_gain_pct = 0.0
    failed_attempts = 0
    MAX_FAILED_ATTEMPTS = 20

    update_gist_state(symbol, {
        "entry_price": entry_price,
        "t1": t1,
        "t2": t2,
        "t3": t3,
        "sl": sl,
        "h1_hit": h1_hit,
        "h2_hit": h2_hit,
        "h3_hit": h3_hit,
        "max_gain": 0.0,
        "status": "active"
    })

    while True:
        if not is_scan_time_allowed():
            print(f"💤 [إيقاف المراقبة] خروج مؤقت لسهم {symbol} بسبب إغلاق الجلسة.", flush=True)

            update_gist_state(symbol, {
                "status": "session_closed",
                "max_gain": max_gain_pct,
                "h1_hit": h1_hit,
                "h2_hit": h2_hit,
                "h3_hit": h3_hit,
                "strong_momentum_sent": strong_momentum_sent,
                "weak_momentum_sent": weak_momentum_sent
            })

            with report_lock:
                session_closed_reports.append({
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "max_gain": max_gain_pct,
                    "h1_hit": h1_hit,
                    "h2_hit": h2_hit,
                    "h3_hit": h3_hit,
                    "strong_momentum_sent": strong_momentum_sent,
                    "weak_momentum_sent": weak_momentum_sent
                })

            break

        try:
            trade = api.get_latest_trade(symbol)

            current_p = trade.price

            failed_attempts = 0

            current_gain = (
                (current_p - entry_price)
                / entry_price
            ) * 100

            if current_gain > max_gain_pct:
                max_gain_pct = round(current_gain, 2)

            momentum_score = 50

            if current_gain >= 10:
                momentum_score += 15
            elif current_gain >= 5:
                momentum_score += 10

            if current_p > entry_price:
                momentum_score += 10

            try:
                bars_1m = api.get_bars(
                    symbol,
                    tradeapi.rest.TimeFrame.Minute,
                    limit=10,
                    adjustment="raw"
                ).df

                if bars_1m is not None and not bars_1m.empty and len(bars_1m) >= 5:
                    bars_1m = bars_1m.sort_index()

                    recent_high = float(bars_1m["high"].max())
                    recent_vol = float(bars_1m["volume"].tail(2).mean())
                    previous_vol = float(bars_1m["volume"].iloc[-6:-2].mean())

                    near_recent_high = current_p >= recent_high * 0.97

                    if near_recent_high:
                        momentum_score += 15

                    if previous_vol > 0 and recent_vol >= previous_vol * 1.5:
                        momentum_score += 20

            except Exception as e:
                failed_attempts += 1

                print(
                    f"⚠️ Error tracking {symbol}: {e}",
                    flush=True
                )

                if failed_attempts >= MAX_FAILED_ATTEMPTS:
                    send_telegram_message(
                        f"⚠️ تم إيقاف مراقبة {symbol} بسبب تعذر جلب البيانات لفترة طويلة."
                    )

                    update_gist_state(
                        symbol,
                        {
                            "status": "monitor_failed",
                            "max_gain": max_gain_pct,
                            "h1_hit": h1_hit,
                            "h2_hit": h2_hit,
                            "h3_hit": h3_hit,
                            "strong_momentum_sent": strong_momentum_sent,
                            "weak_momentum_sent": weak_momentum_sent
                        }
                    )

                    break
            momentum_score = min(momentum_score, 100)

            if momentum_score >= 85 and not strong_momentum_sent:
                strong_momentum_sent = True
                msg = (
                    f"🔥 *[{symbol}] الزخم يتسارع بقوة!*\n"
                    f"• السعر الحالي: ${current_p}\n"
                    f"• الربح الحالي: {round(current_gain, 2)}%\n"
                    f"• Momentum Score: {momentum_score}/100\n"
                    f"• السهم لا يزال يظهر إشارات استمرار."
                )
                send_telegram_message(msg)

            if momentum_score <= 45 and current_gain > 0 and not weak_momentum_sent:
                weak_momentum_sent = True
                msg = (
                    f"⚠️ *[{symbol}] تحذير ضعف زخم*\n"
                    f"• السعر الحالي: ${current_p}\n"
                    f"• الربح الحالي: {round(current_gain, 2)}%\n"
                    f"• Momentum Score: {momentum_score}/100\n"
                    f"• الزخم بدأ يضعف، راقب السهم بحذر."
                )
                send_telegram_message(msg)

            if current_p <= sl:
                msg = (
                    f"🚨 *[{symbol}] ضرب وقف الخسارة!* 🚨\n"
                    f"• سعر الخروج: ${current_p}\n"
                    f"• خسارة: {round(current_gain, 2)}%\n"
                    f"• أعلى ربح وصل له: {max_gain_pct}%"
                )
                send_telegram_message(msg)
                update_gist_state(symbol, {
                    "status": "stopped_by_sl",
                    "exit_price": current_p,
                    "max_gain": max_gain_pct,
                    "h1_hit": h1_hit,
                    "h2_hit": h2_hit,
                    "h3_hit": h3_hit,
                    "momentum_score": momentum_score
                })
                break

            if current_p >= t1 and not h1_hit:
                h1_hit = True
                msg = (
                    f"✅ *[{symbol}] تحقق الهدف الفني الأول!*\n"
                    f"• السعر الحالي: ${current_p}\n"
                    f"• الدخول: ${entry_price}\n"
                    f"• Momentum Score: {momentum_score}/100\n"
                    f"• المراقبة مستمرة."
                )
                send_telegram_message(msg)
                update_gist_state(symbol, {
                    "h1_hit": True,
                    "max_gain": max_gain_pct,
                    "momentum_score": momentum_score
                })

            if current_p >= t2 and not h2_hit:
                h2_hit = True
                msg = (
                    f"🔥 *[{symbol}] تحقق الهدف الفني الثاني!*\n"
                    f"• السعر الحالي: ${current_p}\n"
                    f"• Momentum Score: {momentum_score}/100\n"
                    f"• المراقبة مستمرة طالما الزخم والسعر لم يكسرا الوقف."
                )
                send_telegram_message(msg)
                update_gist_state(symbol, {
                    "h2_hit": True,
                    "max_gain": max_gain_pct,
                    "momentum_score": momentum_score
                })

            if current_p >= t3 and not h3_hit:
                h3_hit = True
                msg = (
                    f"🚀🚀 *[{symbol}] وصل الهدف الفني الثالث!*\n"
                    f"• السعر الحالي: ${current_p}\n"
                    f"• Momentum Score: {momentum_score}/100\n"
                    f"• المراقبة مستمرة لأن السهم قد يتحول لانفجار أكبر."
                )
                send_telegram_message(msg)
                update_gist_state(symbol, {
                    "h3_hit": True,
                    "max_gain": max_gain_pct,
                    "momentum_score": momentum_score
                })

        except Exception as e:
            print(f"⚠️ Error tracking {symbol}: {e}", flush=True)

        time.sleep(TRACK_INTERVAL_SEC)

    if symbol in active_monitors:
        del active_monitors[symbol]

    if not is_scan_time_allowed():
        send_final_session_report_if_ready()
        
def send_explosion_alert(res):
    msg = (
        f"🌟 *[إشارة انفجار ذهبية نخبة]* 🌟\n\n"
        f"🎫 *السهم:* `{res['symbol']}`\n"
        f"💵 *سعر الدخول:* `${res['price']}`\n"
        f"📊 *التغير اليومي:* `+{res['change_pct']}%`\n"
        f"ركائز القوة اللحظية:\n"
        f"🔥 *قوة الانفجار (Score):* `{res['score']}/100`\n"
        f"📈 *الـ RVOL الحالي:* `{res['rvol']}x`\n"
        f"🧬 *تصنيف الفلوت الحقيقي:* `{res['float_tier']}`\n"
        f"🔢 *Real Float:* `{res.get('real_float')}`\n"
        f"➕ *Float Bonus:* `+{res.get('float_bonus', 0)}`\n"
        f"📊 *OBV Status:* `{res.get('obv_tier')}`\n"
        f"➕ *OBV Bonus:* `+{res.get('obv_bonus', 0)}`\n"
        f"🧱 *المقاومة 20 يوم:* `${res['resistance_20']}`\n"
        f"🧱 *المقاومة 50 يوم:* `${res['resistance_50']}`\n"
        f"📏 *ATR 14:* `${res['atr_14']}`\n"
        f"⚡ *تسارع الفوليوم:* `{res['vol_acceleration']}x`\n"
        f"💰 *Dollar Volume:* `${res['dollar_volume']}`\n\n"
        f"🎯 *الأهداف الفنية المحسوبة:*\n"
        f" ├─ Target 1 (ATR ×1): `${res['target1']}`\n"
        f" ├─ Target 2 (ATR ×2): `${res['target2']}`\n"
        f" └─ Target 3 (ATR ×3 أو مقاومة 50 يوم): `${res['target3']}`\n\n"
        f"🛑 *وقف الخسارة الصارم (7%-):* `${res['stop_loss']}`\n"
        f"⏱️ _بدأت الآن مراقبة الزخم كل 10 ثوانٍ._"
    )

    send_telegram_message(msg)

def main_scanner():
    global total_scans_performed, last_scan_timestamp
    global reject_blacklist
    global reject_bad_name
    global reject_bars
    global reject_prev_bars
    global reject_history
    global reject_price
    global reject_avg_vol
    global reject_dollar_volume
    global reject_price_change
    global reject_rvol
    global reject_resistance
    global reject_score

    print("🚀 [رادار النخبة] بدأ العمل بكامل الفلاتر المحدثة والقائمة السوداء الحقيقية...", flush=True)

    api = tradeapi.REST(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
        ALPACA_BASE_URL,
        api_version='v2'
    )

    while True:
        try:
            if should_build_float_cache():
                assets = api.list_assets(
                    status="active"
                )

                threading.Thread(
                    target=build_float_cache_for_assets,
                    args=(assets,),
                    daemon=True
                ).start()

        except Exception as e:
            print(
                f"⚠️ Float cache scheduler error: {e}",
                flush=True
            )
            
        if not is_scan_time_allowed():
            print("⏸️ Scan skipped: outside US premarket/market hours. Sleeping...", flush=True)
            time.sleep(60)
            continue

        global final_session_report_sent, session_closed_reports, last_session_date

        today_key = datetime.now(saudi_tz).strftime("%Y-%m-%d")

        if last_session_date != today_key:
            final_session_report_sent = False
            session_closed_reports = []
            last_session_date = today_key

        print("🔎 Full scan started...", flush=True)

        print(
            "🔍 [بدء مسح شامل للسوق] جاري جلب ومطابقة الأسهم...",
            flush=True
        )

        try:
            assets = api.list_assets(status="active")

            tradable_assets = [
                a
                for a in assets
                if getattr(a, "tradable", False)
                and getattr(a, "_raw", {}).get("class", "") == "us_equity"
                and getattr(a, "_raw", {}).get("exchange", "") in [
                    "NASDAQ",
                    "NYSE",
                    "AMEX"
                ]
                and not any(
                    kw in (getattr(a, "name", "") or "").lower()
                    for kw in BAD_NAME_KEYWORDS
                )
            ]

            print(
                f"✅ Total symbols after filter: {len(tradable_assets)}",
                flush=True
            )

            now_ts = time.time()
            alerts_sent = 0
            stock_count = 0
            clean_radar_watchlist()
            
            for i in range(0, len(tradable_assets), BATCH_SIZE):
                batch = tradable_assets[i:i + BATCH_SIZE]

                for asset in batch:
                    sym = asset.symbol

                    if "/" in sym:
                        continue

                    stock_count += 1

                    if sym in active_monitors:
                        continue

                    if sym in sent_alerts and (now_ts - sent_alerts[sym] < ALERT_COOLDOWN_SEC):
                        continue

                    quick_radar_check(api, sym)

                    if sym not in radar_watchlist:
                        continue

                    result = check_explosion(api, sym, asset.name)

                    if result and result.get("explosion_candidate") is True:
                        send_explosion_alert(result)

                        threading.Thread(
                            target=send_news_after_alert,
                            args=(result,),
                            daemon=True
                        ).start()

                        sent_alerts[sym] = now_ts
                        alerts_sent += 1
                        

                        sent_alerts[sym] = now_ts
                        alerts_sent += 1

                        if sym not in active_monitors:
                            active_monitors[sym] = True

                            t = threading.Thread(
                                target=dedicated_ticker_tracker,
                                args=(
                                    sym,
                                    result["price"],
                                    result["target1"],
                                    result["target2"],
                                    result["target3"],
                                    result["stop_loss"]
                                ),
                                daemon=True
                            )
                            t.start()

                time.sleep(BATCH_DELAY_SEC)

            total_scans_performed += 1
            last_scan_timestamp = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")

            print(
                f"✅ Symbols checked this scan: {stock_count}",
                flush=True
            )
            
            print(f"📡 Radar Watchlist size: {len(radar_watchlist)}", flush=True)
            top_trends = sorted(
                radar_watchlist.items(),
                key=lambda x: x[1].get(
                    "gain_trend",
                    0
                ),
                reverse=True
            )[:5]

            print(
                f"📈 Top Radar Trends: {[s for s, _ in top_trends]}",
                flush=True
            )

            print(
                f"📊 Reject Stats | "
                f"Blacklist={reject_blacklist} | "
                f"BadName={reject_bad_name} | "
                f"Bars={reject_bars} | "
                f"PrevBars={reject_prev_bars} | "
                f"History={reject_history} | "
                f"Price={reject_price} | "
                f"AvgVol={reject_avg_vol} | "
                f"DollarVol={reject_dollar_volume} | "
                f"Change={reject_price_change} | "
                f"RVOL={reject_rvol} | "
                f"Resistance={reject_resistance} | "
                f"Score={reject_score}",
                flush=True
            )

            reject_history = 0
            reject_price = 0
            reject_avg_vol = 0
            reject_dollar_volume = 0
            reject_price_change = 0
            reject_rvol = 0
            reject_resistance = 0
            reject_score = 0
            reject_blacklist = 0
            reject_bad_name = 0
            reject_bars = 0
            reject_prev_bars = 0

            print(
                f"✅ [انتهاء الفحص الشامل] التنبيهات النخبة المرسلة بهذه الدورة: {alerts_sent} | إجمالي الفحوصات: {total_scans_performed}",
                flush=True
            )

        except Exception as e:
            print(f"❌ Main Loop Error: {e}", flush=True)

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    load_float_cache()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    send_telegram_message("🟢 تم تشغيل بوت رادار النخبة بنجاح على سيرفر Render وبدأ مراقبة السوق الآن!")

    main_scanner()
