import os
import time
import requests

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

float_cache = {}
news_cache = {}

FLOAT_CACHE_TTL = 60 * 60 * 24
NEWS_CACHE_TTL = 60 * 30

def get_float_data(symbol):
    if not FINNHUB_API_KEY:
        return None

    cached = float_cache.get(symbol)

    if cached:
        ts, data = cached

        if time.time() - ts < FLOAT_CACHE_TTL:
            return data

    try:
        response = requests.get(
            "https://finnhub.io/api/v1/stock/profile2",
            params={
                "symbol": symbol,
                "token": FINNHUB_API_KEY,
            },
            timeout=10,
        )

        if response.status_code != 200:
            return None

        data = response.json()

        float_shares = (
            data.get("shareOutstanding")
            or 0
        )

        float_cache[symbol] = (
            time.time(),
            float_shares,
        )

        return float_shares

    except Exception:
        return None

  def get_news_data(symbol):
    if not FINNHUB_API_KEY:
        return {}

    cached = news_cache.get(symbol)

    if cached:
        ts, data = cached

        if time.time() - ts < NEWS_CACHE_TTL:
            return data

    try:
        response = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": "2025-01-01",
                "to": "2030-01-01",
                "token": FINNHUB_API_KEY,
            },
            timeout=10,
        )

        if response.status_code != 200:
            return {}

        items = response.json()

        if not items:
            return {}

        latest = items[0]

        data = {
            "news_headline": latest.get(
                "headline"
            ),
            "news_source": latest.get(
                "source"
            ),
        }

        news_cache[symbol] = (
            time.time(),
            data,
        )

        return data

    except Exception:
        return {}

def enrich_with_finnhub_data(entry_data):
    symbol = str(
        entry_data.get("symbol", "")
    ).upper()

    float_shares = get_float_data(symbol)

    if float_shares:
        entry_data["float_shares"] = (
            float_shares
        )

    news_data = get_news_data(symbol)

    if news_data:
        entry_data.update(news_data)

    return entry_data

