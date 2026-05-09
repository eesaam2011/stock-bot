import os
import time
import json
import requests
import threading
from flask import Flask

# =========================
# SETTINGS
# =========================

PRICE_MIN = 0.4
PRICE_MAX = 25.0

MIN_TODAY_VOLUME = 150_000
MIN_DOLLAR_VOLUME = 750_000

TOP_N = 800
RUN_INTERVAL = 900  # كل 15 دقيقة

MASTER_LIST_FILE = "master_list.json"

ALPACA_API_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

ALPACA_TRADING_BASE = "https://paper-api.alpaca.markets"
ALPACA_DATA_BASE = "https://data.alpaca.markets"

app = Flask(__name__)

# =========================
# BLACKLIST
# =========================

BLACKLIST_SYMBOLS = [
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC",
    "MET", "PRU", "ALL", "AIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS",
    "BUD", "TAP", "STZ", "DEO",
    "PM", "MO",
    "CGC", "TLRY", "ACB",
    "NCLH", "CCL", "RCL",
    "AMC", "GPRE", "SKLZ", "PGY", "JELD", "TWO",
]

BLACKLIST_KEYWORDS = [
    # ربوية / مالية
    "bank", "finance", "capital", "credit", "lending",

    # قمار / مراهنات
    "casino", "gambling", "bet", "betting", "sportsbook",

    # كحول
    "alcohol", "beer", "wine", "spirits", "distillery",

    # تبغ
    "tobacco", "cigarette", "smoke",

    # قنب / مخدرات
    "cannabis", "marijuana", "weed", "thc", "cbd",

    # كروز / شحن
    "cruise", "cruises", "shipping",

    # بالغ
    "adult", "xxx", "porn",

    # أفلام / سينما
    "cinema", "theater", "movie", "film"
]

BAD_NAME_KEYWORDS = [
    "etf", "fund", "trust", "warrant", "unit", "rights",
    "preferred", "depositary", "notes", "bond",
    "spdr", "ishares", "proshares", "invesco", "vanguard"
]

# =========================
# HELPERS
# =========================

@app.route("/")
def home():
    return "Alpaca Master List Bot Running"


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
    }


def is_clean_symbol(symbol):
    if not isinstance(symbol, str):
        return False
    if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
        return False
    if len(symbol) > 5:
        return False
    if not symbol.isalpha():
        return False
    return True


def is_blacklisted(symbol, name=""):
    text = f"{symbol} {name}".lower()

    if symbol.upper() in BLACKLIST_SYMBOLS:
        return True

    for word in BLACKLIST_KEYWORDS:
        if word in text:
            return True

    for word in BAD_NAME_KEYWORDS:
        if word in text:
            return True

    return False


def liquidity_filter(price, volume, change_pct):
    dollar_volume = price * volume

    if volume >= MIN_TODAY_VOLUME and dollar_volume >= MIN_DOLLAR_VOLUME:
        return True

    # استثناء للسهم اللي بدأ يتحرك بقوة
    if change_pct >= 5 and volume >= 75_000 and dollar_volume >= 250_000:
        return True

    return False


def chunk_list(items, size=100):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# =========================
# FETCH ASSETS FROM ALPACA
# =========================

def fetch_alpaca_assets():
    url = f"{ALPACA_TRADING_BASE}/v2/assets"

    res = requests.get(
        url,
        headers=alpaca_headers(),
        params={"status": "active", "asset_class": "us_equity"},
        timeout=30
    )

    res.raise_for_status()
    assets = res.json()

    clean_assets = []

    for a in assets:
        symbol = a.get("symbol", "")
        name = a.get("name", "")
        exchange = a.get("exchange", "")

        if exchange != "NASDAQ":
            continue

        if not a.get("tradable", False):
            continue

        if not is_clean_symbol(symbol):
            continue

        if is_blacklisted(symbol, name):
            continue

        clean_assets.append({
            "symbol": symbol,
            "name": name,
            "exchange": exchange
        })

    print(f"✅ Clean Alpaca Assets: {len(clean_assets)}", flush=True)
    return clean_assets


# =========================
# FETCH SNAPSHOTS
# =========================

def fetch_snapshots(symbols):
    url = f"{ALPACA_DATA_BASE}/v2/stocks/snapshots"
    all_data = {}

    for batch in chunk_list(symbols, 100):
        try:
            res = requests.get(
                url,
                headers=alpaca_headers(),
                params={"symbols": ",".join(batch)},
                timeout=20
            )

            if res.status_code != 200:
                print("Snapshot error:", res.text[:200], flush=True)
                continue

            data = res.json()
            snapshots = data.get("snapshots", {})

            all_data.update(snapshots)

            time.sleep(0.15)

        except Exception as e:
            print("Snapshot batch error:", e, flush=True)
            continue

    return all_data


# =========================
# BUILD MASTER LIST
# =========================

def fetch_master_list():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("❌ Alpaca keys missing", flush=True)
        return []

    assets = fetch_alpaca_assets()
    symbols = [a["symbol"] for a in assets]
    asset_map = {a["symbol"]: a for a in assets}

    snapshots = fetch_snapshots(symbols)

    candidates = []

    for symbol, snap in snapshots.items():
        try:
            daily = snap.get("dailyBar") or {}
            prev_daily = snap.get("prevDailyBar") or {}
            latest_trade = snap.get("latestTrade") or {}
            minute = snap.get("minuteBar") or {}

            price = (
                latest_trade.get("p")
                or minute.get("c")
                or daily.get("c")
            )

            volume = daily.get("v", 0) or 0
            prev_close = prev_daily.get("c")
            prev_volume = prev_daily.get("v", 0) or 0

            if price is None or prev_close is None:
                continue

            price = float(price)
            volume = float(volume)
            prev_close = float(prev_close)
            prev_volume = float(prev_volume)

            if not (PRICE_MIN <= price <= PRICE_MAX):
                continue

            change_pct = ((price - prev_close) / prev_close) * 100 if prev_close > 0 else 0
            dollar_volume = price * volume
            rel_volume = volume / prev_volume if prev_volume > 0 else 1

            if not liquidity_filter(price, volume, change_pct):
                continue

            score = (
                abs(change_pct) * 2
                + rel_volume * 3
                + dollar_volume / 1_000_000
            )

            candidates.append({
                "symbol": symbol,
                "price": round(price, 4),
                "volume": int(volume),
                "dollar_volume": round(dollar_volume, 2),
                "change_pct": round(change_pct, 2),
                "rel_volume": round(rel_volume, 2),
                "score": round(score, 2),
                "name": asset_map.get(symbol, {}).get("name", "")
            })

        except Exception:
            continue

    ranked = sorted(candidates, key=lambda x: x["score"], reverse=True)
    top = ranked[:TOP_N]

    final_symbols = [x["symbol"] for x in top]

    print(f"🔥 Final Master List: {len(final_symbols)} symbols", flush=True)
    print(f"Top 20: {final_symbols[:20]}", flush=True)

    return final_symbols


# =========================
# SAVE TO GIST
# =========================

def save_master_list_to_gist(symbols):
    if not GIST_ID or not GITHUB_TOKEN:
        print("❌ GIST_ID or GITHUB_TOKEN missing", flush=True)
        return

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"

        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        content = json.dumps(symbols, ensure_ascii=False)

        res = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    MASTER_LIST_FILE: {
                        "content": content
                    }
                }
            },
            timeout=15
        )

        if res.status_code not in [200, 201]:
            print("❌ Gist save failed:", res.text[:300], flush=True)
            return

        print(f"✅ Saved {len(symbols)} symbols to {MASTER_LIST_FILE}", flush=True)

    except Exception as e:
        print("❌ Save master list error:", e, flush=True)


# =========================
# RUN
# =========================

def run_once():
    symbols = fetch_master_list()

    if symbols:
        save_master_list_to_gist(symbols)
    else:
        print("⚠️ No symbols found, not saving", flush=True)


def run_loop():
    print("🚀 ALPACA MASTER LIST BOT LOOP STARTED", flush=True)

    while True:
        try:
            run_once()
            time.sleep(RUN_INTERVAL)
        except Exception as e:
            print("Main loop error:", e, flush=True)
            time.sleep(60)


threading.Thread(target=run_loop, daemon=True).start()

print("🌐 MASTER LIST WEB SERVICE STARTED", flush=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
