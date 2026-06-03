from market_sources.alpaca_client import api

from hunter.hunter_filters import (
    is_clean_symbol,
    has_bad_keywords,
)

from shared.blacklist import SYMBOL_BLACKLIST


# =========================================
# Static Asset Loader
# =========================================

def get_static_clean_assets():
    assets = api.list_assets(status="active")

    clean_assets = []

    for asset in assets:
        symbol = str(asset.symbol).upper().strip()
        name = getattr(asset, "name", "")

        if not asset.tradable:
            continue

        if not is_clean_symbol(symbol):
            continue

        if symbol in SYMBOL_BLACKLIST:
            continue

        if has_bad_keywords(name):
            continue

        clean_assets.append({
            "symbol": symbol,
            "name": name,
            "tradable": asset.tradable,
            "source": "alpaca_assets",
        })

    return clean_assets
