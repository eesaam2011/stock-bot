"""Pure original Market Radar consumer functions; no bot startup or I/O on import."""
import time,math
NEWS_CACHE_TTL = 60 * 60
NEWS_LOOKBACK_HOURS = 12
MAX_SHARED_NEWS_ARTICLES = 5
def safe_float(value, default=0.0):
    try:
        if value is None:
            return default

        value = float(value)

        if math.isnan(value) or math.isinf(value):
            return default

        return value

    except Exception:
        return default

def safe_int(value, default=0):
    try:
        if value is None:
            return default

        return int(float(value))

    except Exception:
        return default

def normalize_news_article(article):
    if not isinstance(article, dict):
        return None

    published_ts = safe_int(
        article.get("datetime"),
        0
    )

    if published_ts <= 0:
        return None

    article_analysis = article.get(
        "analysis",
        {}
    )

    if not isinstance(
        article_analysis,
        dict
    ):
        article_analysis = {}

    return {
        "id": article.get("id"),
        "datetime": published_ts,
        "headline": str(
            article.get("headline") or ""
        ).strip(),
        "summary": str(
            article.get("summary") or ""
        ).strip(),
        "source": str(
            article.get("source") or ""
        ).strip(),
        "url": str(
            article.get("url") or ""
        ).strip(),
        "category": str(
            article.get("category") or ""
        ).strip(),
        "related": str(
            article.get("related") or ""
        ).strip(),
        "analysis": {
            "score": safe_float(
                article_analysis.get(
                    "score"
                )
            ),
            "category": str(
                article_analysis.get(
                    "category"
                )
                or "neutral"
            ),
            "positive": bool(
                article_analysis.get(
                    "positive"
                )
            ),
            "minor_negative": bool(
                article_analysis.get(
                    "minor_negative"
                )
            ),
            "serious_negative": bool(
                article_analysis.get(
                    "serious_negative"
                )
            ),
            "major_catalyst": bool(
                article_analysis.get(
                    "major_catalyst"
                )
            )
        }
    }

def filter_recent_news_articles(articles):
    if not isinstance(articles, list):
        return []

    now_ts = time.time()

    cutoff_ts = (
        now_ts
        - (NEWS_LOOKBACK_HOURS * 60 * 60)
    )

    normalized_articles = []

    seen_articles = set()

    for article in articles:
        normalized = normalize_news_article(
            article
        )

        if not normalized:
            continue

        published_ts = safe_float(
            normalized.get("datetime")
        )

        if published_ts < cutoff_ts:
            continue

        if published_ts > now_ts + 300:
            continue

        article_id = normalized.get("id")

        duplicate_key = (
            str(article_id)
            if article_id is not None
            else (
                f"{normalized.get('datetime')}:"
                f"{normalized.get('headline')}"
            )
        )

        if duplicate_key in seen_articles:
            continue

        seen_articles.add(duplicate_key)

        normalized_articles.append(
            normalized
        )

    normalized_articles.sort(
        key=lambda item: safe_float(
            item.get("datetime")
        ),
        reverse=True
    )

    return normalized_articles[
        :MAX_SHARED_NEWS_ARTICLES
    ]

def normalize_shared_news_item(
    symbol,
    item
):
    if not isinstance(item, dict):
        return None

    symbol = str(
        symbol or item.get("symbol") or ""
    ).upper().strip()

    if not symbol:
        return None

    fetched_at = safe_float(
        item.get("fetched_at")
        or item.get("checked_at")
    )

    next_refresh_at = safe_float(
        item.get("next_refresh_at")
    )

    analysis = item.get(
        "analysis",
        {}
    )

    if not isinstance(
        analysis,
        dict
    ):
        analysis = {}

    articles = filter_recent_news_articles(
        item.get("articles", [])
    )

    return {
        "schema_version": safe_int(
            item.get("schema_version"),
            1
        ),
        "producer": str(
            item.get("producer") or ""
        ),
        "symbol": symbol,
        "fetched_at": fetched_at,
        "next_refresh_at": next_refresh_at,
        "lookback_hours": safe_int(
            item.get(
                "lookback_hours"
            ),
            NEWS_LOOKBACK_HOURS
        ),
        "status": str(
            item.get("status") or "ok"
        ),
        "analysis": {
            "positive": bool(
                analysis.get("positive")
            ),
            "minor_negative": bool(
                analysis.get(
                    "minor_negative"
                )
            ),
            "serious_negative": bool(
                analysis.get(
                    "serious_negative"
                )
            ),
            "major_catalyst": bool(
                analysis.get(
                    "major_catalyst"
                )
            ),
            "category": str(
                analysis.get("category")
                or "neutral"
            ),
            "bonus": safe_int(
                analysis.get("bonus")
            ),
            "score": safe_float(
                analysis.get("score")
            ),
            "news_id": str(
                analysis.get("news_id")
                or ""
            ),
            "headline": str(
                analysis.get("headline")
                or ""
            ),
            "datetime": safe_int(
                analysis.get("datetime")
            )
        },
        "articles": articles
    }

def get_symbol_news(symbol):
    """
    Read news analysis only from the central Redis cache produced by
    Elite Catalyst Radar.

    No Finnhub request is made here.
    No local news classification is performed here.
    """
    symbol = symbol.upper().strip()

    cached_item = get_cached_symbol_news(
        symbol
    )

    if not cached_item:
        return {
            "status": "shared_cache_missing",
            "positive": False,
            "minor_negative": False,
            "serious_negative": False,
            "major_catalyst": False,
            "bonus": 0,
            "headline": "",
            "category": "none",
            "score": 0,
            "cached": False,
            "producer": "elite_catalyst_radar"
        }

    analysis = cached_item.get(
        "analysis",
        {}
    )

    if not isinstance(
        analysis,
        dict
    ):
        analysis = {}

    fetched_at = safe_float(
        cached_item.get(
            "fetched_at"
        )
    )

    next_refresh_at = safe_float(
        cached_item.get(
            "next_refresh_at"
        )
    )

    now_ts = time.time()

    stale = False

    if fetched_at <= 0:
        stale = True

    elif (
        next_refresh_at > 0
        and now_ts > next_refresh_at
    ):
        stale = True

    elif (
        now_ts - fetched_at
        > NEWS_CACHE_TTL
    ):
        stale = True

    # A stale positive catalyst must not add score.
    # A serious negative is still preserved as a safety block while present.
    bonus = safe_int(
        analysis.get("bonus")
    )

    positive = bool(
        analysis.get("positive")
    )

    serious_negative = bool(
        analysis.get(
            "serious_negative"
        )
    )

    if stale and not serious_negative:
        bonus = 0
        positive = False

    headline = str(
        analysis.get("headline")
        or ""
    )

    if not headline:
        articles = cached_item.get(
            "articles",
            []
        )

        if articles:
            headline = str(
                articles[0].get(
                    "headline",
                    ""
                )
            )

    return {
        "status": (
            "shared_cache_stale"
            if stale
            else cached_item.get(
                "status",
                "ok"
            )
        ),
        "positive": positive,
        "minor_negative": bool(
            analysis.get(
                "minor_negative"
            )
        ),
        "serious_negative": (
            serious_negative
        ),
        "major_catalyst": bool(
            analysis.get(
                "major_catalyst"
            )
        ),
        "bonus": bonus,
        "headline": headline,
        "category": str(
            analysis.get("category")
            or "neutral"
        ),
        "score": safe_float(
            analysis.get("score")
        ),
        "cached": True,
        "stale": stale,
        "producer": str(
            cached_item.get("producer")
            or "elite_catalyst_radar"
        )
    }
