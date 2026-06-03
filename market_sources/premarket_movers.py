from market_sources.bars_data import (
    get_5m_bars,
    get_snapshots,
)


def get_premarket_movers(symbols):
    bars_by_symbol = get_5m_bars(symbols)
    snapshots_by_symbol = get_snapshots(symbols)

    premarket_rows = []

    for symbol, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue

        if len(bars) < 20:
            continue

        snapshot = snapshots_by_symbol.get(symbol)

        try:
            first_close = float(bars["close"].iloc[0])
            last_close = float(bars["close"].iloc[-1])
            total_volume = float(bars["volume"].sum())
            avg_volume = float(bars["volume"].mean())
        except Exception:
            continue

        if first_close <= 0 or avg_volume <= 0:
            continue

        current_price = last_close

        try:
            if snapshot and snapshot.latest_trade:
                current_price = float(snapshot.latest_trade.price)
        except Exception:
            current_price = last_close

        if current_price <= 0:
            continue

        gap_pct = (
            (current_price - first_close)
            / first_close
        ) * 100

        rvol = (
            bars["volume"].tail(12).mean()
            / avg_volume
        )

        recent_volume = bars["volume"].tail(3).mean()

        previous_volume = (
            bars["volume"]
            .tail(12)
            .head(9)
            .mean()
        )

        volume_acceleration = (
            recent_volume > previous_volume
        )

        dollar_volume = total_volume * current_price

        premarket_rows.append({
            "symbol": symbol,
            "price": current_price,
            "gap_pct": gap_pct,
            "premarket_volume": total_volume,
            "dollar_volume": dollar_volume,
            "rvol": float(rvol),
            "volume_acceleration": volume_acceleration,
            "source": "premarket_movers",
        })

    premarket_rows = sorted(
        premarket_rows,
        key=lambda row: (
            row["gap_pct"],
            row["rvol"],
            row["premarket_volume"],
        ),
        reverse=True,
    )

    return premarket_rows
  
