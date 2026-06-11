import os
import time
from datetime import datetime
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

GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN")
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
EXPLOSION_CANDIDATE_MIN_SCORE = 70

BATCH_SIZE         = 250
BATCH_DELAY_SEC    = 1.0

SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC", "PNC", "COF", "DFS",
    "MET", "PRU", "ALL", "AIG", "AFL", "TRV", "HIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR",
    "BUD", "TAP", "STZ", "DEO", "PM", "MO", "BTI",
    "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL", "AMC", "CNK", "IMAX"
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
gist_lock = threading.Lock()
session_closed_reports = []
report_lock = threading.Lock()
final_session_report_sent = False
last_session_date = None

SCAN_INTERVAL_SEC  = 180
TRACK_INTERVAL_SEC = 10
ALERT_COOLDOWN_SEC = 3600

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram-Sim] {text}", flush=True)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
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

def check_explosion(api, symbol, asset_name):
    if symbol in SYMBOL_BLACKLIST:
        return None

    name_lower = asset_name.lower()
    if any(kw in name_lower for kw in BAD_NAME_KEYWORDS):
        return None

    try:
        bars = api.get_bars(
            symbol,
            tradeapi.rest.TimeFrame.Day,
            limit=60,
            adjustment="raw"
        ).df

        if bars is None or bars.empty or len(bars) < 25:
            return None

        bars = bars.sort_index()

        today_bar = bars.iloc[-1]
        previous_bars = bars.iloc[:-1]

        if len(previous_bars) < 20:
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

        today_vol = float(
            snapshot.daily_bar.volume
        )

        prev_close = float(
            previous_bars["close"].iloc[-1]
        ) 
        
        if not (PRICE_MIN <= current_price <= PRICE_MAX):
            return None

        avg_vol_20 = float(previous_bars["volume"].tail(20).mean())
        float_tier = get_float_tier(avg_vol_20)

        if avg_vol_20 < MIN_AVG_VOL or avg_vol_20 > MAX_AVG_VOL:
            return None

        resistance_20 = float(previous_bars["high"].tail(20).max())
        resistance_50 = float(previous_bars["high"].tail(50).max())

        atr_14 = calculate_atr_14(previous_bars)

        price_change_pct = ((current_price - prev_close) / prev_close) * 100

        if price_change_pct < MIN_PRICE_CHANGE:
            return None

        rvol = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0

        if rvol < RVOL_MIN:
            return None

        dollar_volume = today_vol * current_price

        if dollar_volume < MIN_DOLLAR_VOLUME:
            return None

        if current_price < resistance_20 * 0.99:
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

        score = 0

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

        if vol_acceleration >= 2.0:
            score += 20
        elif vol_acceleration >= 1.2:
            score += 10

        if dollar_volume >= 1_000_000:
            score += 10
        elif dollar_volume >= MIN_DOLLAR_VOLUME:
            score += 5

        if score < EXPLOSION_CANDIDATE_MIN_SCORE:
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
            trade = api.get_latest_trade(
                symbol
            )

            current_p = trade.price

            failed_attempts = 0

            current_gain = ((current_p - entry_price) / entry_price) * 100

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
                            "max_gain": max_gain_pct
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
        f"🧬 *تصنيف الفلوت التقريبي:* `{res['float_tier']}`\n"
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

    print("🚀 [رادار النخبة] بدأ العمل بكامل الفلاتر المحدثة والقائمة السوداء الحقيقية...", flush=True)

    api = tradeapi.REST(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
        ALPACA_BASE_URL,
        api_version='v2'
    )

    while True:
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

                    result = check_explosion(api, sym, asset.name)

                    if result and result.get("explosion_candidate") is True:
                        send_explosion_alert(result)

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

            print(
                f"✅ [انتهاء الفحص الشامل] التنبيهات النخبة المرسلة بهذه الدورة: {alerts_sent} | إجمالي الفحوصات: {total_scans_performed}",
                flush=True
            )

        except Exception as e:
            print(f"❌ Main Loop Error: {e}", flush=True)

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    send_telegram_message("🟢 تم تشغيل بوت رادار النخبة بنجاح على سيرفر Render وبدأ مراقبة السوق الآن!")

    main_scanner()
