import os
import alpaca_trade_api as tradeapi
from hunter.hunter_filters import (
    is_clean_symbol,
    has_bad_keywords,
)

from shared.blacklist import SYMBOL_BLACKLIST

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv(
    "APCA_API_BASE_URL",
    "https://paper-api.alpaca.markets",
)

api = tradeapi.REST(
    API_KEY,
    SECRET_KEY,
    BASE_URL,
)
# =========================================
# Layer 0 Asset Loader
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

