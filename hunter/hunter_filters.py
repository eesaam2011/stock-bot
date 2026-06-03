from shared.config import (
    PRICE_MIN,
    PRICE_MAX,
    MIN_DAY_VOLUME,
    MIN_DOLLAR_VOLUME,
)

from shared.blacklist import (
    SYMBOL_BLACKLIST,
    BAD_KEYWORDS,
)
def is_clean_symbol(symbol):
    if not symbol:
        return False

    symbol = str(symbol).upper().strip()

    if not symbol.isalpha():
        return False

    if len(symbol) > 5:
        return False

    if symbol.endswith(("W", "U", "R")):
        return False

    return True
    def has_bad_keywords(text):
    if not text:
        return False

    text = str(text).lower()

    return any(
        keyword in text
        for keyword in BAD_KEYWORDS
    )

def passes_layer0(
    symbol,
    price,
    day_volume,
    dollar_volume,
    tradable=True,
    name="",
    sector="",
    industry="",
):
    if not tradable:
        return False

    if not is_clean_symbol(symbol):
        return False

    symbol = str(symbol).upper().strip()

    if symbol in SYMBOL_BLACKLIST:
        return False

    if price is None or price < PRICE_MIN or price > PRICE_MAX:
        return False

    if day_volume is None or day_volume < MIN_DAY_VOLUME:
        return False

    if dollar_volume is None or dollar_volume < MIN_DOLLAR_VOLUME:
        return False

    combined_text = " ".join([
        str(name),
        str(sector),
        str(industry),
    ])

    if has_bad_keywords(combined_text):
        return False

    return True
# =========================================
# Candidate Score
# =========================================

def calculate_candidate_score(
    rvol=0,
    premarket_volume=0,
    dollar_volume=0,
    gap_pct=0,
    volume_acceleration=False,
    near_high=False,
    above_vwap=False,
    clean_price_range=False,
):
    score = 0

    if rvol >= 5:
        score += 25
    elif rvol >= 3:
        score += 18
    elif rvol >= 2:
        score += 12

    if premarket_volume >= 1_000_000:
        score += 20
    elif premarket_volume >= 500_000:
        score += 14
    elif premarket_volume >= 200_000:
        score += 8

    if dollar_volume >= 5_000_000:
        score += 15
    elif dollar_volume >= 2_000_000:
        score += 10
    elif dollar_volume >= 500_000:
        score += 5

    if gap_pct >= 10:
        score += 10
    elif gap_pct >= 5:
        score += 7
    elif gap_pct >= 2:
        score += 4

    if volume_acceleration:
        score += 10

    if near_high:
        score += 10

    if above_vwap:
        score += 5

    if clean_price_range:
        score += 5

    return min(score, 100)

