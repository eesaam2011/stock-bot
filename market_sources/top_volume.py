from market_sources.bars_data import (
    get_5m_bars,
    get_snapshots,
)


def get_top_volume(symbols):
    bars_by_symbol = get_5m_bars(symbols)
    snapshots_by_symbol = get_snapshots(symbols)

    volume_rows = []

    for symbol, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue

        snapshot = snapshots_by_symbol.get(symbol)

        try:
            total_volume = float(bars["volume"].sum())
            last_close = float(bars["close"].iloc[-1])
        except Exception:
            continue

        current_price = last_close

        try:
            if snapshot and snapshot.latest_trade:
                current_price = float(snapshot.latest_trade.price)
        except Exception:
            current_price = last_close

        if current_price <= 0:
            continue

        dollar_volume = total_volume * current_price

        volume_rows.append({
            "symbol": symbol,
            "price": current_price,
            "day_volume": total_volume,
            "dollar_volume": dollar_volume,
            "source": "top_volume",
        })

    volume_rows = sorted(
        volume_rows,
        key=lambda row: row["day_volume"],
        reverse=True,
    )

    return volume_rows
    
