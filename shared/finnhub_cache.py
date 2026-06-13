import os
import time
from datetime import datetime, timedelta, timezone

import requests

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

FLOAT_CACHE_FILE = "float_cache.json"

float_cache = {}
news_cache = {}

float_cache_loaded = False

FLOAT_CACHE_TTL = 60 * 60 * 24
NEWS_CACHE_TTL = 60 * 60
NEWS_LOOKBACK_HOURS = 12


def load_float_cache_from_gist():
    global float_cache
    global float_cache_loaded

    if float_cache_loaded:
        return

    if not GIST_ID:
        print(
            "Float cache skipped: missing GIST_ID",
            flush=True,
        )
        float_cache_loaded = True
        return

    url = f"https://api.github.com/gists/{GIST_ID}"

    headers = {
        "Accept": "application/vnd.github.v3+json"
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=10,
        )

        if response.status_code != 200:
            print(
                f"Float cache gist error: {response.status_code}",
                flush=True,
            )
            float_cache_loaded = True
            return

        files = response.json().get("files", {})

        if FLOAT_CACHE_FILE not in files:
            print(
                f"Float cache file not found in Gist: {FLOAT_CACHE_FILE}",
                flush=True,
            )
            float_cache_loaded = True
            return

        content = files[FLOAT_CACHE_FILE].get(
            "content",
            "{}",
        )

        data = response.json()

        import json

        raw_cache = json.loads(content)

        cleaned_cache = {}

        for symbol, value in raw_cache.items():
            symbol = str(symbol).upper().strip()

            if isinstance(value, dict):
                real_float = value.get("float")
            else:
                real_float = value

            if real_float:
                cleaned_cache[symbol] = real_float

        float_cache = cleaned_cache
        float_cache_loaded = True

        print(
            f"🧬 Float cache loaded from Gist | Total={len(float_cache)}",
            flush=True,
        )

    except Exception as error:
        print(
            f"Float cache gist load error: {error}",
            flush=True,
        )
        float_cache_loaded = True


def get_float_data(symbol):
    symbol = str(symbol or "").upper().strip()

    if not symbol:
        return None

    load_float_cache_from_gist()

    return float_cache.get(symbol)


def detect_news_sentiment(headline):
    headline_lower = str(
        headline or ""
    ).lower()

    bullish_words = [
        "fda",
        "approval",
        "approved",
        "contract",
        "award",
        "partnership",
        "agreement",
        "positive",
        "phase",
        "trial",
        "results",
        "patent",
        "acquisition",
        "merger",
    ]

    bearish_words = [
        "offering",
        "public offering",
        "registered direct",
        "dilution",
        "bankruptcy",
        "delisting",
        "investigation",
        "lawsuit",
        "halt",
    ]

    if any(
        word in headline_lower
        for word in bearish_words
    ):
        return "bearish"

    if any(
        word in headline_lower
        for word in bullish_words
    ):
        return "bullish"

    return "neutral"


def format_news_age(news_timestamp):
    try:
        news_dt = datetime.fromtimestamp(
            int(news_timestamp),
            tz=timezone.utc,
        )

        age_hours = (
            datetime.now(timezone.utc) - news_dt
        ).total_seconds() / 3600

        if age_hours < 1:
            return "أقل من ساعة"

        return f"{age_hours:.1f} ساعة"

    except Exception:
        return "غير متوفر"


def get_news_data(symbol):
    if not FINNHUB_API_KEY:
        return {}

    cached = news_cache.get(symbol)

    if cached:
        ts, data = cached

        if time.time() - ts < NEWS_CACHE_TTL:
            return data

    try:
        today = datetime.now(timezone.utc).date()
        from_date = today - timedelta(days=2)

        response = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": from_date.isoformat(),
                "to": today.isoformat(),
                "token": FINNHUB_API_KEY,
            },
            timeout=10,
        )

        if response.status_code != 200:
            return {}

        items = response.json() or []

        if not items:
            news_cache[symbol] = (
                time.time(),
                {},
            )
            return {}

        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=NEWS_LOOKBACK_HOURS
        )

        recent_items = []

        for item in items:
            news_time = item.get("datetime")

            if not news_time:
                continue

            try:
                news_dt = datetime.fromtimestamp(
                    int(news_time),
                    tz=timezone.utc,
                )
            except Exception:
                continue

            if news_dt >= cutoff:
                recent_items.append(item)

        if not recent_items:
            news_cache[symbol] = (
                time.time(),
                {},
            )
            return {}

        latest = sorted(
            recent_items,
            key=lambda item: item.get("datetime", 0),
            reverse=True,
        )[0]

        headline = latest.get(
            "headline",
            "",
        )

        data = {
            "news_headline": headline,
            "news_source": latest.get("source"),
            "news_url": latest.get("url"),
            "news_age": format_news_age(
                latest.get("datetime")
            ),
            "news_sentiment": detect_news_sentiment(
                headline
            ),
        }

        news_cache[symbol] = (
            time.time(),
            data,
        )

        return data

    except Exception as error:
        print(
            f"Finnhub news error {symbol}: {error}",
            flush=True,
        )
        return {}


def enrich_with_finnhub_data(entry_data):
    symbol = str(
        entry_data.get("symbol", "")
    ).upper().strip()

    if not symbol:
        return entry_data

    float_shares = get_float_data(
        symbol
    )

    if float_shares:
        entry_data["float_shares"] = (
            float_shares
        )

    news_data = get_news_data(
        symbol
    )

    if news_data:
        entry_data.update(
            news_data
        )

    return entry_data
