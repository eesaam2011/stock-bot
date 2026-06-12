# =========================
# Direct Entry Filters
# =========================

from shared.config import (
    PRICE_MIN,
    PRICE_MAX,
    MIN_DAY_VOLUME,
    MIN_DOLLAR_VOLUME,
)
from direct_entry.confirmed_breakout import (
    analyze_confirmed_breakout,
)

def passes_layer0(row):
    price = float(row.get("price", 0) or 0)
    day_volume = float(row.get("day_volume", 0) or 0)
    dollar_volume = float(row.get("dollar_volume", 0) or 0)

    if price < PRICE_MIN or price > PRICE_MAX:
        return False, "Price out of range"

    if day_volume < MIN_DAY_VOLUME:
        return False, "Day volume too low"

    if dollar_volume < MIN_DOLLAR_VOLUME:
        return False, "Dollar volume too low"

    return True, "Layer 0 passed"


def passes_liquidity_layer(row):
    instant_rvol = float(row.get("instant_rvol", 0) or 0)
    volume_acceleration = bool(row.get("volume_acceleration", False))

    if instant_rvol < 2.5:
        return False, "Instant RVOL too low"

    if not volume_acceleration:
        return False, "No volume acceleration"

    return True, "Liquidity passed"


def passes_trend_layer(row):
    price = float(row.get("price", 0) or 0)
    vwap = float(row.get("vwap", 0) or 0)
    ema9 = float(row.get("ema9", 0) or 0)
    ema20 = float(row.get("ema20", 0) or 0)
    close_position = float(row.get("close_position", 0) or 0)

    if vwap <= 0 or ema9 <= 0 or ema20 <= 0:
        return False, "Missing VWAP or EMA"

    if price <= vwap:
        return False, "Price below VWAP"

    if price <= ema9:
        return False, "Price below EMA9"

    if close_position < 0.65:
        return False, "Weak close position"

    if ema9 < ema20 * 0.995:
        return False, "EMA trend not ready"

    return True, "Trend passed"


def passes_ignition_layer(row):
    move_3m = float(row.get("move_3m", 0) or 0)
    move_5m = float(row.get("move_5m", 0) or 0)
    real_breakout = bool(row.get("real_breakout", False))

    if move_3m >= 0.60:
        return True, "3m ignition"

    if move_5m >= 1.00:
        return True, "5m ignition"

    if real_breakout:
        return True, "Real breakout"

    return False, "No ignition"


def passes_smart_resistance_layer(row):
    resistance_distance_pct = float(
        row.get("resistance_distance_pct", 0) or 0
    )
    real_breakout = bool(row.get("real_breakout", False))
    instant_rvol = float(row.get("instant_rvol", 0) or 0)
    volume_acceleration = bool(row.get("volume_acceleration", False))
    close_position = float(row.get("close_position", 0) or 0)

    buying_pressure = (
        instant_rvol >= 2.5
        and volume_acceleration
        and close_position >= 0.65
    )

    if resistance_distance_pct <= 0.20 and not real_breakout:
        return False, "Too close to resistance without breakout"

    if real_breakout:
        return True, "Resistance breakout"

    if resistance_distance_pct <= 2.0:
        if buying_pressure:
            return True, "Strong pressure near resistance"

        return False, "Near resistance with weak pressure"

    return True, "Resistance passed"


def passes_protection_layer(row):
    distribution_score = float(row.get("distribution_score", 0) or 0)
    upper_wick_pct = float(row.get("upper_wick_pct", 0) or 0)
    rsi = float(row.get("rsi", 0) or 0)
    overextended = bool(row.get("overextended", False))
    repeated_rejection = bool(row.get("repeated_rejection", False))

    if distribution_score > 20:
        return False, "Distribution risk"

    if upper_wick_pct > 0.35:
        return False, "Upper wick too high"

    if rsi > 78:
        return False, "RSI too high"

    if overextended:
        return False, "Overextended"

    if repeated_rejection:
        return False, "Repeated rejection"

    return True, "Protection passed"


def grade_entry(row):
    real_breakout = bool(row.get("real_breakout", False))
    resistance_distance_pct = float(
        row.get("resistance_distance_pct", 0) or 0
    )
    instant_rvol = float(row.get("instant_rvol", 0) or 0)
    volume_acceleration = bool(row.get("volume_acceleration", False))
    close_position = float(row.get("close_position", 0) or 0)
    move_3m = float(row.get("move_3m", 0) or 0)
    move_5m = float(row.get("move_5m", 0) or 0)

    fresh_breakout_zone = (
        resistance_distance_pct <= 0
        and resistance_distance_pct >= -1.0
    )

    strong_momentum = (
        move_3m >= 0.60
        or move_5m >= 1.00
    )

    acceptable_momentum = (
        move_3m >= 0.40
        or move_5m >= 0.70
    )

    if (
        real_breakout
        and fresh_breakout_zone
        and instant_rvol >= 4
        and volume_acceleration
        and close_position >= 0.75
        and strong_momentum
    ):
        return "A++"

    if (
        resistance_distance_pct <= 1.0
        and resistance_distance_pct >= -1.0
        and instant_rvol >= 2.5
        and volume_acceleration
        and close_position >= 0.65
        and acceptable_momentum
    ):
        return "A"

    return "A"

def analyze_entry_opportunity(row):
    checks = [
        passes_layer0,
        passes_liquidity_layer,
        passes_trend_layer,
        passes_ignition_layer,
        passes_smart_resistance_layer,
        passes_protection_layer,
    ]

    for check in checks:
        passed, reason = check(row)

        if not passed:
            return {
                "ready_to_alert": False,
                "grade": None,
                "reason": reason,
            }

    confirmed_result = analyze_confirmed_breakout(
        row
    )

    if confirmed_result:
        return confirmed_result 
    grade = grade_entry(row)

    if grade == "A":
        return {
            "ready_to_alert": False,
            "grade": grade,
            "reason": "Grade A filtered",
        }

    return {
        "ready_to_alert": True,
        "grade": grade,
        "reason": "Direct entry confirmed",
    }
