# =========================
# Direct Entry Data
# =========================

from market_sources.bars_data import (
    get_1m_bars,
    get_daily_bars,
)


def calculate_vwap(bars):
    total_volume = bars["volume"].sum()

    if total_volume <= 0:
        return 0

    return float(
        (bars["close"] * bars["volume"]).sum()
        / total_volume
    )


def calculate_ema(bars, period):
    return float(
        bars["close"]
        .ewm(span=period, adjust=False)
        .mean()
        .iloc[-1]
    )


def calculate_rsi(bars, period=14):
    if len(bars) < period + 1:
        return 0

    delta = bars["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]

    if avg_loss <= 0:
        return 100

    rs = avg_gain / avg_loss

    return float(100 - (100 / (1 + rs)))


def calculate_move_pct(bars, minutes):
    if len(bars) <= minutes:
        return 0

    current_price = float(bars["close"].iloc[-1])
    old_price = float(bars["close"].iloc[-minutes])

    if old_price <= 0:
        return 0

    return float(
        ((current_price - old_price) / old_price) * 100
    )


def calculate_instant_rvol(bars):
    if len(bars) < 20:
        return 0

    recent_volume = bars["volume"].tail(3).mean()
    average_volume = bars["volume"].mean()

    if average_volume <= 0:
        return 0

    return float(recent_volume / average_volume)


def calculate_volume_acceleration(bars):
    if len(bars) < 12:
        return False

    recent_volume = bars["volume"].tail(3).mean()

    previous_volume = (
        bars["volume"]
        .tail(12)
        .head(9)
        .mean()
    )

    if previous_volume <= 0:
        return False

    return bool(recent_volume > previous_volume)


def calculate_close_position(bars):
    last = bars.iloc[-1]

    high = float(last["high"])
    low = float(last["low"])
    close = float(last["close"])

    candle_range = high - low

    if candle_range <= 0:
        return 0

    return float((close - low) / candle_range)


def calculate_upper_wick_pct(bars):
    last = bars.iloc[-1]

    high = float(last["high"])
    low = float(last["low"])
    open_price = float(last["open"])
    close = float(last["close"])

    candle_top = max(open_price, close)
    candle_range = high - low

    if candle_range <= 0:
        return 0

    return float((high - candle_top) / candle_range)


def calculate_distribution_score(bars):
    if len(bars) < 10:
        return 0

    recent_bars = bars.tail(10)
    average_volume = recent_bars["volume"].mean()
    score = 0

    for _, candle in recent_bars.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        open_price = float(candle["open"])
        close = float(candle["close"])
        volume = float(candle["volume"])

        candle_range = high - low

        if candle_range <= 0:
            continue

        close_position = (close - low) / candle_range
        upper_wick = (high - max(open_price, close)) / candle_range

        if close < open_price and volume > average_volume:
            score += 6

        if upper_wick > 0.45:
            score += 5

        if close_position < 0.45:
            score += 4

    return float(score)


def calculate_repeated_rejection(bars):
    if len(bars) < 10:
        return False

    recent_bars = bars.tail(10)
    recent_high = float(recent_bars["high"].max())

    rejection_count = 0

    for _, candle in recent_bars.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])

        candle_range = high - low

        if candle_range <= 0:
            continue

        close_position = (close - low) / candle_range

        if high >= recent_high * 0.995 and close_position < 0.55:
            rejection_count += 1

    return bool(rejection_count >= 2)


def calculate_real_breakout(bars):
    if len(bars) < 30:
        return False

    current_price = float(bars["close"].iloc[-1])
    previous_high = float(bars["high"].iloc[-30:-1].max())
    instant_rvol = calculate_instant_rvol(bars)
    close_position = calculate_close_position(bars)

    return bool(
        current_price > previous_high
        and instant_rvol >= 2.5
        and close_position >= 0.65
    )


def calculate_overextended(bars):
    move_3m = calculate_move_pct(bars, 3)
    move_5m = calculate_move_pct(bars, 5)
    rsi = calculate_rsi(bars)

    return bool(
        move_3m > 2.2
        or move_5m > 3.5
        or rsi > 82
    )


def calculate_resistance_levels(bars):
    if len(bars) < 60:
        return {
            "resistance_30m": 0,
            "resistance_60m": 0,
            "nearest_resistance": 0,
            "resistance_distance_pct": 0,
        }

    current_price = float(bars["close"].iloc[-1])

    resistance_30m = float(bars["high"].tail(30).max())
    resistance_60m = float(bars["high"].tail(60).max())

    resistances = [
        resistance_30m,
        resistance_60m,
    ]

    valid_resistances = [
        level for level in resistances
        if level >= current_price
    ]

    if valid_resistances:
        nearest_resistance = min(valid_resistances)
    else:
        nearest_resistance = max(resistances)

    if current_price <= 0:
        resistance_distance_pct = 0
    else:
        resistance_distance_pct = float(
            ((nearest_resistance - current_price) / current_price) * 100
        )

    return {
        "resistance_30m": resistance_30m,
        "resistance_60m": resistance_60m,
        "nearest_resistance": nearest_resistance,
        "resistance_distance_pct": resistance_distance_pct,
    }


def get_day_volume(symbol):
    daily_dict = get_daily_bars(
        symbols=[symbol],
        limit=1,
    )

    daily_bars = daily_dict.get(symbol)

    if daily_bars is None or daily_bars.empty:
        return 0

    return float(daily_bars["volume"].iloc[-1])


def build_entry_data(symbol):
    bars_dict = get_1m_bars(
        symbols=[symbol],
        limit=120,
        days=1,
    )

    bars = bars_dict.get(symbol)

    if bars is None or bars.empty:
        return None

    if len(bars) < 60:
        return None

    price = float(bars["close"].iloc[-1])
    recent_volume = float(bars["volume"].sum())
    day_volume = get_day_volume(symbol)

    if day_volume <= 0:
        day_volume = recent_volume

    dollar_volume = float(day_volume * price)

    vwap = calculate_vwap(bars)
    ema9 = calculate_ema(bars, 9)
    ema20 = calculate_ema(bars, 20)
    rsi = calculate_rsi(bars)

    move_3m = calculate_move_pct(bars, 3)
    move_5m = calculate_move_pct(bars, 5)

    instant_rvol = calculate_instant_rvol(bars)
    volume_acceleration = calculate_volume_acceleration(bars)

    close_position = calculate_close_position(bars)
    upper_wick_pct = calculate_upper_wick_pct(bars)

    day_high = float(bars["high"].max())
    near_high = bool(price >= day_high * 0.97)

    distribution_score = calculate_distribution_score(bars)
    repeated_rejection = calculate_repeated_rejection(bars)
    real_breakout = calculate_real_breakout(bars)
    overextended = calculate_overextended(bars)

    resistance_data = calculate_resistance_levels(bars)

    return {
        "symbol": symbol,
        "price": price,
        "recent_volume": recent_volume,
        "day_volume": day_volume,
        "dollar_volume": dollar_volume,
        "vwap": vwap,
        "ema9": ema9,
        "ema20": ema20,
        "rsi": rsi,
        "move_3m": move_3m,
        "move_5m": move_5m,
        "instant_rvol": instant_rvol,
        "volume_acceleration": volume_acceleration,
        "close_position": close_position,
        "upper_wick_pct": upper_wick_pct,
        "day_high": day_high,
        "near_high": near_high,
        "distribution_score": distribution_score,
        "repeated_rejection": repeated_rejection,
        "real_breakout": real_breakout,
        "overextended": overextended,
        **resistance_data,
    }
  
