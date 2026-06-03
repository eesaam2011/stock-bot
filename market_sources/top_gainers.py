from market_sources.bars_data import get_5m_bars


def get_top_gainers(symbols):
    bars_by_symbol = get_5m_bars(symbols)

    gainers = []

    for symbol, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue

        try:
            first_close = float(bars["close"].iloc[0])
            last_close = float(bars["close"].iloc[-1])
        except Exception:
            continue

        if first_close <= 0:
            continue

        gain_pct = (
            (last_close - first_close)
            / first_close
        ) * 100

        gainers.append({
            "symbol": symbol,
            "price": last_close,
            "gain_pct": gain_pct,
            "source": "top_gainers",
        })

    gainers = sorted(
        gainers,
        key=lambda row: row["gain_pct"],
        reverse=True,
    )

    return gainers
  
