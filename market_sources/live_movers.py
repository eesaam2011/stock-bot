from market_sources.bars_data import (
    get_5m_bars,
    get_snapshots,
)


def get_live_movers(symbols):
    bars_by_symbol = get_5m_bars(symbols)
    snapshots_by_symbol = get_snapshots(symbols)

    movers = []

    for symbol, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue

        if len(bars) < 20:
            continue

        snapshot = snapshots_by_symbol.get(symbol)

        try:
            first_close = float(bars["close"].iloc[0])
            last_close = float(bars["close"].iloc[-1])
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

        gain_pct = (
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

        movers.append({
            "symbol": symbol,
            "price": current_price,
            "gain_pct": gain_pct,
            "rvol": float(rvol),
            "volume_acceleration": volume_acceleration,
            "source": "live_movers",
        })

    movers = sorted(
        movers,
        key=lambda row: (
            row["gain_pct"],
            row["rvol"],
        ),
        reverse=True,
    )

    return movers
  
