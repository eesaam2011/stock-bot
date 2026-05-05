import requests
import pandas as pd

YAHOO_COUNT = 200

PRICE_MIN = 0.5
PRICE_MAX = 25
MIN_VOLUME = 300000

BLACKLIST = [
    "JPM","BAC","WFC","C","GS","MS","AXP","USB","TFC",
    "MET","PRU","ALL","AIG","CB",
    "DKNG","PENN","WYNN","LVS",
    "BUD","TAP","STZ","DEO",
    "PM","MO",
    "CGC","TLRY","ACB",
    "NCLH","CCL","RCL"
]

SCREENS = [
    "most_actives",
    "day_gainers",
    "small_cap_gainers",
    "high_beta_stocks",
    "growth_technology_stocks",
    "most_shorted_stocks",
    "undervalued_growth_stocks",
    "aggressive_small_caps"
]


def is_clean_symbol(symbol):
    if not isinstance(symbol, str):
        return False
    if "." in symbol or "^" in symbol or "-" in symbol or "/" in symbol:
        return False
    if len(symbol) > 5:
        return False
    if not symbol.isalpha():
        return False
    return True


def fetch_master_list():
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    headers = {"User-Agent": "Mozilla/5.0"}

    candidates = {}

    for scr in SCREENS:
        try:
            res = requests.get(
                url,
                params={"scrIds": scr, "count": YAHOO_COUNT},
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
                    and is_clean_symbol(symbol)
                    and symbol not in BLACKLIST
                    and price is not None
                    and PRICE_MIN <= float(price) <= PRICE_MAX
                    and float(volume) >= MIN_VOLUME
                ):
                    score = abs(float(change_pct)) + (float(volume) / 1_000_000)

                    old = candidates.get(symbol)

                    if old is None or score > old["score"]:
                        candidates[symbol] = {
                            "symbol": symbol,
                            "price": float(price),
                            "volume": float(volume),
                            "change_pct": float(change_pct),
                            "score": score
                        }

        except Exception as e:
            print(f"Error {scr}: {e}")
            continue

    ranked = sorted(
        candidates.values(),
        key=lambda x: (abs(x["change_pct"]), x["volume"], x["score"]),
        reverse=True
    )

    symbols = [x["symbol"] for x in ranked]

    print(f"🔥 Total Clean Symbols: {len(symbols)}")

    return symbols


# =========================
# تجربة
# =========================
symbols = fetch_master_list()

print(symbols[:20])
