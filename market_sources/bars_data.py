from datetime import datetime, timedelta

import alpaca_trade_api as tradeapi

from market_sources.alpaca_client import api
from shared.config import BARS_BATCH_SIZE


# =========================================
# Alpaca Bars Loader
# =========================================

def get_5m_bars(
    symbols,
    limit=120,
    days=2,
):
    if not symbols:
        return {}

    symbols = [
        str(symbol).upper().strip()
        for symbol in symbols
        if symbol
    ]

    if not symbols:
        return {}

    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=days)

    bars_by_symbol = {}

    for i in range(0, len(symbols), BARS_BATCH_SIZE):
        batch_symbols = symbols[i:i + BARS_BATCH_SIZE]

        try:
            bars = api.get_bars(
                batch_symbols,
                tradeapi.TimeFrame(
                    5,
                    tradeapi.TimeFrameUnit.Minute,
                ),
                start=start_time.isoformat() + "Z",
                end=end_time.isoformat() + "Z",
                limit=limit,
                adjustment="raw",
            ).df
        except Exception:
            continue

        if bars is None or bars.empty:
            continue

        for symbol in batch_symbols:
            try:
                symbol_bars = bars.xs(
                    symbol,
                    level="symbol",
                )
            except Exception:
                continue

            if symbol_bars is None or symbol_bars.empty:
                continue

            bars_by_symbol[symbol] = symbol_bars

    return bars_by_symbol
    
