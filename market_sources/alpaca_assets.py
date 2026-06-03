import os
import alpaca_trade_api as tradeapi

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

api = tradeapi.REST(
    API_KEY,
    SECRET_KEY,
    BASE_URL,
)

def get_tradable_assets():
    assets = api.list_assets(status="active")

    symbols = []

    for asset in assets:
        if asset.tradable:
            symbols.append({
                "symbol": asset.symbol,
                "name": asset.name,
                "tradable": asset.tradable,
                "source": "alpaca_assets",
            })

    return symbols

