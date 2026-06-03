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
    
