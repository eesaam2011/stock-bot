import os
import time
import json
import html
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import pytz

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

saudi_tz = pytz.timezone("Asia/Riyadh")

PRICE_MIN = 0.5
PRICE_MAX = 25
YAHOO_COUNT = 200
NEWS_SYMBOL_LIMIT = 250
SCAN_INTERVAL = 900  # 15 دقيقة
NEWS_FILE = "news_signals.json"

sent_news_alerts = {}


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


def is_trading_time():
    now = datetime.now(saudi_tz)
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()  # 0=Monday, 6=Sunday

    if weekday > 4:
        return False

    if hour > 10 or (hour == 10 and minute >= 30):
        return True

    if hour < 3:
        return True

    return False


def get_base_candidates():
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    headers = {"User-Agent": "Mozilla/5.0"}

    black_list = [
        "JPM", "BAC", "WFC", "C", "GS", "MS", "AXP", "USB", "TFC",
        "MET", "PRU", "ALL", "AIG", "CB",
        "DKNG", "PENN", "WYNN", "LVS",
        "BUD", "TAP", "STZ", "DEO",
        "PM", "MO",
        "CGC", "TLRY", "ACB",
        "NCLH", "CCL", "RCL"
    ]

    candidates = {}

    for scr_id in [
        "most_actives",
        "day_gainers",
        "small_cap_gainers",
        "undervalued_growth_stocks",
        "aggressive_small_caps",
        "most_shorted_stocks",
        "high_beta_stocks",
        "growth_technology_stocks"
    ]:
        try:
            res = requests.get(
                url,
                params={"scrIds": scr_id, "count": YAHOO_COUNT},
                headers=headers,
                timeout=10
            ).json()

            data = res.get("finance", {}).get("result")
            if not data:
                continue

            quotes = data[0].get("quotes", [])

            for q in quotes:
                symbol = q.get("symbol")
                price = q.get("regularMarketPrice")
                volume = q.get("regularMarketVolume", 0) or 0
                change_pct = q.get("regularMarketChangePercent", 0) or 0

                if (
                    symbol
                    and isinstance(symbol, str)
                    and "." not in symbol
                    and "^" not in symbol
                    and "-" not in symbol
                    and symbol not in black_list
                    and price is not None
                    and PRICE_MIN <= float(price) <= PRICE_MAX
                ):
                    score = abs(float(change_pct)) + (float(volume) / 1_000_000)

                    old = candidates.get(symbol)
                    if old is None or score > old["raw_score"]:
                        candidates[symbol] = {
                            "symbol": symbol,
                            "price": float(price),
                            "volume": float(volume),
                            "change_pct": float(change_pct),
                            "raw_score": score,
                            "source_list": scr_id
                        }

        except Exception as e:
            print(f"Yahoo list error {scr_id}: {e}", flush=True)
            continue

    ranked = sorted(
        candidates.values(),
        key=lambda x: (abs(x["change_pct"]), x["volume"], x["raw_score"]),
        reverse=True
    )

    return ranked[:NEWS_SYMBOL_LIMIT]


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

        for item in root.findall(".//item")[:5]:
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
        "fda approval": 4,
        "approval": 3,
        "contract": 3,
        "partnership": 3,
        "collaboration": 2,
        "acquisition": 3,
        "merger": 3,
        "earnings beat": 4,
        "beats estimates": 4,
        "raises guidance": 4,
        "guidance raised": 4,
        "patent": 2,
        "launch": 2,
        "breakthrough": 3,
        "positive data": 4,
        "phase 3": 3,
        "phase 2": 2,
        "buyout": 4,
        "upgrade": 2,
        "price target raised": 2
    }

    negative_keywords = {
        "offering": 4,
        "public offering": 5,
        "direct offering": 5,
        "dilution": 5,
        "bankruptcy": 5,
        "delisting": 5,
        "lawsuit": 3,
        "investigation": 3,
        "downgrade": 3,
        "misses estimates": 4,
        "cuts guidance": 4,
        "reverse split": 5,
        "sec investigation": 5,
        "fraud": 5
    }

    total_score = 0
    best_title = ""
    strongest_negative = False

    for item in items:
        title = item["title"]
        text = title.lower()

        item_score = 0

        for kw, weight in positive_keywords.items():
            if kw in text:
                item_score += weight

        for kw, weight in negative_keywords.items():
            if kw in text:
                item_score -= weight
                if weight >= 5:
                    strongest_negative = True

        age_hours = item.get("age_hours")

        if age_hours is not None:
            if age_hours <= 6:
                item_score += 2
            elif age_hours <= 24:
                item_score += 1

        if item_score > total_score or not best_title:
            best_title = title

        total_score += item_score

    if strongest_negative:
        return {
            "grade": "NEGATIVE",
            "label": "🔴 خبر سلبي / خطر",
            "score": total_score,
            "headline": best_title
        }

    if total_score >= 7:
        grade = "STRONG"
        label = "🔥 خبر إيجابي قوي"
    elif total_score >= 4:
        grade = "MEDIUM"
        label = "🟢 خبر إيجابي متوسط"
    elif total_score >= 1:
        grade = "WEAK"
        label = "⚪ خبر ضعيف / غير مؤثر"
    else:
        grade = "NONE"
        label = "⚪ لا يوجد خبر مؤثر"

    return {
        "grade": grade,
        "label": label,
        "score": total_score,
        "headline": best_title
    }


def read_news_gist():
    if not GIST_ID or not GITHUB_TOKEN:
        return []

    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()

        file_data = data.get("files", {}).get(NEWS_FILE)

        if not file_data:
            return []

        content = file_data.get("content", "[]")

        try:
            return json.loads(content)
        except Exception:
            return []

    except Exception as e:
        print("Read news gist error:", e, flush=True)
        return []


def save_news_to_gist(new_items):
    if not GIST_ID or not GITHUB_TOKEN:
        print("Gist keys missing", flush=True)
        return

    try:
        old_items = read_news_gist()
        now_ts = time.time()

        old_items = [
            x for x in old_items
            if now_ts - float(x.get("time", 0)) < 86400
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

        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    NEWS_FILE: {
                        "content": json.dumps(merged[-300:], ensure_ascii=False)
                    }
                }
            },
            timeout=10
        )

        print(f"News gist saved: {len(new_items)} new items", flush=True)

    except Exception as e:
        print("Save news gist error:", e, flush=True)


def should_alert(symbol):
    now = datetime.now(saudi_tz)

    if symbol not in sent_news_alerts:
        sent_news_alerts[symbol] = now
        return True

    diff = (now - sent_news_alerts[symbol]).total_seconds() / 60

    if diff >= 60:
        sent_news_alerts[symbol] = now
        return True

    return False


def run_news_scanner():
    print("📰 Fetching candidates for News Scanner...", flush=True)

    candidates = get_base_candidates()
    print(f"✅ News candidates loaded: {len(candidates)}", flush=True)

    strong_news = []

    for i, stock in enumerate(candidates, start=1):
        symbol = stock["symbol"]

        try:
            print(f"📰 {i}/{len(candidates)} checking news: {symbol}", flush=True)

            items = fetch_google_news(symbol)

            if not items:
                time.sleep(0.2)
                continue

            analysis = analyze_news_items(items)

            if analysis["grade"] in ["STRONG", "MEDIUM", "NEGATIVE"]:
                news_item = {
                    "symbol": symbol,
                    "price": stock["price"],
                    "change_pct": stock["change_pct"],
                    "volume": stock["volume"],
                    "source": "news_bot",
                    "news_grade": analysis["grade"],
                    "news_label": analysis["label"],
                    "news_score": analysis["score"],
                    "headline": analysis["headline"],
                    "source_list": stock.get("source_list"),
                    "time": time.time()
                }

                strong_news.append(news_item)

                if analysis["grade"] == "STRONG" and should_alert(symbol):
                    msg = (
                        f"📰🔥 *بوت الأخبار - خبر قوي*\n\n"
                        f"🎫 السهم: `{symbol}`\n"
                        f"💰 السعر: {stock['price']:.2f}\n"
                        f"📈 الحركة: {stock['change_pct']:.2f}%\n"
                        f"📊 الفوليوم: {stock['volume']:,.0f}\n\n"
                        f"🗞️ التصنيف: {analysis['label']}\n"
                        f"⭐ News Score: {analysis['score']}\n"
                        f"🧠 العنوان:\n{analysis['headline']}\n\n"
                        f"📌 ملاحظة: هذا ليس دخول مباشر، فقط سهم عليه خبر قوي.\n"
                        f"🔗 https://www.tradingview.com/chart/?symbol={symbol}"
                    )

                    send_telegram_msg(msg)

            time.sleep(0.2)

        except Exception as e:
            print(f"News scanner error {symbol}: {e}", flush=True)
            continue

    if strong_news:
        save_news_to_gist(strong_news)

    print(f"✅ News scan completed. Found: {len(strong_news)} useful news", flush=True)


print("📰 NEWS SCANNER BOT STARTED", flush=True)
send_telegram_msg("📰 تم تشغيل بوت الأخبار")

while True:
    try:
        if not is_trading_time():
            print("⏸️ خارج وقت التشغيل - بوت الأخبار ينتظر", flush=True)
            time.sleep(300)
            continue

        run_news_scanner()
        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print("News main loop error:", e, flush=True)
        time.sleep(30)
