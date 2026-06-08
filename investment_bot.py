import os
import time
import json
import requests
import threading
import pandas as pd
import alpaca_trade_api as tradeapi
from flask import Flask
from datetime import datetime, timedelta
import pytz

# ─── إعدادات ───────────────────────────────────────────
API_KEY    = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GIST_ID      = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

api      = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)
app      = Flask(__name__)
saudi_tz = pytz.timezone("Asia/Riyadh")
ny_tz    = pytz.timezone("America/New_York")

# ─── ثوابت ─────────────────────────────────────────────
PRICE_MIN          = 0.3
PRICE_MAX          = 25.0
MIN_AVG_VOL        = 50_000
MAX_AVG_VOL        = 800_000
RVOL_MIN           = 3.0
MIN_PRICE_CHANGE   = 5.0
BATCH_SIZE         = 100
SCAN_INTERVAL      = 60
FULL_SCAN_INTERVAL = 10 * 60
REPEAT_BLOCK_HOURS = 12
STATE_FILE         = "explosion_state.json"

SYMBOL_BLACKLIST = {
    "JPM","BAC","WFC","C","GS","MS","AXP","USB","TFC","PNC","COF","DFS",
    "MET","PRU","ALL","AIG","AFL","TRV","HIG","CB",
    "DKNG","PENN","WYNN","LVS","MGM","CZR",
    "BUD","TAP","STZ","DEO","PM","MO","BTI",
    "CGC","TLRY","ACB","SNDL","CRON",
    "NCLH","CCL","RCL","AMC","CNK","IMAX",
}

BAD_NAME_KEYWORDS = [
    "etf","fund","trust","warrant","unit","right","preferred",
    "bond","notes","income","index","acquisition","blank check",
    "spac","bank","bancorp","credit","lending","loan","mortgage",
    "insurance","casino","gambling","betting","sportsbook",
    "alcohol","beer","wine","tobacco","cannabis","marijuana",
    "hemp","cruise","cinema","movie","theater",
]

sent_alerts = {}

@app.route("/")
def home():
    return "Explosion Bot Running"

def now_saudi():
    return datetime.now(saudi_tz)

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def gist_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def save_state(data):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"save_state local error: {e}", flush=True)

    if not GIST_ID or not GITHUB_TOKEN:
        return

    try:
        requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=gist_headers(),
            json={
                "files": {
                    STATE_FILE: {
                        "content": json.dumps(data, ensure_ascii=False, indent=2)
                    }
                }
            },
            timeout=20,
        )
    except Exception as e:
        print(f"save_state gist error: {e}", flush=True)

def load_state():
    if GIST_ID and GITHUB_TOKEN:
        try:
            r = requests.get(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=gist_headers(),
                timeout=20,
            )

            if r.status_code == 200:
                content = r.json().get("files", {}).get(STATE_FILE, {}).get("content", "")
                if content:
                    return json.loads(content)

        except Exception as e:
            print(f"load_state gist error: {e}", flush=True)

    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"load_state local error: {e}", flush=True)

    return {"sent_alerts": {}}

def is_clean_symbol(symbol):
    symbol = str(symbol).upper().strip()

    if not symbol or len(symbol) > 5:
        return False

    if not symbol.isalpha():
        return False

    if any(c in symbol for c in "./-^"):
        return False

    if symbol.endswith(("W","U","R","P","Q","Z")):
        return False

    if symbol in SYMBOL_BLACKLIST:
        return False

    return True

def get_all_symbols():
    symbols = []

    try:
        assets = api.list_assets(status="active")

        for asset in assets:
            symbol = getattr(asset, "symbol", "")
            name   = getattr(asset, "name", "") or ""

            if not symbol:
                continue

            symbol = symbol.upper().strip()

            if not getattr(asset, "tradable", False):
                continue

            if not is_clean_symbol(symbol):
                continue

            if any(k in name.lower() for k in BAD_NAME_KEYWORDS):
                continue

            symbols.append(symbol)

        symbols = list(set(symbols))
        print(f"✅ Total symbols after filter: {len(symbols)}", flush=True)

    except Exception as e:
        print(f"get_all_symbols error: {e}", flush=True)

    return symbols

def get_prices_bulk(symbols):
    prices = {}

    try:
        snapshots = api.get_snapshots(symbols)

        for symbol, snap in snapshots.items():
            price = None

            if snap.latest_trade:
                price = getattr(snap.latest_trade, "price", None)

            if price is None and snap.daily_bar:
                price = getattr(snap.daily_bar, "close", None)

            if price is not None:
                prices[symbol.upper()] = float(price)

    except Exception as e:
        print(f"get_prices_bulk error: {e}", flush=True)

    return prices

def filter_by_price(symbols):
    filtered = []

    for i in range(0, len(symbols), BATCH_SIZE):
        batch  = symbols[i:i + BATCH_SIZE]
        prices = get_prices_bulk(batch)

        for s in batch:
            p = prices.get(s)

            if p and PRICE_MIN <= p <= PRICE_MAX:
                filtered.append(s)

        time.sleep(0.2)

    print(f"✅ After price filter: {len(filtered)}", flush=True)
    return filtered

def get_daily_bars(symbols, limit=30):
    bars_map = {}

    try:
        bars = api.get_bars(
            symbols,
            tradeapi.TimeFrame.Day,
            limit=limit,
            adjustment="raw",
        ).df

        if bars is None or bars.empty:
            return bars_map

        if isinstance(bars.index, pd.MultiIndex):
            for sym in symbols:
                try:
                    df = bars.xs(sym)

                    if df is not None and not df.empty:
                        bars_map[sym] = df.copy()

                except Exception:
                    continue

        elif "symbol" in bars.columns:
            for sym, df in bars.groupby("symbol"):
                bars_map[sym] = df.copy()

    except Exception as e:
        print(f"get_daily_bars error: {e}", flush=True)

    return bars_map

def get_intraday_volume_bulk(symbols):
    volumes = {}

    if not symbols:
        return volumes

    try:
        now_ny = datetime.now(ny_tz)

        start_ny = now_ny.replace(
            hour=4,
            minute=0,
            second=0,
            microsecond=0,
        )

        bars = api.get_bars(
            symbols,
            tradeapi.TimeFrame(5, tradeapi.TimeFrameUnit.Minute),
            start=start_ny.isoformat(),
            end=now_ny.isoformat(),
            adjustment="raw",
        ).df

        if bars is None or bars.empty:
            return volumes

        if isinstance(bars.index, pd.MultiIndex):
            for sym in symbols:
                try:
                    df = bars.xs(sym)

                    if df is not None and not df.empty and "volume" in df.columns:
                        volumes[sym.upper()] = float(df["volume"].sum())

                except Exception:
                    continue

        elif "symbol" in bars.columns:
            for sym, df in bars.groupby("symbol"):
                if df is not None and not df.empty and "volume" in df.columns:
                    volumes[str(sym).upper()] = float(df["volume"].sum())

    except Exception as e:
        print(f"get_intraday_volume_bulk error: {e}", flush=True)

    return volumes

def check_explosion(symbol, df, current_price, intraday_volume):
    try:
        if df is None or df.empty or len(df) < 10:
            return None

        if "close" not in df.columns or "volume" not in df.columns:
            return None

        close  = df["close"]
        volume = df["volume"]

        prev_close = float(close.iloc[-2])
        avg_vol_20 = float(volume.iloc[:-1].tail(20).mean())
        today_vol  = float(intraday_volume or 0)

        if not (MIN_AVG_VOL <= avg_vol_20 <= MAX_AVG_VOL):
            return None

        if avg_vol_20 <= 0:
            return None

        if today_vol <= 0:
            return None

        rvol = today_vol / avg_vol_20

        if rvol < RVOL_MIN:
            return None

        if prev_close <= 0:
            return None

        price_change_pct = ((current_price - prev_close) / prev_close) * 100

        if price_change_pct < MIN_PRICE_CHANGE:
            return None

        resistance_20 = float(close.iloc[:-1].tail(20).max())

        if current_price < resistance_20 * 1.01:
            return None

        return {
            "symbol":           symbol,
            "price":            round(current_price, 4),
            "prev_close":       round(prev_close, 4),
            "price_change_pct": round(price_change_pct, 2),
            "rvol":             round(rvol, 2),
            "avg_vol_20":       int(avg_vol_20),
            "today_vol":        int(today_vol),
            "resistance_20":    round(resistance_20, 4),
        }

    except Exception as e:
        print(f"check_explosion error {symbol}: {e}", flush=True)
        return None

def send_explosion_alert(result):
    sym   = result["symbol"]
    price = result["price"]
    chg   = result["price_change_pct"]
    rvol  = result["rvol"]
    avg_v = result["avg_vol_20"]
    t_v   = result["today_vol"]
    res   = result["resistance_20"]

    msg = (
        f"🔥 <b>بداية اشتعال سهم - {sym}</b>\n\n"
        f"💰 السعر الحالي: {price}\n"
        f"📈 الارتفاع عن إغلاق أمس: +{chg}%\n"
        f"🔥 RVOL intraday: {rvol}x المعدل\n"
        f"📊 حجم اليوم الفعلي intraday: {t_v:,}\n"
        f"📊 معدل الحجم 20 يوم: {avg_v:,}\n"
        f"🧱 اختراق مقاومة 20 يوم: {res}\n\n"
        f"⚠️ سهم سريع الحركة — تقلب عالٍ جداً\n"
        f"🔗 https://www.tradingview.com/chart/?symbol={sym}"
    )

    send_telegram(msg)
    print(f"🔥 Alert sent: {sym} | +{chg}% | RVOL={rvol}x", flush=True)

def full_scan():
    print("🔎 Full scan started...", flush=True)

    symbols = get_all_symbols()
    symbols = filter_by_price(symbols)

    now_ts = time.time()
    alerts_sent = 0

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]

        prices           = get_prices_bulk(batch)
        bars_map         = get_daily_bars(batch, limit=30)
        intraday_volumes = get_intraday_volume_bulk(batch)

        for sym in batch:
            last_alert = sent_alerts.get(sym, 0)

            if now_ts - last_alert < REPEAT_BLOCK_HOURS * 3600:
                continue

            cp = prices.get(sym)

            if cp is None:
                continue

            df = bars_map.get(sym)
            intraday_volume = intraday_volumes.get(sym, 0)

            result = check_explosion(
                symbol=sym,
                df=df,
                current_price=cp,
                intraday_volume=intraday_volume,
            )

            if result:
                send_explosion_alert(result)
                sent_alerts[sym] = now_ts
                alerts_sent += 1

        time.sleep(0.3)

    print(f"✅ Full scan done | alerts={alerts_sent}", flush=True)

    save_state({
        "sent_alerts": {
            k: v for k, v in sent_alerts.items()
        }
    })

def quick_monitor():
    if not sent_alerts:
        return

    recent = [
        sym for sym, ts in sent_alerts.items()
        if time.time() - ts < 3 * 3600
    ]

    if not recent:
        return

    prices = get_prices_bulk(recent)

    for sym in recent:
        cp = prices.get(sym)

        if cp is None:
            continue

        print(f"👁️ Monitor: {sym} = {cp}", flush=True)

def run_bot():
    global sent_alerts

    state = load_state()
    sent_alerts = state.get("sent_alerts", {})

    last_full_scan = 0

    while True:
        try:
            now_ts = time.time()

            if now_ts - last_full_scan >= FULL_SCAN_INTERVAL:
                full_scan()
                last_full_scan = time.time()

            quick_monitor()

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"run_bot error: {e}", flush=True)
            time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()

    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
