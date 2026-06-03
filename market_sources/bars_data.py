from market_sources.alpaca_client import api
from datetime import datetime, timedelta
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

    try:
        bars = api.get_bars(
            symbols,
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
        return {}

    if bars is None or bars.empty:
        return {}

    bars_by_symbol = {}

    for symbol in symbols:
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


