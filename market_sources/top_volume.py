from market_sources.bars_data import (
    get_5m_bars,
    get_daily_bars,
    get_snapshots,
)


def get_top_volume(symbols):
    bars_by_symbol = get_5m_bars(symbols)
    daily_by_symbol = get_daily_bars(symbols)
    snapshots_by_symbol = get_snapshots(symbols)

    volume_rows = []

    for symbol, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue

        daily_bars = daily_by_symbol.get(symbol)
        snapshot = snapshots_by_symbol.get(symbol)

        if daily_bars is None or daily_bars.empty:
            continue

        try:
            daily_bar = daily_bars.iloc[-1]

            day_volume = float(daily_bar["volume"])
            last_close = float(bars["close"].iloc[-1])
            day_high = float(bars["high"].max())

            vwap = float(
                (bars["close"] * bars["volume"]).sum()
                / bars["volume"].sum()
            )
        except Exception:
            continue

        current_price = last_close

        try:
            if snapshot and snapshot.latest_trade:
                current_price = float(snapshot.latest_trade.price)
        except Exception:
            current_price = last_close

        if current_price <= 0 or day_volume <= 0 or day_high <= 0 or vwap <= 0:
            continue

        dollar_volume = day_volume * current_price

        near_high = current_price >= day_high * 0.97
        above_vwap = current_price > vwap

        volume_rows.append({
            "symbol": symbol,
            "price": current_price,
            "day_volume": day_volume,
            "dollar_volume": dollar_volume,
            "day_high": day_high,
            "vwap": vwap,
            "near_high": near_high,
            "above_vwap": above_vwap,
            "source": "top_volume",
        })

    volume_rows = sorted(
        volume_rows,
        key=lambda row: row["day_volume"],
        reverse=True,
    )

    return volume_rows 
