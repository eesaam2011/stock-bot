from market_sources.bars_data import (
    get_5m_bars,
    get_daily_bars,
    get_snapshots,
)


def get_live_movers(symbols):
    bars_by_symbol = get_5m_bars(symbols)
    daily_by_symbol = get_daily_bars(symbols)
    snapshots_by_symbol = get_snapshots(symbols)

    movers = []

    for symbol, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue

        if len(bars) < 20:
            continue

        daily_bars = daily_by_symbol.get(symbol)
        snapshot = snapshots_by_symbol.get(symbol)

        if daily_bars is None or daily_bars.empty:
            continue

        try:
            daily_bar = daily_bars.iloc[-1]

            day_volume = float(daily_bar["volume"])
            first_close = float(bars["close"].iloc[0])
            last_close = float(bars["close"].iloc[-1])
            avg_volume = float(bars["volume"].mean())
            day_high = float(bars["high"].max())

            vwap = float(
                (bars["close"] * bars["volume"]).sum()
                / bars["volume"].sum()
            )
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

        if current_price <= 0 or day_volume <= 0 or day_high <= 0 or vwap <= 0:
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

        dollar_volume = day_volume * current_price

        near_high = current_price >= day_high * 0.97
        above_vwap = current_price > vwap

        movers.append({
            "symbol": symbol,
            "price": current_price,
            "day_volume": day_volume,
            "dollar_volume": dollar_volume,
            "gain_pct": gain_pct,
            "rvol": float(rvol),
            "volume_acceleration": volume_acceleration,
            "day_high": day_high,
            "vwap": vwap,
            "near_high": near_high,
            "above_vwap": above_vwap,
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
