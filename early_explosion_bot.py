import os
import time
import datetime
import pytz
import threading
import requests
import alpaca_trade_api as tradeapi
from flask import Flask

# ==============================================================================
# 1. إعداد سيرفر Flask لإبقاء السيرفر مستيقظاً على Render
# ==============================================================================
app = Flask(__name__)

# متغيرات لمراقبة الحالة الحية من المتصفح
total_scans_performed = 0
last_scan_timestamp = "Never"

# ==============================================================================
# 1. إعداد سيرفر Flask لإبقاء السيرفر مستيقظاً على Render
# ==============================================================================
app = Flask(__name__)

# متغيرات لمراقبة الحالة الحية من المتصفح
total_scans_performed = 0
last_scan_timestamp = "Never"

@app.route('/')
def home():
    global total_scans_performed, last_scan_timestamp
    status_msg = (
        f"⚡ Early Explosion Radar is Running Perfectly 24/7!<br>"
        f"📊 Total Market Scans: {total_scans_performed}<br>"
        f"⏱️ Last Scan Time (Riyadh): {last_scan_timestamp}"
    )
    return status_msg, 200

def run_flask():
    port = int(os.getenv("PORT", 10000))
    # التعديل هنا: أضفنا use_reloader=False لمنع الـ Debug mode من تعليق السيرفر
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)
    
# ==============================================================================
# 2. الإعدادات والمتغيرات البيئية (Environment Variables)
# ==============================================================================
ALPACA_API_KEY      = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET_KEY   = os.getenv("APCA_API_SECRET_KEY")
ALPACA_BASE_URL     = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")

GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN")
GIST_ID             = os.getenv("GIST_ID")

# ==============================================================================
# 3. إعدادات الفلاتر المتفق عليها والقائمة السوداء الخاصة بك تماماً
# ==============================================================================
PRICE_MIN          = 0.3
PRICE_MAX          = 25.0
MIN_AVG_VOL        = 50_000
MAX_AVG_VOL        = 5_000_000  
RVOL_MIN           = 1.8        
MIN_PRICE_CHANGE   = 4.0        

MOMENTUM_RVOL_MIN             = 1.2
MOMENTUM_PRICE_CHANGE_MIN     = 3.0

# الفلتر الحاسم للدرجة النخبة
EXPLOSION_CANDIDATE_MIN_SCORE = 80 

# حجم الدفعة والاستراحة لمنع الـ Rate Limit
BATCH_SIZE         = 200 
BATCH_DELAY_SEC    = 0.5

# 🌟 القائمة السوداء الصحيحة والخاصة بك تماماً (تم دمجها) 🌟
SYMBOL_BLACKLIST = {
    "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC", "PNC", "COF", "DFS",
    "MET", "PRU", "ALL", "AIG", "AFL", "TRV", "HIG", "CB",
    "DKNG", "PENN", "WYNN", "LVS", "MGM", "CZR",
    "BUD", "TAP", "STZ", "DEO", "PM", "MO", "BTI",
    "CGC", "TLRY", "ACB", "SNDL", "CRON",
    "NCLH", "CCL", "RCL", "AMC", "CNK", "IMAX"
}

# الكلمات الدلالية المستبعدة للأسماء الخاصة بك
BAD_NAME_KEYWORDS = [
    "etf", "fund", "trust", "warrant", "unit", "right", "preferred",
    "bond", "notes", "income", "index", "acquisition", "blank check",
    "spac", "bank", "bancorp", "credit", "lending", "loan", "mortgage",
    "insurance", "casino", "gambling", "betting", "sportsbook",
    "alcohol", "beer", "wine", "tobacco", "cannabis", "marijuana",
    "hemp", "cruise", "cinema", "movie", "theater"
]

# هياكل البيانات للتحكم بالخيوط (Threading)
active_monitors = {}              
sent_alerts = {}                  
SCAN_INTERVAL_SEC  = 180          # المسح الشامل للسوق كل 3 دقائق
TRACK_INTERVAL_SEC = 10           # المراقبة الشرسة لكل سهم ينفجر (كل 10 ثوانٍ)
ALERT_COOLDOWN_SEC = 3600         # منع إعادة إرسال نفس السهم لمدة ساعة

# ==============================================================================
# 4. الدوال المساعدة (تليجرام، جيت هاب، وألباكا)
# ==============================================================================
def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram-Sim] {text}", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"❌ Telegram Error: {e}", flush=True)

def update_gist_state(symbol, data_dict):
    if not GITHUB_TOKEN or not GIST_ID:
        return
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        current_content = {}
        if res.status_code == 200:
            files = res.json().get("files", {})
            if "bot_state.json" in files:
                import json
                try: current_content = json.loads(files["bot_state.json"]["content"])
                except: current_content = {}
        
        current_content[symbol] = data_dict
        import json
        payload = {"files": {"bot_state.json": {"content": json.dumps(current_content, indent=4)}}}
        requests.patch(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"❌ Gist Update Error: {e}", flush=True)

def is_scan_time_allowed():
    tz_ny = pytz.timezone("America/New_York")
    now_ny = datetime.datetime.now(tz_ny)
    if now_ny.weekday() >= 5:
        return False
    start_time = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
    end_time = now_ny.replace(hour=20, minute=0, second=0, microsecond=0)
    return start_time <= now_ny <= end_time

# ==============================================================================
# 5. محرك الفحص الرياضي (التحليل وتوليد الإشارات والنقاط)
# ==============================================================================
def check_explosion(api, symbol, asset_name):
    if symbol in SYMBOL_BLACKLIST:
        return None
        
    name_lower = asset_name.lower()
    if any(kw in name_lower for kw in BAD_NAME_KEYWORDS):
        return None

    try:
        bars = api.get_bars(symbol, tradeapi.rest.TimeFrame.Day, 
                            limit=21, adjustment='raw').df
        if bars.empty or len(bars) < 15:
            return None
        
        recent_bars = bars.iloc[:-1]
        avg_vol_20 = recent_bars['volume'].mean()
        if not (MIN_AVG_VOL <= avg_vol_20 <= MAX_AVG_VOL):
            return None
        
        resistance_20 = recent_bars['high'].max()
        
        today_bar = bars.iloc[-1]
        current_price = today_bar['close']
        today_vol = today_bar['volume']
        open_price = today_bar['open']
        
        if not (PRICE_MIN <= current_price <= PRICE_MAX):
            return None
        
        price_change_pct = ((current_price - open_price) / open_price) * 100
        if price_change_pct < MIN_PRICE_CHANGE:
            return None
            
        rvol = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0
        if rvol < RVOL_MIN:
            return None
            
        if current_price < resistance_20 * 0.99:
            return None

        bars_5m = api.get_bars(symbol, tradeapi.rest.TimeFrame.Minute, 
                               limit=6, adjustment='raw').df
        vol_acceleration = 1.0
        if not bars_5m.empty and len(bars_5m) >= 2:
            last_5m_vol = bars_5m.iloc[-1]['volume']
            prev_5m_vol = bars_5m.iloc[-2]['volume']
            if prev_5m_vol > 0:
                vol_acceleration = last_5m_vol / prev_5m_vol

        score = 0
        if rvol >= 3.0: score += 30
        elif rvol >= RVOL_MIN: score += 20
        
        if price_change_pct >= 15: score += 30
        elif price_change_pct >= 8: score += 20
        elif price_change_pct >= MIN_PRICE_CHANGE: score += 10
        
        if current_price >= resistance_20: score += 20
        else: score += 10
        
        if vol_acceleration >= 2.0: score += 20
        elif vol_acceleration >= MOMENTUM_RVOL_MIN: score += 10
        
        dollar_volume = today_vol * current_price
        if dollar_volume > 1_000_000: score += 10
        
        if score < EXPLOSION_CANDIDATE_MIN_SCORE:
            return None

        stop_loss = round(current_price * 0.93, 2)
        target1   = round(current_price * 1.10, 2)
        target2   = round(current_price * 1.25, 2)
        target3   = round(current_price * 1.50, 2)

        return {
            "symbol": symbol, "price": round(current_price, 2), "rvol": round(rvol, 2),
            "change_pct": round(price_change_pct, 2), "score": score,
            "target1": target1, "target2": target2, "target3": target3, "stop_loss": stop_loss,
            "explosion_candidate": True
        }
        
    except Exception as e:
        return None

# ==============================================================================
# 6. خيط المراقبة اللحظية الشرسة (مستقل لكل سهم متفجر)
# ==============================================================================
def dedicated_ticker_tracker(symbol, entry_price, t1, t2, t3, sl):
    print(f"🎯 [بدء المراقبة اللحظية الشرسة] خيط مستقل انطلق لملاحقة سهم: {symbol}", flush=True)
    api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')
    
    h1_hit, h2_hit, h3_hit = False, False, False
    max_gain_pct = 0.0
    
    update_gist_state(symbol, {
        "entry_price": entry_price, "t1": t1, "t2": t2, "t3": t3, "sl": sl,
        "h1_hit": h1_hit, "h2_hit": h2_hit, "h3_hit": h3_hit, "max_gain": 0.0, "status": "active"
    })

    while True:
        if not is_scan_time_allowed():
            print(f"💤 [إيقاف المراقبة] خروج مؤقت لسهم {symbol} بسبب إغلاق الجلسة.", flush=True)
            break
            
        try:
            trade = api.get_latest_trade(symbol)
            current_p = trade.price
            
            current_gain = ((current_p - entry_price) / entry_price) * 100
            if current_gain > max_gain_pct:
                max_gain_pct = round(current_gain, 2)

            if current_p <= sl:
                msg = f"🚨 *[{symbol}] ضرب وقف الخسارة!* 🚨\n• سعر الخروج: ${current_p}\n• خسارة: {round(current_gain, 2)}%\n• أعلى ربح وصل له: {max_gain_pct}%"
                send_telegram_message(msg)
                update_gist_state(symbol, {"status": "stopped_by_sl", "exit_price": current_p, "max_gain": max_gain_pct})
                break
                
            if current_p >= t1 and not h1_hit:
                h1_hit = True
                msg = f"✅ *[{symbol}] تحقق الهدف الأول تبارك الله! (10%+)*\n• السعر الحالي: ${current_p}\n• الدخول: ${entry_price}\n• الأهداف القادمة: ${t2} | ${t3}"
                send_telegram_message(msg)
                update_gist_state(symbol, {"h1_hit": True, "max_gain": max_gain_pct})
                
            if current_p >= t2 and not h2_hit:
                h2_hit = True
                msg = f"🔥 *[{symbol}] انفجار مستمر! تحقق الهدف الثاني (25%+)*\n• السعر الحالي: ${current_p}\n• أرباح ممتازة، تذكر تأمين صفقاتك!"
                send_telegram_message(msg)
                update_gist_state(symbol, {"h2_hit": True, "max_gain": max_gain_pct})
                
            if current_p >= t3 and not h3_hit:
                h3_hit = True
                msg = f"🚀🚀 *[{symbol}] القناص ضرب الهدف الثالث الأسطوري (50%+)!*\n• السعر الحالي: ${current_p}\n• السهم مفرقع بالكامل اليوم!"
                send_telegram_message(msg)
                update_gist_state(symbol, {"h3_hit": True, "max_gain": max_gain_pct})
                break
                
        except Exception as e:
            print(f"⚠️ Error tracking {symbol}: {e}", flush=True)
            
        time.sleep(TRACK_INTERVAL_SEC)
        
    if symbol in active_monitors:
        del active_monitors[symbol]

def send_explosion_alert(res):
    msg = (
        f"🌟 *[إشارة انفجار ذهبية نخبة]* 🌟\n\n"
        f"🎫 *السهم:* `{res['symbol']}`\n"
        f"💵 *سعر الدخول:* `${res['price']}`\n"
        f"📊 *التغير اليومي:* `+{res['change_pct']}%`\n"
        f"ركائز القوة اللحظية:\n"
        f"🔥 *قوة الانفجار (Score):* `{res['score']}/100`\n"
        f"📈 *الـ RVOL الحالي:* `{res['rvol']}x`\n\n"
        f"🎯 *الأهداف الفنية المحسوبة:*\n"
        f" ├─ Target 1 (10%): `${res['target1']}`\n"
        f" ├─ Target 2 (25%): `${res['target2']}`\n"
        f" └─ Target 3 (50%): `${res['target3']}`\n\n"
        f"🛑 *وقف الخسارة الصارع (7%-):* `${res['stop_loss']}`\n"
        f"⏱️ _بدأت الآن خيوط المطاردة الشرسة كل 10 ثوانٍ._"
    )
    send_telegram_message(msg)

# ==============================================================================
# 7. المحرك الرئيسي للرادار (Main Loop المحدث بالدفعات)
# ==============================================================================
def main_scanner():
    global total_scans_performed, last_scan_timestamp
    print("🚀 [رادار النخبة] بدأ العمل بكامل الفلاتر المحدثة والقائمة السوداء الحقيقية...", flush=True)
    api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')
    
    while True:
        if not is_scan_time_allowed():
            print("⏸️ Scan skipped: outside US premarket/market hours. Sleeping...", flush=True)
            time.sleep(60)
            continue
            
        print(f"🔍 [بدء مسح شامل للسوق] جاري جلب ومطابقة الأسهم...", flush=True)
        try:
            assets = api.list_assets(status='active')
            tradable_assets = [a for a in assets if a.tradable and a.fractionable]
            
            now_ts = time.time()
            alerts_sent = 0
            
            for i in range(0, len(tradable_assets), BATCH_SIZE):
                batch = tradable_assets[i:i + BATCH_SIZE]
                
                for asset in batch:
                    sym = asset.symbol
                    if sym in active_monitors: continue
                    if sym in sent_alerts and (now_ts - sent_alerts[sym] < ALERT_COOLDOWN_SEC): continue
                    
                    result = check_explosion(api, sym, asset.name)
                    
                    if result and result.get("explosion_candidate") is True:
                        send_explosion_alert(result)
                        sent_alerts[sym] = now_ts
                        alerts_sent += 1
                        
                        if sym not in active_monitors:
                            active_monitors[sym] = True
                            t = threading.Thread(
                                target=dedicated_ticker_tracker,
                                args=(sym, result["price"], result["target1"], result["target2"], result["target3"], result["stop_loss"]),
                                daemon=True
                            )
                            t.start()
                
                time.sleep(BATCH_DELAY_SEC)
                        
            total_scans_performed += 1
            last_scan_timestamp = datetime.datetime.now(saudi_tz).strftime("%Y-%m-%d %H:%M:%S")
            print(f"✅ [انتهاء الفحص الشامل] التنبيهات النخبة المرسلة بهذه الدورة: {alerts_sent} | إجمالي الفحوصات: {total_scans_performed}", flush=True)
            
        except Exception as e:
            print(f"❌ Main Loop Error: {e}", flush=True)
            
        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # سطر اختبار تليجرام فور التشغيل (اختياري لطمأنتك)
    send_telegram_message("🟢 تم تشغيل بوت رادار النخبة بنجاح على سيرفر Render وبدأ مراقبة السوق الآن!")
    
    main_scanner()
