from market_sources.bars_data import (
    get_5m_bars,
    get_snapshots,
)


def get_top_gainers(symbols):
    bars_by_symbol = get_5m_bars(symbols)
    snapshots_by_symbol = get_snapshots(symbols)

    gainers = []

    for symbol, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue

        snapshot = snapshots_by_symbol.get(symbol)

        try:
            first_close = float(bars["close"].iloc[0])
            last_close = float(bars["close"].iloc[-1])
        except Exception:
            continue

        current_price = last_close

        try:
            if snapshot and snapshot.latest_trade:
                current_price = float(snapshot.latest_trade.price)
        except Exception:
            current_price = last_close

        if first_close <= 0 or current_price <= 0:
            continue

        gain_pct = (
            (current_price - first_close)
            / first_close
        ) * 100

        gainers.append({
            "symbol": symbol,
            "price": current_price,
            "gain_pct": gain_pct,
            "source": "top_gainers",
        })

    gainers = sorted(
        gainers,
        key=lambda row: row["gain_pct"],
        reverse=True,
    )

    return gainers
    
