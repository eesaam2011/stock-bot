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

        if "symbol" not in bars.columns:
            continue

        for symbol in batch_symbols:
            symbol_bars = bars[
                bars["symbol"] == symbol
            ]

            if symbol_bars.empty:
                continue

            bars_by_symbol[symbol] = symbol_bars

    return bars_by_symbol
    
# =========================================
# Alpaca Snapshots Loader
# =========================================

def get_snapshots(symbols):
    if not symbols:
        return {}

    symbols = [
        str(symbol).upper().strip()
        for symbol in symbols
        if symbol
    ]

    if not symbols:
        return {}

    snapshots_by_symbol = {}

    for i in range(0, len(symbols), BARS_BATCH_SIZE):
        batch_symbols = symbols[i:i + BARS_BATCH_SIZE]

        try:
            snapshots = api.get_snapshots(batch_symbols)
        except Exception:
            continue

        if not snapshots:
            continue

        for symbol in batch_symbols:
            snapshot = snapshots.get(symbol)

            if not snapshot:
                continue

            snapshots_by_symbol[symbol] = snapshot

    return snapshots_by_symbol 

def get_daily_bars(
    symbols,
    limit=1,
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

    daily_by_symbol = {}

    for i in range(0, len(symbols), BARS_BATCH_SIZE):
        batch_symbols = symbols[i:i + BARS_BATCH_SIZE]

        try:
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=30)

            bars = api.get_bars(
                batch_symbols,
                tradeapi.TimeFrame.Day,
                start=start_time.isoformat() + "Z",
                end=end_time.isoformat() + "Z",
                limit=limit,
                adjustment="raw",
            ).df
        except Exception as error:
            print(
                f"❌ get_daily_bars error: {error}",
                flush=True,
            )
            continue

        if bars is None or bars.empty:
            print(
                f"⚠️ Empty daily bars batch: {batch_symbols[:5]}",
                flush=True,
            )
            continue

        print(
            f"✅ Daily bars batch received: {len(batch_symbols)} symbols",
            flush=True,
        )

        print(
            f"DAILY INDEX TYPE: {type(bars.index)}",
            flush=True,
        )

        print(
            f"DAILY COLUMNS: {list(bars.columns)}",
            flush=True,
        )

        if "symbol" not in bars.columns:
            continue

        for symbol in batch_symbols:
            symbol_bars = bars[
                bars["symbol"] == symbol
            ]

            if symbol_bars.empty:
                continue

            daily_by_symbol[symbol] = symbol_bars

    return daily_by_symbol
