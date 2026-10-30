from __future__ import annotations
from datetime import datetime,timedelta,timezone
from statistics import mean
from typing import Any
import math
from zoneinfo import ZoneInfo
UTC=timezone.utc
NY=ZoneInfo("America/New_York")
def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)

def clamp(value: float, low: float=0.0, high: float=100.0) -> float:
    return max(low, min(high, float(value)))

def efficiency_ratio_45(bars: list[dict[str, Any]]) -> float | None:
    if len(bars) < 2:
        return None
    closes = [float(bar['c']) for bar in bars]
    path = sum((abs(current - previous) for previous, current in zip(closes, closes[1:])))
    return (closes[-1] - closes[0]) / path if path > 1e-12 else 0.0

def phase2_features(history: list[dict[str, Any]], signal_time: datetime) -> tuple[dict[str, float], dict[str, Any]] | None:
    completed = sorted([bar for bar in history if bar.get('t') and parse_dt(bar['t']) + timedelta(minutes=1) <= signal_time], key=lambda bar: bar['t'])
    if len(completed) < 24:
        return None
    signal_time = parse_dt(completed[-1]['t'])
    tail = completed[-30:]
    closes = [float(bar['c']) for bar in tail]
    highs = [float(bar['h']) for bar in tail]
    total_volume = sum((float(bar.get('v') or 0) for bar in completed))
    vwap = sum((float(bar.get('vw') or bar['c']) * float(bar.get('v') or 0) for bar in completed)) / total_volume if total_volume else mean(closes)
    price = closes[-1]
    recent = completed[-12:]
    prior = completed[-24:-12]
    recent_volume = mean((float(bar.get('v') or 0) for bar in recent))
    prior_volume = mean((float(bar.get('v') or 0) for bar in prior)) if prior else max(1.0, recent_volume)
    acceleration = recent_volume / max(1.0, prior_volume)
    low, high = (min(closes), max(closes))
    span = max(1e-09, high - low)
    acceptance = clamp(100 * mean((1 if close >= low + 0.55 * span else 0 for close in closes)))
    close_position = clamp(100 * (price - low) / span)
    demand = clamp(0.5 * close_position + 0.25 * acceptance + 0.25 * min(100, acceleration * 40))
    resistance = max(highs[-21:-1] or highs[-1:])
    reclaim = 100 if price >= resistance * 0.998 else clamp(50 + (price / resistance - 1) * 1000)
    pullback = clamp(100 - max(0, (high - price) / max(high, 1e-09) * 500))
    reference = float(completed[0]['o'])
    session_change = (price / reference - 1) * 100
    extension = clamp(max(0, session_change - 12) * 4 + max(0, (price / max(vwap, 1e-09) - 1) * 100 - 8) * 5)
    trajectory = clamp(50 + (closes[-1] / max(closes[max(0, len(closes) - 10)], 1e-09) - 1) * 700)
    continuity = clamp(mean((1 if float(bar.get('n') or 0) > 0 else 0 for bar in recent)) * 100)
    spread_proxy = clamp(mean(((float(bar['h']) - float(bar['l'])) / max(float(bar['c']), 1e-09) * 100 for bar in recent)), 0, 25)
    spread_quality = clamp(100 - spread_proxy * 15)
    participation = clamp(mean([min(100, acceleration * 45), continuity, min(100, math.log10(total_volume + 1) * 18)]))
    persistence = clamp(mean([acceptance, pullback, trajectory]))
    liquidity = clamp(mean([spread_quality, continuity]))
    context = clamp(mean([100 - extension, trajectory]))
    opportunity = clamp(0.24 * participation + 0.34 * demand + 0.18 * persistence + 0.1 * liquidity + 0.14 * context)
    failure = clamp(0.3 * (100 - demand) + 0.25 * (100 - acceptance) + 0.2 * (100 - pullback) + 0.15 * (100 - reclaim) + 0.1 * (100 - spread_quality))
    window_start = signal_time - timedelta(minutes=45)
    window45 = [bar for bar in completed if parse_dt(bar['t']) >= window_start]
    if len(window45) < 5:
        return None
    first_open = float(window45[0]['o'])
    price_change = (float(window45[-1]['c']) / first_open - 1) * 100
    er45 = efficiency_ratio_45(window45)
    if er45 is None or price <= 0:
        return None
    local = signal_time.astimezone(NY)
    minutes_since_open = local.hour * 60 + local.minute - 570
    model_features = {'price_change_pct_last45m': float(price_change), 'er45': float(er45), 'price_change_x_er45': float(price_change * er45), 'log_signal_price': float(math.log(price)), 'opportunity': float(opportunity), 'failure_pressure': float(failure), 'minutes_since_regular_open': float(minutes_since_open)}
    diagnostics = {'price': price, 'vwap': vwap, 'resistance': resistance, 'demand_efficiency': demand, 'price_acceptance': acceptance, 'volume_acceleration': acceleration, 'spread_proxy_pct': spread_proxy, 'bars_used': len(completed), 'bars_used_last45m': len(window45), 'base_ready': bool(opportunity >= 88 and failure <= 35 and (price >= vwap) and (demand >= 65) and (acceptance >= 62) and (acceleration >= 1))}
    return (model_features, diagnostics)
