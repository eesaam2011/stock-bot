import os
import time
import json
import html
import requests
import threading
import xml.etree.ElementTree as ET
from flask import Flask
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import pytz

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

saudi_tz = pytz.timezone("Asia/Riyadh")

SCAN_INTERVAL = 900  # كل 15 دقيقة

NEWS_FILE = "news_signals.json"

BOT2_CANDIDATES_FILE = "bot2_news_candidates.json"
BOT3_RESULTS_FILE = "bot3_early_candidates.json"
INVESTMENT_NEWS_50_FILE = "investment_news_candidates_50.json"

TOP_TOP_NEWS_SCORE = 18
MAX_ALERT_NEWS_AGE_HOURS = 6
MAX_STORE_NEWS_AGE_SECONDS = 7 * 86400

SPEC_SYMBOL_LIMIT = 300
INVESTMENT_SYMBOL_LIMIT = 50

sent_news_alerts = {}

app = Flask(__name__)


@app.route("/")
def home():
    return "News Bot Running"


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def send_telegram_msg(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram keys missing", flush=True)
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e, flush=True)


def read_gist_file(filename, default_value=None):
    if default_value is None:
        default_value = []

    if not GIST_ID or not GITHUB_TOKEN:
        return default_value

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()

        file_data = data.get("files", {}).get(filename)
        if not file_data:
            return default_value

        content = file_data.get("content", "")
        if not content:
            return default_value

        try:
            return json.loads(content)
        except Exception:
            return default_value

    except Exception as e:
        print(f"Gist read error ({filename}): {e}", flush=True)
        return default_value


def save_gist_file(filename, content_obj):
    if not GIST_ID or not GITHUB_TOKEN:
        print("Gist keys missing", flush=True)
        return

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        res = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    filename: {
                        "content": json.dumps(content_obj, ensure_ascii=False)
                    }
                }
            },
            timeout=10
        )

        if res.status_code not in [200, 201]:
            print(f"Gist save failed {filename}: {res.text[:300]}", flush=True)
            return

        print(f"Gist saved: {filename}", flush=True)

    except Exception as e:
        print(f"Gist save error ({filename}): {e}", flush=True)


def clean_symbol(symbol):
    if not symbol or not isinstance(symbol, str):
        return None

    symbol = symbol.upper().strip()

    if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
        return None

    if len(symbol) > 5:
        return None

    if not symbol.isalpha():
        return None

    return symbol


def extract_symbols_from_file(data, source_name, max_items=None):
    results = []
    now_ts = time.time()
    max_age_seconds = 2 * 60 * 60 if "SPEC" in source_name else 7 * 86400

    if not isinstance(data, list):
        return results

    for item in data:
        if isinstance(item, str):
            symbol = item
            item_time = now_ts
            score = 0
        elif isinstance(item, dict):
            symbol = item.get("symbol")
            item_time = float(
                item.get("time")
                or item.get("last_update")
                or item.get("first_seen")
                or now_ts
            )
            score = float(
                item.get("final_score")
                or item.get("technical_score")
                or item.get("pending_score")
                or item.get("score")
                or 0
            )
        else:
            continue

        if now_ts - item_time > max_age_seconds:
            continue

        symbol = clean_symbol(symbol)
        if not symbol:
            continue

        results.append({
            "symbol": symbol,
            "source": source_name,
            "score": score,
            "time": item_time
        })

    results = sorted(
        results,
        key=lambda x: (x["score"], x["time"]),
        reverse=True
    )

    if max_items:
        results = results[:max_items]

    return results


def load_news_symbols():
    now = datetime.now(saudi_tz)

    bot2_data = read_gist_file(BOT2_CANDIDATES_FILE, [])
    bot3_data = read_gist_file(BOT3_RESULTS_FILE, [])

    bot2_symbols = extract_symbols_from_file(
        bot2_data,
        "BOT2_SPEC",
        max_items=SPEC_SYMBOL_LIMIT
    )

    bot3_symbols = extract_symbols_from_file(
        bot3_data,
        "BOT3_SPEC",
        max_items=SPEC_SYMBOL_LIMIT
    )

    all_items = bot2_symbols + bot3_symbols

    # الاستثمار: الأربعاء فقط بعد تجهيز أفضل 50
    if now.weekday() == 2:
        investment_data = read_gist_file(INVESTMENT_NEWS_50_FILE, [])
        investment_symbols = extract_symbols_from_file(
            investment_data,
            "INVESTMENT",
            max_items=INVESTMENT_SYMBOL_LIMIT
        )
        all_items += investment_symbols

    deduped = {}

    for item in all_items:
        symbol = item["symbol"]

        old = deduped.get(symbol)
        if old is None:
            deduped[symbol] = item
        else:
            # إذا ظهر من أكثر من مصدر نخلي المصدر مركب ونحفظ أعلى score
            old_sources = set(str(old.get("source", "")).split("+"))
            old_sources.add(item["source"])
            old["source"] = "+".join(sorted(old_sources))
            old["score"] = max(old.get("score", 0), item.get("score", 0))
            old["time"] = max(old.get("time", 0), item.get("time", 0))

    symbols = sorted(
        deduped.values(),
        key=lambda x: (x["score"], x["time"]),
        reverse=True
    )

    print(
        f"✅ Loaded news candidates: {len(symbols)} "
        f"| Bot2: {len(bot2_symbols)} | Bot3: {len(bot3_symbols)} "
        f"| Investment included: {now.weekday() == 2}",
        flush=True
    )

    return symbols


def fetch_google_news(symbol):
    try:
        query = quote_plus(f"{symbol} stock")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        headers = {"User-Agent": "Mozilla/5.0"}

        res = requests.get(url, headers=headers, timeout=10)

        if res.status_code != 200:
            return []

        root = ET.fromstring(res.content)
        items = []

        for item in root.findall(".//item")[:7]:
            title = html.unescape(item.findtext("title", default="")).strip()
            link = item.findtext("link", default="")
            pub_date = item.findtext("pubDate", default="")

            age_hours = None

            try:
                dt = parsedate_to_datetime(pub_date)
                now_utc = datetime.now(dt.tzinfo)
                age_hours = (now_utc - dt).total_seconds() / 3600
            except Exception:
                pass

            if title:
                items.append({
                    "title": title,
                    "link": link,
                    "pub_date": pub_date,
                    "age_hours": age_hours
                })

        return items

    except Exception as e:
        print(f"Google news error {symbol}: {e}", flush=True)
        return []


def analyze_news_items(items):
    positive_keywords = {
        "fda approval": 6,
        "approval": 4,
        "contract": 4,
        "major contract": 6,
        "partnership": 4,
        "strategic partnership": 5,
        "collaboration": 3,
        "acquisition": 5,
        "merger": 5,
        "buyout": 7,
        "earnings beat": 5,
        "beats estimates": 5,
        "raises guidance": 6,
        "guidance raised": 6,
        "patent": 3,
        "launch": 3,
        "breakthrough": 5,
        "positive data": 6,
        "phase 3": 5,
        "phase 2": 3,
        "upgrade": 3,
        "price target raised": 3,
        "short squeeze": 4,
        "receives grant": 4,
        "secures funding": 4,
        "partnership agreement": 5,
        "commercial agreement": 5,
        "record revenue": 4,
        "analyst upgrade": 4
    }

    negative_keywords = {
        "offering": 6,
        "public offering": 7,
        "direct offering": 7,
        "dilution": 7,
        "bankruptcy": 8,
        "delisting": 8,
        "lawsuit": 4,
        "investigation": 5,
        "downgrade": 4,
        "misses estimates": 5,
        "cuts guidance": 6,
        "reverse split": 7,
        "sec investigation": 8,
        "fraud": 8
    }

    total_score = 0
    best_title = ""
    best_age = None
    strongest_negative = False

    for item in items:
        title = item["title"]
        text = title.lower()
        age_hours = item.get("age_hours")

        item_score = 0

        for kw, weight in positive_keywords.items():
            if kw in text:
                item_score += weight

        for kw, weight in negative_keywords.items():
            if kw in text:
                item_score -= weight
                if weight >= 7:
                    strongest_negative = True

        if age_hours is not None:
            if age_hours <= 1:
                item_score += 5
            elif age_hours <= 3:
                item_score += 4
            elif age_hours <= 6:
                item_score += 3
            elif age_hours <= 12:
                item_score += 1
            elif age_hours > 24:
                item_score -= 2

        if item_score > 0 and (not best_title or item_score > total_score):
            best_title = title
            best_age = age_hours

        if not best_title:
            best_title = title
            best_age = age_hours

        total_score += item_score

    if strongest_negative:
        return {
            "grade": "NEGATIVE",
            "label": "🔴 خبر سلبي / خطر",
            "score": total_score,
            "headline": best_title,
            "age_hours": best_age
        }

    if total_score >= TOP_TOP_NEWS_SCORE:
        grade = "SUPER_STRONG"
        label = "🔥🔥🔥🔥 خبر قوي جدًا جدًا جدًا جدًا"
    elif total_score >= 10:
        grade = "STRONG"
        label = "🔥 خبر قوي"
    elif total_score >= 5:
        grade = "MEDIUM"
        label = "🟢 خبر متوسط"
    elif total_score >= 1:
        grade = "WEAK"
        label = "⚪ خبر ضعيف"
    else:
        grade = "NONE"
        label = "⚪ لا يوجد خبر مؤثر"

    return {
        "grade": grade,
        "label": label,
        "score": total_score,
        "headline": best_title,
        "age_hours": best_age
    }


def save_news_to_gist(new_items):
    old_items = read_gist_file(NEWS_FILE, [])
    now_ts = time.time()

    if not isinstance(old_items, list):
        old_items = []

    old_items = [
        x for x in old_items
        if now_ts - float(x.get("time", 0)) < MAX_STORE_NEWS_AGE_SECONDS
    ]

    merged = old_items[:]

    existing_keys = {
        (x.get("symbol"), x.get("headline"))
        for x in merged
    }

    for item in new_items:
        key = (item.get("symbol"), item.get("headline"))

        if key not in existing_keys:
            merged.append(item)
            existing_keys.add(key)

    save_gist_file(NEWS_FILE, merged[-1000:])
    print(f"News gist saved: {len(new_items)} new items", flush=True)


def should_alert(symbol, headline):
    now = datetime.now(saudi_tz)
    key = f"{symbol}:{headline}"

    if key not in sent_news_alerts:
        sent_news_alerts[key] = now
        return True

    diff = (now - sent_news_alerts[key]).total_seconds() / 60

    if diff >= 240:
        sent_news_alerts[key] = now
        return True

    return False


def is_fresh_top_top_news(analysis):
    age_hours = analysis.get("age_hours")

    if age_hours is None:
        return False

    return (
        analysis["grade"] == "SUPER_STRONG"
        and analysis["score"] >= TOP_TOP_NEWS_SCORE
        and age_hours <= MAX_ALERT_NEWS_AGE_HOURS
    )


def is_spec_source(source):
    return "BOT2_SPEC" in source or "BOT3_SPEC" in source


def run_news_scanner():
    print("📰 Loading candidates from Bot2 / Bot3 / Investment...", flush=True)

    candidates = load_news_symbols()

    if not candidates:
        print("⚠️ No news candidates found", flush=True)
        return

    useful_news = []

    for i, item in enumerate(candidates, start=1):
        symbol = item["symbol"]
        source = item.get("source", "")

        try:
            print(f"📰 {i}/{len(candidates)} checking news: {symbol} | {source}", flush=True)

            news_items = fetch_google_news(symbol)

            if not news_items:
                time.sleep(0.2)
                continue

            analysis = analyze_news_items(news_items)

            if analysis["grade"] in ["SUPER_STRONG", "STRONG", "MEDIUM", "NEGATIVE"]:
                news_item = {
                    "symbol": symbol,
                    "source": source,
                    "news_grade": analysis["grade"],
                    "news_label": analysis["label"],
                    "news_score": analysis["score"],
                    "headline": analysis["headline"],
                    "age_hours": analysis.get("age_hours"),
                    "time": time.time()
                }

                useful_news.append(news_item)

                # الإرسال فقط للمضاربة Bot2/Bot3، وليس Investment
                if (
                    is_spec_source(source)
                    and is_fresh_top_top_news(analysis)
                    and should_alert(symbol, analysis["headline"])
                ):
                    msg = (
                        f"📰🔥🔥🔥🔥 *بوت الأخبار - خبر مضاربي قوي جدًا جدًا جدًا جدًا*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"📡 المصدر: {source}\n"
                        f"🗞️ التصنيف: {analysis['label']}\n"
                        f"⭐ News Score: {analysis['score']}\n"
                        f"⏱️ عمر الخبر: {analysis['age_hours']:.1f} ساعة\n\n"
                        f"🧠 العنوان:\n{analysis['headline']}\n\n"
                        f"📌 ملاحظة: هذا ليس دخول مباشر، لكنه خبر قوي جدًا يرجع لـ Bot 2 / Bot 3 للتأكيد.\n"
                        f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
                    )

                    send_telegram_msg(msg)

            time.sleep(0.2)

        except Exception as e:
            print(f"News scanner error {symbol}: {e}", flush=True)
            continue

    if useful_news:
        save_news_to_gist(useful_news)

    print(f"✅ News scan completed. Found: {len(useful_news)} useful news", flush=True)


threading.Thread(target=run_web_server, daemon=True).start()

print("📰 NEWS SCANNER BOT STARTED", flush=True)
send_telegram_msg("📰 تم تشغيل بوت الأخبار - Bot2/Bot3 مستمر + الاستثمار الأربعاء فقط")

while True:
    try:
        run_news_scanner()
        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("News main loop error:", e, flush=True)
        time.sleep(30)
