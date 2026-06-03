from market_sources.bars_data import get_5m_bars


def get_top_volume(
    symbols,
    limit=50,
):
    bars_by_symbol = get_5m_bars(symbols)

    volume_rows = []

    for symbol, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue

        total_volume = float(bars["volume"].sum())
        last_close = float(bars["close"].iloc[-1])
        dollar_volume = total_volume * last_close

        volume_rows.append({
            "symbol": symbol,
            "price": last_close,
            "day_volume": total_volume,
            "dollar_volume": dollar_volume,
            "source": "top_volume",
        })

    volume_rows = sorted(
        volume_rows,
        key=lambda row: row["day_volume"],
        reverse=True,
    )

    return volume_rows[:limit]

