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
UPSTASH_REDIS_REST_URL = (
    os.getenv("UPSTASH_REDIS_REST_URL") or ""
).rstrip("/")

UPSTASH_REDIS_REST_TOKEN = os.getenv(
    "UPSTASH_REDIS_REST_TOKEN"
)

LTM_INCOMING_KEY = "live_trade_manager:incoming"

PRICE_MIN          = 0.3
PRICE_MAX          = 50.0
MIN_AVG_VOL        = 50_000
MAX_AVG_VOL        = 5_000_000
MIN_DOLLAR_VOLUME  = 300_000

RVOL_MIN           = 1.8
MIN_PRICE_CHANGE   = 4.0

EXPLOSION_CANDIDATE_MIN_SCORE = 90
PREMARKET_CANDIDATE_MIN_SCORE = 84
BATCH_SIZE         = 500
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
    "hemp", "cruise", "cinema", "movie", "theater", "reit", "cbd",
]

active_monitors = {}
sent_alerts = {}
radar_watchlist = {}
premarket_entry_watchlist = {}
PREMARKET_WATCHLIST_LOCK = threading.RLock()
PREMARKET_CONFIRM_WINDOW_MINUTES = 30
PREMARKET_RECHECK_INTERVAL_SEC = 10
PREMARKET_MIN_CONFIRM_MINUTES_AFTER_OPEN = 2
float_cache = {}
news_cache = {}
NEWS_CACHE_MINUTES = 10
NEWS_LOOKBACK_HOURS = 12

FLOAT_CACHE_HOUR = 7
FLOAT_CACHE_MINUTE = 50
FINNHUB_DELAY_SEC = 1.05
last_float_cache_date = None
float_cache_building = False
RADAR_TRIGGER_CHANGE_PCT = 3.5
RADAR_MIN_DOLLAR_VOLUME = 100_000
RADAR_EXPIRE_MINUTES = 30
gist_lock = threading.Lock()
session_closed_reports = []
report_lock = threading.Lock()
final_session_report_sent = False
last_session_date = None
reject_rvol = 0
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
top_rejected_candidates = []

SCAN_INTERVAL_SEC  = 60
TRACK_INTERVAL_SEC = 10
ALERT_COOLDOWN_SEC = 3600
ENTRY_MAX_BREAKOUT_EXTENSION_PCT = 5.0
ENTRY_LARGE_DAILY_MOVE_PCT = 12.0
ENTRY_CONSOLIDATION_MAX_RANGE_PCT = 3.5
ENTRY_REENTRY_NEW_HIGH_PCT = 0.5
ENTRY_MIN_HEADROOM_ATR = 0.75

session_alert_state = {}

FLOAT_CACHE_FILE = "float_cache.json"
LIVE_ALERTS_FILE = "early_explosion_live_alerts.json"
LIVE_RESULTS_FILE = "early_explosion_live_results.json"

def send_to_live_trade_manager(res):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        print(
            f"⚠️ Live Trade Manager Upstash config missing for {res.get('symbol')}",
            flush=True
        )
        return False

    try:
        import json

        payload = {
            "source_bot": "early_explosion",

            "symbol": res.get("symbol"),
            "entry_price": res.get("price"),
            "entry_ts": res.get(
                "alert_sent_ts",
                time.time(),
            ),
            "alert_sent_at": res.get(
                "alert_sent_at"
            ),
            "entry_source": res.get(
                "entry_source",
                "UNKNOWN",
            ),            
            "score": res.get("score"),
            "rvol": res.get("rvol"),

            "stop_loss": res.get("stop_loss"),

            "target1": res.get("target1"),
            "target2": res.get("target2"),
            "target3": res.get("target3"),

            "resistance_20": res.get("resistance_20"),

            "atr_14": res.get("atr_14"),
            "change_pct": res.get("change_pct"),
            "real_float": res.get("real_float"),

            "vol_acceleration": res.get("vol_acceleration"),
            "volume_acceleration_score": res.get(
                "volume_acceleration_score"
            ),
        }

        response = requests.post(
            UPSTASH_REDIS_REST_URL,
            headers={
                "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
                "Content-Type": "application/json",
            },
            json=[
                "RPUSH",
                LTM_INCOMING_KEY,
                json.dumps(payload)
            ],
            timeout=10,
        )

        if response.status_code != 200:
            print(
                f"⚠️ LTM publish failed {res.get('symbol')} | "
                f"HTTP={response.status_code} | "
                f"{response.text[:200]}",
                flush=True
            )
            return False

        data = response.json()

        if isinstance(data, dict) and data.get("error"):
            print(
                f"⚠️ LTM publish Redis error {res.get('symbol')}: "
                f"{data.get('error')}",
                flush=True
            )
            return False

        print(
            f"🧠 Sent to Live Trade Manager: "
            f"{res.get('symbol')} | "
            f"Entry={res.get('price')}",
            flush=True
        )

        return True

    except Exception as e:
        print(
            f"⚠️ Live Trade Manager publish error "
            f"{res.get('symbol')}: {e}",
            flush=True
        )
        return False

def send_telegram_message(text):
    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_INVESTMENT_CHAT_ID
    ):
        print(
            f"[Telegram-Sim] {text}",
            flush=True,
        )
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            json={
                "chat_id": (
                    TELEGRAM_INVESTMENT_CHAT_ID
                ),
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )

        if response.status_code != 200:
            print(
                f"❌ Telegram HTTP Error: "
                f"{response.status_code} | "
                f"{response.text[:300]}",
                flush=True,
            )
            return False

        data = response.json()

        if (
            not isinstance(data, dict)
            or not data.get("ok")
        ):
            print(
                f"❌ Telegram API Error: "
                f"{data}",
                flush=True,
            )
            return False

        return True

    except Exception as e:
        print(
            f"❌ Telegram Error: {e}",
            flush=True,
        )
        return False
        
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

def load_gist_json_file(filename, default_value):
    if not GITHUB_TOKEN or not GIST_ID:
        return default_value

    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    try:
        res = requests.get(url, headers=headers, timeout=10)

        if res.status_code != 200:
            return default_value

        files = res.json().get("files", {})

        if filename not in files:
            return default_value

        content = files[filename].get("content", "")

        if not content:
            return default_value

        import json
        return json.loads(content)

    except Exception as e:
        print(f"⚠️ Gist JSON load error ({filename}): {e}", flush=True)
        return default_value


def append_live_alert_to_gist(alert):
    try:
        import json

        data = load_gist_json_file(LIVE_ALERTS_FILE, [])

        if not isinstance(data, list):
            data = []

        data.append(alert)

        # احتفظ بآخر 1000 تنبيه فقط حتى لا يكبر الملف
        data = data[-1000:]

        update_gist_file(
            LIVE_ALERTS_FILE,
            json.dumps(data, indent=2, default=str)
        )

        print(f"✅ Live alert saved to Gist: {alert.get('symbol')}", flush=True)

    except Exception as e:
        print(f"⚠️ Save live alert error: {e}", flush=True)


def append_live_result_to_gist(result):
    try:
        import json

        data = load_gist_json_file(LIVE_RESULTS_FILE, [])

        if not isinstance(data, list):
            data = []

        data.append(result)

        # احتفظ بآخر 1000 نتيجة فقط
        data = data[-1000:]

        update_gist_file(
            LIVE_RESULTS_FILE,
            json.dumps(data, indent=2, default=str)
        )

        print(f"✅ Live result saved to Gist: {result.get('symbol')}", flush=True)

    except Exception as e:
        print(f"⚠️ Save live result error: {e}", flush=True)
        
def is_scan_time_allowed():
    tz_ny = pytz.timezone("America/New_York")
    now_ny = datetime.now(tz_ny)

    if now_ny.weekday() >= 5:
        return False

    start_time = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
    end_time = now_ny.replace(hour=20, minute=0, second=0, microsecond=0)

    return start_time <= now_ny <= end_time

def get_market_session():
    tz_ny = pytz.timezone("America/New_York")
    now_ny = datetime.now(tz_ny)

    if now_ny.weekday() >= 5:
        return "CLOSED"

    minutes = (
        now_ny.hour * 60
        + now_ny.minute
    )

    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "PREMARKET"

    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "REGULAR"

    if 16 * 60 <= minutes <= 20 * 60:
        return "AFTER_HOURS"

    return "CLOSED"
    
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
        return 10, "ULTRA_LOW_FLOAT"

    if real_float <= 15_000_000:
        return 8, "VERY_LOW_FLOAT"

    if real_float <= 30_000_000:
        return 5, "LOW_FLOAT"

    if real_float <= 60_000_000:
        return 2, "MEDIUM_FLOAT"

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
        "approval", "approved", "fda",
        "contract", "agreement", "deal",
        "partnership", "collaboration",
        "distribution", "distributor",
        "supply", "supply agreement",
        "exclusive", "exclusive agreement",
        "license", "licensing",
        "purchase order", "order",
        "commercial", "commercial supply",
        "commercialization",
        "launch", "expands", "expansion",
        "milestone", "breakthrough",
        "patent", "grant",
        "merger", "acquisition",
        "positive",
        "phase 1", "phase 2", "phase 3",
        "topline", "successful"
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

    scheduled_time = now_ksa.replace(
        hour=FLOAT_CACHE_HOUR,
        minute=FLOAT_CACHE_MINUTE,
        second=0,
        microsecond=0
    )

    return now_ksa >= scheduled_time

def is_weekly_float_refresh_day():
    now_ksa = datetime.now(saudi_tz)
    return now_ksa.weekday() == 0
    
def build_float_cache_for_assets(assets):
    global last_float_cache_date
    global float_cache_building

    if float_cache_building:
        return

    if not should_build_float_cache():
        return

    weekly_refresh = is_weekly_float_refresh_day()

    float_cache_building = True

    if weekly_refresh:
        print("🧬 Starting WEEKLY full Finnhub float refresh...", flush=True)
    else:
        print("🧬 Starting daily Finnhub float cache build...", flush=True)

    loaded = 0
    updated = 0
    skipped = 0

    try:
        for asset in assets:
            symbol = asset.symbol

            if symbol in float_cache and not weekly_refresh:
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
                old_float = None

                if symbol in float_cache and isinstance(float_cache.get(symbol), dict):
                    old_float = float_cache[symbol].get("float")

                float_cache[symbol] = {
                    "float": real_float,
                    "updated": datetime.now(saudi_tz).strftime("%Y-%m-%d")
                }

                if old_float is None:
                    loaded += 1
                elif float(old_float) != float(real_float):
                    updated += 1
                else:
                    skipped += 1

            save_float_cache()

            time.sleep(FINNHUB_DELAY_SEC)

        last_float_cache_date = datetime.now(
            saudi_tz
        ).strftime("%Y-%m-%d")

        print(
            f"✅ Float cache build finished | Loaded={loaded} | Updated={updated} | Skipped={skipped} | Total={len(float_cache)}",
            flush=True
        )

    except Exception as e:
        print(
            f"❌ Float cache build error: {e}",
            flush=True
        )

    finally:
        float_cache_building = False
    
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

def quick_radar_check_from_snapshot(symbol, snapshot):
    try:
        if not snapshot:
            return False

        if snapshot.latest_trade:
            current_price = float(snapshot.latest_trade.price)
        else:
            return False

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

    last_1m = float(volumes.iloc[-1])
    avg_prev_10 = float(volumes.iloc[-11:-1].mean())
    last_1m_vs_avg = last_1m / avg_prev_10 if avg_prev_10 > 0 else 0.0

    last_3m_avg = float(volumes.iloc[-3:].mean())
    prev_7m_avg = float(volumes.iloc[-10:-3].mean())
    last_3m_vs_prev_7m = last_3m_avg / prev_7m_avg if prev_7m_avg > 0 else 0.0

    v1 = float(volumes.iloc[-3])
    v2 = float(volumes.iloc[-2])
    v3 = float(volumes.iloc[-1])
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
        score += 7
    elif last_3m_vs_prev_7m >= 1.8:
        score += 5
    elif last_3m_vs_prev_7m >= 1.3:
        score += 3

    if volume_trend_up:
        score += 2

    if volume_peak_recent:
        score += 3

    vol_acceleration = max(last_1m_vs_avg, last_3m_vs_prev_7m)

    return {
        "volume_acceleration_score": min(score, 15),
        "vol_acceleration": round(vol_acceleration, 2),
        "last_1m_vs_avg": round(last_1m_vs_avg, 2),
        "last_3m_vs_prev_7m": round(last_3m_vs_prev_7m, 2),
        "volume_trend_up": bool(volume_trend_up),
        "volume_peak_recent": bool(volume_peak_recent)
    }

def track_rejected_candidate(symbol, score, reason, rvol, price_change_pct, vol_acceleration, dollar_volume, breakdown=None):
    global top_rejected_candidates

    try:
        top_rejected_candidates.append({
            "symbol": symbol,
            "score": float(score),
            "reason": reason,
            "rvol": float(rvol),
            "change_pct": float(price_change_pct),
            "vol_acceleration": float(vol_acceleration),
            "dollar_volume": float(dollar_volume),
            "breakdown": breakdown or {}
        })

        top_rejected_candidates = sorted(
            top_rejected_candidates,
            key=lambda x: x.get("score", 0),
            reverse=True
        )[:10]

    except Exception:
        pass

def evaluate_entry_quality(
    symbol,
    current_price,
    price_change_pct,
    resistance_20,
    atr_14,
    previous_bars,
    bars_1m,
    volume_acceleration_score,
    last_1m_vs_avg,
    last_3m_vs_prev_7m,
):
    try:
        if (
            bars_1m is None
            or bars_1m.empty
            or len(bars_1m) < 12
        ):
            return False, "INSUFFICIENT_INTRADAY_DATA", {}

        bars_1m = bars_1m.sort_index()

        closes = bars_1m["close"].astype(float)
        highs = bars_1m["high"].astype(float)
        lows = bars_1m["low"].astype(float)

        ema9 = closes.ewm(
            span=9,
            adjust=False
        ).mean()

        ema20 = closes.ewm(
            span=20,
            adjust=False
        ).mean()

        ema9_now = float(ema9.iloc[-1])
        ema20_now = float(ema20.iloc[-1])
        last_close = float(closes.iloc[-1])

        momentum_structure_ok = (
            ema9_now > ema20_now
            and last_close >= ema9_now
        )

        prior_6 = bars_1m.iloc[-7:-1]

        prior_6_high = float(
            prior_6["high"].max()
        )

        prior_6_low = float(
            prior_6["low"].min()
        )

        consolidation_range_pct = (
            (
                prior_6_high
                - prior_6_low
            )
            / prior_6_low
        ) * 100 if prior_6_low > 0 else 999.0

        consolidation_ok = (
            consolidation_range_pct
            <= ENTRY_CONSOLIDATION_MAX_RANGE_PCT
        )

        fresh_intraday_breakout = (
            last_close
            > prior_6_high * 1.001
            and momentum_structure_ok
            and (
                volume_acceleration_score >= 5
                or last_1m_vs_avg >= 1.3
                or last_3m_vs_prev_7m >= 1.3
            )
        )

        breakout_extension_pct = 0.0

        if resistance_20 > 0:
            breakout_extension_pct = (
                (
                    current_price
                    - resistance_20
                )
                / resistance_20
            ) * 100

        if breakout_extension_pct > ENTRY_MAX_BREAKOUT_EXTENSION_PCT:
            if not (
                consolidation_ok
                and fresh_intraday_breakout
            ):
                return False, "OVEREXTENDED_FROM_BREAKOUT", {
                    "breakout_extension_pct": round(
                        breakout_extension_pct,
                        2
                    ),
                    "consolidation_range_pct": round(
                        consolidation_range_pct,
                        2
                    ),
                }

        if price_change_pct >= ENTRY_LARGE_DAILY_MOVE_PCT:
            if not (
                consolidation_ok
                and fresh_intraday_breakout
            ):
                return False, "LATE_DAILY_MOVE_NO_NEW_BASE", {
                    "breakout_extension_pct": round(
                        breakout_extension_pct,
                        2
                    ),
                    "consolidation_range_pct": round(
                        consolidation_range_pct,
                        2
                    ),
                }

        overhead_highs = previous_bars.loc[
            previous_bars["high"] > current_price,
            "high"
        ]

        nearest_overhead = None
        headroom_atr = None

        if not overhead_highs.empty:
            nearest_overhead = float(
                overhead_highs.min()
            )

            if atr_14 > 0:
                headroom_atr = (
                    nearest_overhead
                    - current_price
                ) / atr_14

                if headroom_atr < ENTRY_MIN_HEADROOM_ATR:
                    return False, "INSUFFICIENT_HEADROOM", {
                        "nearest_overhead": round(
                            nearest_overhead,
                            4
                        ),
                        "headroom_atr": round(
                            headroom_atr,
                            2
                        ),
                    }

        previous_alert = session_alert_state.get(
            symbol
        )

        recent_intraday_high = float(
            highs.max()
        )

        if previous_alert:
            previous_high = float(
                previous_alert.get(
                    "recent_intraday_high",
                    previous_alert.get(
                        "alert_price",
                        0
                    )
                )
            )

            required_new_high = (
                previous_high
                * (
                    1
                    + ENTRY_REENTRY_NEW_HIGH_PCT / 100
                )
            )

            reentry_structure_ok = (
                current_price >= required_new_high
                and momentum_structure_ok
                and fresh_intraday_breakout
            )

            if not reentry_structure_ok:
                return False, "REENTRY_NOT_RESET", {
                    "previous_high": round(
                        previous_high,
                        4
                    ),
                    "required_new_high": round(
                        required_new_high,
                        4
                    ),
                    "current_price": round(
                        current_price,
                        4
                    ),
                }

        return True, "ENTRY_QUALITY_OK", {
            "ema9": round(ema9_now, 4),
            "ema20": round(ema20_now, 4),
            "breakout_extension_pct": round(
                breakout_extension_pct,
                2
            ),
            "consolidation_range_pct": round(
                consolidation_range_pct,
                2
            ),
            "fresh_intraday_breakout": bool(
                fresh_intraday_breakout
            ),
            "recent_intraday_high": round(
                recent_intraday_high,
                4
            ),
            "nearest_overhead": (
                round(nearest_overhead, 4)
                if nearest_overhead is not None
                else None
            ),
            "headroom_atr": (
                round(headroom_atr, 2)
                if headroom_atr is not None
                else None
            ),
        }

    except Exception as e:
        print(
            f"⚠️ Entry Quality Gate error "
            f"{symbol}: {e}",
            flush=True
        )

        return False, "ENTRY_QUALITY_ERROR", {}
        
def check_explosion(api, symbol, asset_name):
    global reject_price_change
    global reject_rvol
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
            adjustment="split",
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

        avg_vol_20 = float(
            previous_bars["volume"].tail(20).mean()
        )

        float_info = float_cache.get(symbol)
        real_float = None

        if float_info:
            real_float = float_info.get("float")

        float_bonus_raw, float_tier = get_float_bonus(real_float)
        float_bonus = float_bonus_raw

        if avg_vol_20 < MIN_AVG_VOL:
            reject_avg_vol += 1
            return None

        effective_max_avg_vol = (
            10_000_000
            if current_price > 30.0
            else MAX_AVG_VOL
        )

        if avg_vol_20 > effective_max_avg_vol:
            if real_float is not None:
                if real_float > 30_000_000:
                    reject_avg_vol += 1
                    return None
            elif avg_vol_20 > effective_max_avg_vol * 2:
                reject_avg_vol += 1
                return None
                
        resistance_20 = float(
            previous_bars["high"].tail(20).max()
        )
        resistance_50 = float(
            previous_bars["high"].tail(50).max()
        )

        atr_14 = calculate_atr_14(previous_bars)

        if price_change_pct < MIN_PRICE_CHANGE:
            reject_price_change += 1
            return None

        rvol = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0

        if rvol < RVOL_MIN:
            reject_rvol += 1
            return None

        dollar_volume = today_vol * current_price

        if dollar_volume < MIN_DOLLAR_VOLUME:
            reject_dollar_volume += 1
            return None

        resistance_points = 0

        if current_price >= resistance_20:
            resistance_points = 15
        elif current_price >= resistance_20 * 0.99:
            resistance_points = 8
            

        bars_1m = api.get_bars(
            symbol,
            tradeapi.rest.TimeFrame.Minute,
            limit=20,
            adjustment="raw"
        ).df

        acceleration_data = calculate_volume_acceleration(bars_1m)

        vol_acceleration = acceleration_data["vol_acceleration"]
        volume_acceleration_score = acceleration_data["volume_acceleration_score"]
        last_1m_vs_avg = acceleration_data["last_1m_vs_avg"]
        last_3m_vs_prev_7m = acceleration_data["last_3m_vs_prev_7m"]
        volume_trend_up = acceleration_data["volume_trend_up"]
        volume_peak_recent = acceleration_data["volume_peak_recent"]
        
        obv_bonus_raw, obv_tier = calculate_obv_bonus(
            bars_1m
        )
        obv_bonus = min(obv_bonus_raw, 10)

        score = 0

        # RVOL: max 25
        if rvol >= 10:
            score += 25
        elif rvol >= 5:
            score += 20
        elif rvol >= RVOL_MIN:
            score += 15

        # Price Change: max 20
        if price_change_pct >= 20:
            score += 20
        elif price_change_pct >= 10:
            score += 12
        elif price_change_pct >= MIN_PRICE_CHANGE:
            score += 8

        # Breakout / Resistance: max 15
        score += resistance_points

        # Volume Acceleration: max 15
        score += volume_acceleration_score

        # Volume Cooling Penalty
        if not volume_trend_up and volume_peak_recent:
            score -= 8

        # Dollar Volume: max 10
        if dollar_volume >= 10_000_000:
            score += 10
        elif dollar_volume >= 2_000_000:
            score += 7
        elif dollar_volume >= MIN_DOLLAR_VOLUME:
            score += 5

        # Reject cooled-off moves
        if (
            last_1m_vs_avg < 0.8
            and not volume_trend_up
            and not volume_peak_recent
        ):
            reject_score += 1
            return None

        # Gain Trend: max 5
        if gain_trend >= 1.0:
            score += 5
        elif gain_trend >= 0.5:
            score += 3
        elif gain_trend > 0:
            score += 1

        # OBV: max 10
        score += obv_bonus

        # Float: max 10
        score += float_bonus

        if gain_trend <= 0:
            reject_score += 1
            return None

        if vol_acceleration < 1.0 and obv_bonus == 0:
            reject_score += 1
            return None

        score = min(score, 100)

        if score >= 70:
            print(
                f"[ACCEL] {symbol} | "
                f"Score={score} | "
                f"1m={last_1m_vs_avg:.2f}x | "
                f"3m={last_3m_vs_prev_7m:.2f}x | "
                f"Trend={volume_trend_up} | "
                f"Peak={volume_peak_recent} | "
                f"AccelScore={volume_acceleration_score}",
                flush=True
            )
        score_breakdown = {
            "RVOL": 25 if rvol >= 10 else 20 if rvol >= 5 else 15 if rvol >= RVOL_MIN else 0,
            "Accel": volume_acceleration_score,
            "Price": 20 if price_change_pct >= 20 else 12 if price_change_pct >= 10 else 8 if price_change_pct >= MIN_PRICE_CHANGE else 0,
            "Breakout": 15 if current_price >= resistance_20 else 8 if current_price >= resistance_20 * 0.99 else 0,
            "OBV": obv_bonus,
            "Float": float_bonus,
            "Liquidity": 10 if dollar_volume >= 10_000_000 else 7 if dollar_volume >= 2_000_000 else 5 if dollar_volume >= MIN_DOLLAR_VOLUME else 0,
            "GainTrend": 5 if gain_trend >= 1.0 else 3 if gain_trend >= 0.5 else 1 if gain_trend > 0 else 0,
            "CoolingPenalty": -8 if not volume_trend_up and volume_peak_recent else 0,
        }
        
        mega_volume_exception = (
            score >= 87
            and rvol >= 20
            and dollar_volume >= 10_000_000
            and volume_acceleration_score >= 10
        )
        market_session = get_market_session()

        required_candidate_score = (
            PREMARKET_CANDIDATE_MIN_SCORE
            if market_session == "PREMARKET"
            else EXPLOSION_CANDIDATE_MIN_SCORE
        )

        if (
            score < required_candidate_score
            and not mega_volume_exception
        ):
            track_rejected_candidate(
                symbol,
                score,
                "LOW_SCORE",
                rvol,
                price_change_pct,
                vol_acceleration,
                dollar_volume,
                score_breakdown
            )
            reject_score += 1
            return None

        entry_quality_ok, entry_quality_reason, entry_quality_data = (
            evaluate_entry_quality(
                symbol,
                current_price,
                price_change_pct,
                resistance_20,
                atr_14,
                previous_bars,
                bars_1m,
                volume_acceleration_score,
                last_1m_vs_avg,
                last_3m_vs_prev_7m,
            )
        )

        if not entry_quality_ok:
            print(
                f"🚫 ENTRY QUALITY REJECT | "
                f"{symbol} | "
                f"{entry_quality_reason} | "
                f"{entry_quality_data}",
                flush=True
            )
            return None
            
        digits = 4 if current_price < 1 else 2

        stop_loss = round(current_price * 0.93, digits)

        target1 = round(current_price + atr_14, digits)
        target2 = round(current_price + (atr_14 * 2), digits)
        target3 = round(
            max(resistance_50, current_price + (atr_14 * 3)),
            digits
        )

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
            "volume_acceleration_score": volume_acceleration_score,
            "last_1m_vs_avg": last_1m_vs_avg,
            "last_3m_vs_prev_7m": last_3m_vs_prev_7m,
            "volume_trend_up": volume_trend_up,
            "volume_peak_recent": volume_peak_recent,
            "dollar_volume": round(dollar_volume, 0),
            "target1": target1,
            "target2": target2,
            "target3": target3,
            "stop_loss": stop_loss,
            "entry_quality_reason": entry_quality_reason,
            "entry_quality": entry_quality_data,
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
                result_record = {
                    "type": "SESSION_RESULT",
                    "saved_at_ksa": datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S"),
                    "saved_at_ts": time.time(),

                    "symbol": symbol,
                    "entry_price": entry_price,

                    "t1": t1,
                    "t2": t2,
                    "t3": t3,
                    "sl": sl,

                    "max_gain": max_gain_pct,
                    "h1_hit": h1_hit,
                    "h2_hit": h2_hit,
                    "h3_hit": h3_hit,

                    "strong_momentum_sent": strong_momentum_sent,
                    "weak_momentum_sent": weak_momentum_sent,

                    "status": "session_closed"
                }

                session_closed_reports.append(result_record)

                threading.Thread(
                    target=append_live_result_to_gist,
                    args=(result_record,),
                    daemon=True
                ).start()

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

def add_to_premarket_entry_watchlist(
    result,
    asset_name="",
):
    symbol = result.get("symbol")

    if not symbol:
        return False

    with PREMARKET_WATCHLIST_LOCK:
        existing = premarket_entry_watchlist.get(
            symbol
        )

        if existing:
            added_at = existing.get(
                "added_at",
                time.time(),
            )
            added_at_ksa = existing.get(
                "added_at_ksa",
                datetime.now(
                    saudi_tz
                ).strftime("%Y-%m-%d %H:%M:%S"),
            )

        else:
            added_at = time.time()
            added_at_ksa = datetime.now(
                saudi_tz
            ).strftime("%Y-%m-%d %H:%M:%S")

        premarket_entry_watchlist[symbol] = {
            "result": dict(result),
            "asset_name": asset_name,
            "market_date": datetime.now(
                pytz.timezone("America/New_York")
            ).strftime("%Y-%m-%d"),
            "added_at": added_at,
            "added_at_ksa": added_at_ksa,
            "last_updated_at": time.time(),
            "last_recheck_at": existing.get(
                "last_recheck_at",
                0,
            ) if existing else 0,
        }

    print(
        f"🌅 PREMARKET WATCHLIST: "
        f"{symbol} | "
        f"Score={result.get('score', 0)} | "
        f"Price={result.get('price', 0)}"
    )

    return True

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def is_premarket_candidate_weak(
    api,
    symbol,
    item,
):
    original = item.get(
        "result",
        {},
    )

    try:
        bars = api.get_bars(
            symbol,
            tradeapi.rest.TimeFrame.Minute,
            limit=10,
            adjustment="raw",
        ).df

        if (
            bars is None
            or bars.empty
            or len(bars) < 5
        ):
            return False, None

        bars = bars.sort_index()

        closes = bars[
            "close"
        ].astype(float)

        volumes = bars[
            "volume"
        ].astype(float)

        live_price = float(
            closes.iloc[-1]
        )

        original_price = safe_float(
            original.get("price")
        )

        resistance = safe_float(
            original.get("resistance_20")
        )

        if original_price <= 0:
            return False, None

        drop_from_premarket_pct = (
            (
                live_price
                - original_price
            )
            / original_price
        ) * 100.0

        severe_price_failure = (
            drop_from_premarket_pct <= -5.0
        )

        meaningful_price_failure = (
            drop_from_premarket_pct <= -3.0
        )

        lost_resistance = (
            resistance > 0
            and closes.iloc[-1] < resistance
            and closes.iloc[-2] < resistance
            and closes.iloc[-3] < resistance
        )

        recent_avg_volume = float(
            volumes.tail(3).mean()
        )

        previous_avg_volume = float(
            volumes.iloc[-8:-3].mean()
        )

        volume_ratio = (
            recent_avg_volume
            / previous_avg_volume
            if previous_avg_volume > 0
            else 1.0
        )

        volume_fading = (
            volume_ratio < 0.55
        )

        recent_downtrend = (
            closes.iloc[-1]
            < closes.iloc[-2]
            < closes.iloc[-3]
        )

        weak_structure = (
            lost_resistance
            and (
                volume_fading
                or recent_downtrend
            )
        )

        if severe_price_failure:
            return True, (
                f"price={live_price:.4f} | "
                f"drop={drop_from_premarket_pct:.2f}% | "
                f"severe_price_failure=True"
            )

        if (
            meaningful_price_failure
            and weak_structure
        ):
            return True, (
                f"price={live_price:.4f} | "
                f"drop={drop_from_premarket_pct:.2f}% | "
                f"lost_resistance={lost_resistance} | "
                f"volume_fading={volume_fading} | "
                f"recent_downtrend={recent_downtrend}"
            )

        return False, None

    except Exception as e:
        print(
            f"⚠️ PREMARKET WEAKNESS CHECK ERROR: "
            f"{symbol} | {e}",
            flush=True,
        )
        return False, None


        
def process_premarket_entry_watchlist(api):
    if get_market_session() != "REGULAR":
        return

    with PREMARKET_WATCHLIST_LOCK:
        symbols = list(
            premarket_entry_watchlist.keys()
        )

    for symbol in symbols:
        with PREMARKET_WATCHLIST_LOCK:
            item = premarket_entry_watchlist.get(
                symbol
            )

        if not item:
            continue
            
        last_recheck_at = safe_float(
            item.get("last_recheck_at")
        )

        if (
            time.time() - last_recheck_at
            < PREMARKET_RECHECK_INTERVAL_SEC
        ):
            continue

        with PREMARKET_WATCHLIST_LOCK:
            if symbol in premarket_entry_watchlist:
                premarket_entry_watchlist[
                    symbol
                ]["last_recheck_at"] = time.time()
            
        now_ts = time.time()

        if (
            symbol in sent_alerts
            and (
                now_ts
                - sent_alerts[symbol]
                < ALERT_COOLDOWN_SEC
            )
        ):
            with PREMARKET_WATCHLIST_LOCK:
                premarket_entry_watchlist.pop(
                    symbol,
                    None,
                )

            print(
                f"🧹 PREMARKET REMOVED: "
                f"{symbol} | "
                f"Already alerted",
                flush=True,
            )
            continue
            
        current_market_date = datetime.now(
            pytz.timezone("America/New_York")
        ).strftime("%Y-%m-%d")

        if (
            item.get("market_date")
            != current_market_date
        ):
            with PREMARKET_WATCHLIST_LOCK:
                premarket_entry_watchlist.pop(
                    symbol,
                    None,
                )

            print(
                f"🧹 PREMARKET STALE REMOVED: "
                f"{symbol} | "
                f"OldDate="
                f"{item.get('market_date')} | "
                f"CurrentDate="
                f"{current_market_date}",
                flush=True,
            )
            continue
            
        tz_ny = pytz.timezone(
            "America/New_York"
        )
        now_ny = datetime.now(tz_ny)

        market_open_ny = now_ny.replace(
            hour=9,
            minute=30,
            second=0,
            microsecond=0,
        )

        minutes_since_open = (
            now_ny - market_open_ny
        ).total_seconds() / 60.0
        if (
            minutes_since_open
            < PREMARKET_MIN_CONFIRM_MINUTES_AFTER_OPEN
        ):
            continue
        if (
            minutes_since_open
            > PREMARKET_CONFIRM_WINDOW_MINUTES
        ):
            with PREMARKET_WATCHLIST_LOCK:
                premarket_entry_watchlist.pop(
                    symbol,
                    None,
                )

            print(
                f"⌛ PREMARKET EXPIRED: "
                f"{symbol} | "
                f"No confirmation within "
                f"{PREMARKET_CONFIRM_WINDOW_MINUTES}m "
                f"after open"
            )
            continue

        weak, weakness_reason = (
            is_premarket_candidate_weak(
                api,
                symbol,
                item,
            )
        )

        if weak:
            with PREMARKET_WATCHLIST_LOCK:
                premarket_entry_watchlist.pop(
                    symbol,
                    None,
                )

            print(
                f"📉 PREMARKET REMOVED: "
                f"{symbol} | "
                f"Weakness: {weakness_reason}",
                flush=True,
            )
            continue
            
        asset_name = item.get(
            "asset_name",
            "",
        )
        radar_refreshed = quick_radar_check(
            api,
            symbol,
        )

        if not radar_refreshed:
            continue
            
        try:
            fresh_result = check_explosion(
                api,
                symbol,
                asset_name,
            )

        except Exception as e:
            print(
                f"⚠️ PREMARKET RECHECK ERROR: "
                f"{symbol} | {e}"
            )
            continue
        if (
            fresh_result
            and safe_float(
                fresh_result.get("score")
            ) < EXPLOSION_CANDIDATE_MIN_SCORE
        ):
            continue
            
        if (
            fresh_result
            and fresh_result.get(
                "explosion_candidate"
            ) is True
        ):
            fresh_result["entry_source"] = (
                "PREMARKET_CONFIRMED_AT_OPEN"
            )      
            alert_sent = send_explosion_alert(
                fresh_result
            )

            if not alert_sent:
                continue

            sent_alerts[symbol] = time.time()
            
            threading.Thread(
                target=send_news_after_alert,
                args=(fresh_result,),
                daemon=True,
            ).start()

            manager_started = (
                send_to_live_trade_manager(
                    fresh_result
                )
            )

            if manager_started:
                print(
                    f"✅ {symbol} handed to "
                    f"Unified Live Trade Manager",
                    flush=True,
                )

            else:
                print(
                    f"⚠️ Unified Live Trade Manager "
                    f"unavailable — using legacy "
                    f"tracker for {symbol}",
                    flush=True,
                )

                if symbol not in active_monitors:
                    active_monitors[symbol] = True

                    t = threading.Thread(
                        target=dedicated_ticker_tracker,
                        args=(
                            symbol,
                            fresh_result["price"],
                            fresh_result["target1"],
                            fresh_result["target2"],
                            fresh_result["target3"],
                            fresh_result["stop_loss"],
                        ),
                        daemon=True,
                    )

                    t.start()
                    
            with PREMARKET_WATCHLIST_LOCK:
                premarket_entry_watchlist.pop(
                    symbol,
                    None,
                )

            print(
                f"✅ PREMARKET CONFIRMED "
                f"AT OPEN: {symbol}"
            )
            
def send_explosion_alert(res):

    msg = (
        f"🌟 *[إشارة انفجار ذهبية نخبة]* 🌟\n\n"
        f"🎫 *السهم:* `{res['symbol']}`\n"
        f"🕒 *مصدر الدخول:* "
        f"`{res.get('entry_source', 'UNKNOWN')}`\n"
        f"💵 *سعر الدخول:* `${res['price']}`\n"
        f"📊 *التغير اليومي:* "
        f"`+{res['change_pct']}%`\n"
        f"ركائز القوة اللحظية:\n"
        f"🔥 *قوة الانفجار (Score):* "
        f"`{res['score']}/100`\n"
        f"📈 *الـ RVOL الحالي:* "
        f"`{res['rvol']}x`\n"
        f"🧬 *تصنيف الفلوت الحقيقي:* "
        f"`{res['float_tier']}`\n"
        f"🔢 *Real Float:* "
        f"`{res.get('real_float')}`\n"
        f"➕ *Float Bonus:* "
        f"`+{res.get('float_bonus', 0)}`\n"
        f"📊 *OBV Status:* "
        f"`{res.get('obv_tier')}`\n"
        f"➕ *OBV Bonus:* "
        f"`+{res.get('obv_bonus', 0)}`\n"
        f"🧱 *المقاومة 20 يوم:* "
        f"`${res['resistance_20']}`\n"
        f"🧱 *المقاومة 50 يوم:* "
        f"`${res['resistance_50']}`\n"
        f"📏 *ATR 14:* "
        f"`${res['atr_14']}`\n"
        f"⚡ *تسارع الفوليوم:* "
        f"`{res['vol_acceleration']}x`\n"
        f"• آخر دقيقة/المتوسط: "
        f"`{res.get('last_1m_vs_avg')}x`\n"
        f"• آخر 3 دقائق/السابق: "
        f"`{res.get('last_3m_vs_prev_7m')}x`\n"
        f"• الحجم يتصاعد: "
        f"`{'نعم' if res.get('volume_trend_up') else 'لا'}`\n"
        f"• قمة الحجم حديثة: "
        f"`{'نعم' if res.get('volume_peak_recent') else 'لا'}`\n"
        f"💰 *Dollar Volume:* "
        f"`${res['dollar_volume']}`\n\n"
        f"🎯 *الأهداف الفنية المحسوبة:*\n"
        f" ├─ Target 1 (ATR ×1): "
        f"`${res['target1']}`\n"
        f" ├─ Target 2 (ATR ×2): "
        f"`${res['target2']}`\n"
        f" └─ Target 3 "
        f"(ATR ×3 أو مقاومة 50 يوم): "
        f"`${res['target3']}`\n\n"
        f"🛑 *وقف الخسارة الصارم (7%-):* "
        f"`${res['stop_loss']}`\n"
        f"⏱️ _بدأت الآن مراقبة الزخم "
        f"كل 10 ثوانٍ._"
    )

    alert_sent = send_telegram_message(
        msg
    )

    if not alert_sent:
        return False

    alert_sent_ts = time.time()
    alert_sent_at = datetime.now(
        saudi_tz
    ).strftime("%Y-%m-%d %H:%M:%S")

    res["alert_sent_ts"] = alert_sent_ts
    res["alert_sent_at"] = alert_sent_at

    live_alert_record = {
        "type": "ENTRY_ALERT",
        "saved_at_ksa": alert_sent_at,
        "saved_at_ts": alert_sent_ts,

        "symbol": res.get("symbol"),
        "price": res.get("price"),
        "rvol": res.get("rvol"),
        "change_pct": res.get("change_pct"),
        "score": res.get("score"),
        "entry_source": res.get(
            "entry_source",
            "UNKNOWN",
        ),

        "float_tier": res.get(
            "float_tier"
        ),
        "real_float": res.get(
            "real_float"
        ),
        "float_bonus": res.get(
            "float_bonus"
        ),

        "obv_tier": res.get(
            "obv_tier"
        ),
        "obv_bonus": res.get(
            "obv_bonus"
        ),

        "resistance_20": res.get(
            "resistance_20"
        ),
        "resistance_50": res.get(
            "resistance_50"
        ),
        "atr_14": res.get(
            "atr_14"
        ),

        "vol_acceleration": res.get(
            "vol_acceleration"
        ),
        "volume_acceleration_score": res.get(
            "volume_acceleration_score"
        ),
        "last_1m_vs_avg": res.get(
            "last_1m_vs_avg"
        ),
        "last_3m_vs_prev_7m": res.get(
            "last_3m_vs_prev_7m"
        ),
        "volume_trend_up": res.get(
            "volume_trend_up"
        ),
        "volume_peak_recent": res.get(
            "volume_peak_recent"
        ),

        "dollar_volume": res.get(
            "dollar_volume"
        ),

        "target1": res.get("target1"),
        "target2": res.get("target2"),
        "target3": res.get("target3"),
        "stop_loss": res.get(
            "stop_loss"
        ),

        "alert_sent_at": alert_sent_at,
        "alert_sent_ts": alert_sent_ts,
    }

    threading.Thread(
        target=append_live_alert_to_gist,
        args=(live_alert_record,),
        daemon=True,
    ).start()

    return True            
    
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
            session_alert_state.clear()
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
            global top_rejected_candidates
            top_rejected_candidates = []

            for i in range(0, len(tradable_assets), BATCH_SIZE):
                batch = tradable_assets[i:i + BATCH_SIZE]
                batch_symbols = [
                    asset.symbol
                    for asset in batch
                    if "/" not in asset.symbol
                ]

                try:
                    batch_snapshots = api.get_snapshots(batch_symbols)
                except Exception as e:
                    print(f"⚠️ Batch snapshots error: {e}", flush=True)
                    batch_snapshots = {}

                for asset in batch:
                    sym = asset.symbol

                    if "/" in sym:
                        continue

                    stock_count += 1

                    if sym in active_monitors:
                        continue

                    if sym in sent_alerts and (now_ts - sent_alerts[sym] < ALERT_COOLDOWN_SEC):
                        continue

                    snapshot = batch_snapshots.get(sym)

                    if not quick_radar_check_from_snapshot(sym, snapshot):
                        continue

                    if sym not in radar_watchlist:
                        continue

                    result = check_explosion(api, sym, asset.name)

                    if result and result.get("explosion_candidate") is True:
                        
                        market_session = get_market_session()
                        if market_session not in (
                            "PREMARKET",
                            "REGULAR",
                        ):
                            continue
                            
                        if market_session == "REGULAR":
                            with PREMARKET_WATCHLIST_LOCK:
                                is_premarket_watched = (
                                    sym
                                    in premarket_entry_watchlist
                                )

                            if is_premarket_watched:
                                continue
                                
                        if market_session == "PREMARKET":
                            add_to_premarket_entry_watchlist(
                                result,
                                asset.name,
                            )
                            continue
                        result["entry_source"] = (
                            "REGULAR_DIRECT"
                        )
                        alert_sent = send_explosion_alert(
                            result
                        )

                        if not alert_sent:
                            continue

                        sent_alerts[sym] = time.time()
                        
                        entry_quality_data = result.get(
                            "entry_quality",
                            {}
                        )

                        session_alert_state[sym] = {
                            "alert_ts": time.time(),
                            "alert_price": float(
                                result.get("price", 0)
                            ),
                            "recent_intraday_high": float(
                                entry_quality_data.get(
                                    "recent_intraday_high",
                                    result.get("price", 0)
                                )
                            ),
                        }
                        
                        threading.Thread(
                            target=send_news_after_alert,
                            args=(result,),
                            daemon=True
                        ).start()

                        manager_started = send_to_live_trade_manager(result)

                        if manager_started:
                            print(
                                f"✅ {sym} handed to Unified Live Trade Manager",
                                flush=True
                            )

                        else:
                            print(
                                f"⚠️ Unified Live Trade Manager unavailable — "
                                f"using legacy tracker for {sym}",
                                flush=True
                            )

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
                                
                process_premarket_entry_watchlist(api)
                
                time.sleep(BATCH_DELAY_SEC)

            total_scans_performed += 1
            last_scan_timestamp = datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")

            print(
                f"✅ Symbols checked this scan: {stock_count}",
                flush=True
            )
            if top_rejected_candidates:
                print("🔍 Top 10 Rejected Candidates:", flush=True)

                for idx, item in enumerate(top_rejected_candidates, start=1):
                    print(
                        f"{idx}. {item['symbol']} | "
                        f"Score={item['score']:.1f} | "
                        f"Reason={item['reason']} | "
                        f"RVOL={item['rvol']:.2f} | "
                        f"Change={item['change_pct']:.2f}% | "
                        f"Accel={item['vol_acceleration']:.2f}x | "
                        f"DollarVol=${item['dollar_volume']:,.0f}",
                        flush=True
                    )
                    
                    breakdown = item.get("breakdown", {})

                    if breakdown:
                        print(
                            f"   Breakdown | "
                            f"RVOL={breakdown.get('RVOL', 0)} | "
                            f"Accel={breakdown.get('Accel', 0)} | "
                            f"Price={breakdown.get('Price', 0)} | "
                            f"Breakout={breakdown.get('Breakout', 0)} | "
                            f"OBV={breakdown.get('OBV', 0)} | "
                            f"Float={breakdown.get('Float', 0)} | "
                            f"Liquidity={breakdown.get('Liquidity', 0)} | "
                            f"GainTrend={breakdown.get('GainTrend', 0)} | "
                            f"CoolingPenalty={breakdown.get('CoolingPenalty', 0)}",
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
                f"Score={reject_score}",
                flush=True
            )

            reject_history = 0
            reject_price = 0
            reject_avg_vol = 0
            reject_dollar_volume = 0
            reject_price_change = 0
            reject_rvol = 0
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
