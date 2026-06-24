# =========================
# Confirmed Breakout Entry
# =========================


def analyze_confirmed_breakout(row):
    bars = row.get("bars")
    resistance = float(
        row.get("nearest_resistance", 0) or 0
    )
    instant_rvol = float(
        row.get("instant_rvol", 0) or 0
    )
    volume_acceleration = bool(
        row.get("volume_acceleration", False)
    )
    quality_bonus = float(
        row.get("quality_bonus", 0) or 0
    )

    if bars is None or len(bars) < 3:
        return None

    if resistance <= 0:
        return None

    last_3_closes = list(
        bars["close"].tail(3)
    )

    last_3_volumes = list(
        bars["volume"].tail(3)
    )

    close_1 = float(last_3_closes[0])
    close_2 = float(last_3_closes[1])
    close_3 = float(last_3_closes[2])

    volume_1 = float(last_3_volumes[0])
    volume_2 = float(last_3_volumes[1])
    volume_3 = float(last_3_volumes[2])

    closes_above_resistance = (
        close_1 > resistance
        and close_2 > resistance
        and close_3 > resistance
    )

    closes_hold_breakout = (
        close_3 >= close_1
    )

    strict_volume_confirmed = (
        instant_rvol >= 3
        and volume_acceleration
        and volume_3 >= volume_1 * 0.80
        and volume_3 >= volume_2 * 0.80
    )

    boosted_volume_confirmed = (
        instant_rvol >= 2.5
        and volume_acceleration
        and volume_3 >= volume_1 * 0.70
        and volume_3 >= volume_2 * 0.70
        and quality_bonus >= 15
    )

    volume_is_strong = (
        strict_volume_confirmed
        or boosted_volume_confirmed
    )

    if not closes_above_resistance:
        print(
            f"🧱 REJECT {row.get('symbol')} | "
            f"3 closes above resistance failed",
            flush=True,
        )
        return None

    if not closes_hold_breakout:
        print(
            f"🧱 REJECT {row.get('symbol')} | "
            f"breakout hold failed",
            flush=True,
        )
        return None

    if not volume_is_strong:
        print(
            f"🧱 REJECT {row.get('symbol')} | "
            f"volume confirmation failed | "
            f"RVOL={instant_rvol}",
            flush=True,
        )
        return None

    entry_price = close_3
    stop_loss = resistance * 0.99

    return {
        "ready_to_alert": True,
        "grade": "CONFIRMED_BREAKOUT",
        "reason": "Confirmed breakout with 3 closes holding above resistance",
        "price": entry_price,
        "stop_loss": stop_loss,
        "confirmed_resistance": resistance,
        "confirmation_close_1": close_1,
        "confirmation_close_2": close_2,
        "confirmation_close_3": close_3,
        "confirmed_quality_bonus": quality_bonus,
    }
