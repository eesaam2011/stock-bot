from __future__ import annotations

import hashlib
import fnmatch
import gzip
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from statistics import mean, median
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
from flask import Flask, jsonify, request, send_file


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")
FEATURE_NAMES = (
    "price_change_pct_last45m",
    "er45",
    "price_change_x_er45",
    "log_signal_price",
    "opportunity",
    "failure_pressure",
    "minutes_since_regular_open",
)

VERSION = "1.4.0"
BUILD = "INDEPENDENT-PRIORITY-RADAR-2026-09-05-E"
PROTOCOL_ID = "IPR-PHASE2-SHADOW-2026-09-03-A"
PROTOCOL = {
    "protocol_id": PROTOCOL_ID,
    "purpose": "Independent Phase-2 priority radar with simple causal confirmation and complete shadow outcomes.",
    "quality_model": {
        "algorithm": "L2 logistic regression",
        "l2_penalty": 1.0,
        "features": list(FEATURE_NAMES),
        "selection": "top 5 percent of full-development fitted probabilities",
        "historical_oof_context": {
            "baseline_explosion_ge10": "160/16894 (0.9471%)",
            "top5_explosion_ge10": "19/352 (5.3977%)",
            "lift": "approximately 5.7x",
        },
    },
    "confirmation": {
        "completed_bar_only": True,
        "close_above_frozen_resistance": True,
        "upper_wick_rule": "upper_wick <= real_body and upper_wick <= 35 percent of full range",
        "confirmation_window_minutes": 15,
        "telegram_only_after_confirmation": True,
    },
    "outcomes": {"primary_minutes": 60, "levels_pct": [2.0, 5.0, 10.0]},
    "safety": {"orders_enabled": False, "automatic_execution": False},
}
PROTOCOL_SHA256 = hashlib.sha256(
    json.dumps(PROTOCOL, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

# Runtime sampling is deliberately outside PROTOCOL. It does not alter the
# frozen quality model, its cutoff, or the official minute-bar outcome rules.
MONITORING_SPEC = {
    "market_scan_seconds": 30,
    "pending_confirmation_seconds": 10,
    "confirmed_live_sample_seconds": 5,
    "live_sample_source": "Alpaca latestTrade snapshot",
    "official_outcome_source": "Alpaca one-minute bars",
}

# This audit is deliberately outside the frozen live protocol. It never changes
# the live model/cutoff and never sends Telegram alerts or orders.
HISTORICAL_CONFIRMATION_AUDIT_SPEC = {
    "audit_id": "IPR-HISTORICAL-CONFIRMATION-2026-09-04-A",
    "development_selection": "three expanding-window OOF folds; top 5 percent ranked independently in each fold",
    "legacy_holdout_selection": "full-development frozen model and frozen probability cutoff; audit only",
    "confirmation_window_minutes": 15,
    "outcome_window_minutes": 60,
    "targets_pct": [2.0, 5.0, 10.0],
    "stops_pct": [1.0, 2.0, 3.0, 4.0, 5.0],
    "same_minute_stop_target": "AMBIGUOUS",
    "market_data": "Alpaca SIP one-minute raw bars fetched after hours",
    "safety": {
        "alerts_enabled": False,
        "orders_enabled": False,
        "changes_live_model": False,
        "legacy_holdout_can_approve_live": False,
    },
}

# Isolated research only. These fields and labels are frozen before the run;
# none of them is used by the live scanner, confirmation path, or Telegram.
EARLY_CAUSAL_FEATURE_NAMES = (
    "discovery_body_pct",
    "discovery_range_pct",
    "discovery_close_location",
    "discovery_upper_wick_to_range",
    "discovery_upper_wick_to_body",
    "log_discovery_volume",
    "volume_ratio_to_prior5",
    "volume_acceleration_3v3",
    "return_2m_pct",
    "return_3m_pct",
    "return_5m_pct",
    "distance_to_resistance_pct",
    "distance_above_vwap_pct",
) + FEATURE_NAMES

EARLY_CAUSAL_ENTRY_SPEC = {
    "research_id": "IPR-EARLY-CAUSAL-ENTRY-2026-09-05-A",
    "purpose": "Test whether information available by discovery-bar close can select profitable early entries.",
    "candidate_count_expected": 583,
    "development_candidates_expected": 352,
    "legacy_holdout_candidates_expected": 231,
    "primary_outcome": "60-minute time-exit net return from discovery price",
    "decision_cost_pct_round_trip": 0.25,
    "primary_label": "net_time_exit_return_pct > 0",
    "diagnostic_policies_only": [
        {"stop_pct": 4.0, "target_pct": 2.0},
        {"stop_pct": 5.0, "target_pct": 2.0},
    ],
    "feature_availability": "end of completed discovery candle only",
    "model": {
        "algorithm": "L2 logistic regression",
        "l2_penalty": 1.0,
        "outer_evaluation": "three expanding chronological Development folds",
        "feature_screen": "same signed profitable-minus-losing standardized mean difference in three training-only temporal blocks",
        "minimum_median_absolute_effect": 0.10,
        "maximum_features": 6,
        "selection_threshold": "training probability median; no threshold search",
    },
    "judgment": {
        "PROMISING": "selected net PF > 1 and selected average net return > 0 in every outer fold",
        "NO_STABLE_SIGNAL": "otherwise",
    },
    "safety": {
        "alerts_enabled": False,
        "orders_enabled": False,
        "changes_live_model": False,
        "changes_live_cutoff": False,
        "legacy_holdout_can_approve_live": False,
    },
}

def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def find_symbol_record(document: Any, symbol: str) -> Any:
    """Accept mapping-, list-, or nested float-cache schemas without changing the source."""
    target = symbol.upper()
    if isinstance(document, dict):
        for key in (target, target.lower()):
            if key in document:
                return document[key]
        own_symbol = document.get("symbol") or document.get("ticker") or document.get("code")
        if str(own_symbol or "").upper() == target:
            return document
        for container in ("symbols", "data", "stocks", "results", "items", "floats"):
            if container in document:
                found = find_symbol_record(document[container], target)
                if found is not None:
                    return found
    elif isinstance(document, list):
        for item in document:
            if isinstance(item, dict):
                own_symbol = item.get("symbol") or item.get("ticker") or item.get("code")
                if str(own_symbol or "").upper() == target:
                    return item
    return None


class RedisREST:
    def __init__(self) -> None:
        self.url = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
        self.token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def command(self, *parts: Any) -> Any:
        if not self.configured:
            raise RuntimeError("Redis environment is missing")
        payload = json.dumps(list(parts), ensure_ascii=False).encode("utf-8")
        req = Request(
            self.url,
            data=payload,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=90) as response:
                result = json.load(response)
        except HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"Redis HTTP {exc.code}: {detail}") from exc
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result.get("result")

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.command("GET", key)
        return default if raw is None else json.loads(raw)

    def set_json(self, key: str, value: Any) -> Any:
        return self.command("SET", key, json_compact(value))

    def hget_json(self, key: str, field: str, default: Any = None) -> Any:
        raw = self.command("HGET", key, field)
        return default if raw is None else json.loads(raw)

    def hset_json(self, key: str, field: str, value: Any) -> Any:
        return self.command("HSET", key, field, json_compact(value))

    def scan_hash_json(self, key: str, count: int = 500) -> Iterable[tuple[str, Any]]:
        cursor = "0"
        while True:
            result = self.command("HSCAN", key, cursor, "COUNT", count)
            cursor = str(result[0])
            pairs = result[1] or []
            for index in range(0, len(pairs), 2):
                try:
                    yield str(pairs[index]), json.loads(pairs[index + 1])
                except (TypeError, json.JSONDecodeError):
                    continue
            if cursor == "0":
                return


class AlpacaClient:
    def __init__(self) -> None:
        self.headers = {
            "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", os.getenv("APCA_API_KEY_ID", "")),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", os.getenv("APCA_API_SECRET_KEY", "")),
        }
        self.data_base = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
        self.trading_base = os.getenv("ALPACA_TRADING_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.headers["APCA-API-KEY-ID"] and self.headers["APCA-API-SECRET-KEY"])

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        target = url + ("?" + urlencode(params, doseq=True) if params else "")
        for attempt in range(6):
            try:
                with urlopen(Request(target, headers=self.headers), timeout=90) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code == 429 and attempt < 5:
                    time.sleep(min(20, 2 ** attempt))
                    continue
                detail = exc.read(500).decode("utf-8", "replace")
                raise RuntimeError(f"Alpaca HTTP {exc.code}: {detail}") from exc
            except URLError as exc:
                if attempt < 5:
                    time.sleep(min(20, 2 ** attempt))
                    continue
                raise RuntimeError(f"Alpaca network error: {exc}") from exc

    def assets(self) -> list[dict[str, Any]]:
        return list(self.get(f"{self.trading_base}/v2/assets", {"status": "active", "asset_class": "us_equity"}) or [])

    def snapshots(self, symbols: list[str], feed: str = "sip") -> dict[str, Any]:
        if not symbols:
            return {}
        result = self.get(f"{self.data_base}/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed})
        return result if isinstance(result, dict) else {}

    def bars(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        feed: str = "sip",
        adjustment: str = "raw",
    ) -> dict[str, list[dict[str, Any]]]:
        output = {symbol: [] for symbol in symbols}
        page_token = None
        while True:
            params: dict[str, Any] = {
                "symbols": ",".join(symbols), "timeframe": "1Min",
                "start": iso(start), "end": iso(end), "feed": feed,
                "adjustment": adjustment, "limit": 10000, "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            page = self.get(f"{self.data_base}/v2/stocks/bars", params)
            for symbol, rows in (page.get("bars") or {}).items():
                output.setdefault(symbol, []).extend(rows or [])
            page_token = page.get("next_page_token")
            if not page_token:
                return output


class QualityModel:
    def __init__(self, artifact: dict[str, Any]):
        self.mean = np.asarray(artifact["standardization_mean"], dtype=float)
        self.scale = np.asarray(artifact["standardization_scale"], dtype=float)
        self.beta = np.asarray(artifact["intercept_and_standardized_coefficients"], dtype=float)
        self.cutoff = float(artifact["frozen_probability_cutoff"])

    def probability(self, features: dict[str, float]) -> float:
        row = np.asarray([float(features[name]) for name in FEATURE_NAMES], dtype=float)
        z = (row - self.mean) / self.scale
        value = float(self.beta[0] + z @ self.beta[1:])
        value = max(-35.0, min(35.0, value))
        return 1.0 / (1.0 + math.exp(-value))


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, max_iter: int = 100) -> dict[str, Any]:
    feature_mean = X.mean(axis=0)
    feature_scale = X.std(axis=0)
    feature_scale[feature_scale < 1e-12] = 1.0
    Z = (X - feature_mean) / feature_scale
    design = np.column_stack((np.ones(len(Z)), Z))
    beta = np.zeros(design.shape[1], dtype=float)
    penalty = np.eye(design.shape[1], dtype=float) * float(l2)
    penalty[0, 0] = 0.0
    converged = False
    for iteration in range(max_iter):
        logits = np.clip(design @ beta, -35, 35)
        probability = 1.0 / (1.0 + np.exp(-logits))
        weights = np.maximum(probability * (1.0 - probability), 1e-8)
        gradient = design.T @ (y - probability) - penalty @ beta
        information = design.T @ (design * weights[:, None]) + penalty
        step = np.linalg.solve(information, gradient)
        beta_next = beta + step
        if float(np.max(np.abs(beta_next - beta))) < 1e-8:
            beta = beta_next
            converged = True
            break
        beta = beta_next
    return {
        "mean": feature_mean, "scale": feature_scale, "beta": beta,
        "converged": converged, "iterations": iteration + 1,
    }


def efficiency_ratio_45(bars: list[dict[str, Any]]) -> float | None:
    if len(bars) < 2:
        return None
    closes = [float(bar["c"]) for bar in bars]
    path = sum(abs(current - previous) for previous, current in zip(closes, closes[1:]))
    return (closes[-1] - closes[0]) / path if path > 1e-12 else 0.0


def phase2_features(history: list[dict[str, Any]], signal_time: datetime) -> tuple[dict[str, float], dict[str, Any]] | None:
    completed = sorted(
        [bar for bar in history if bar.get("t") and parse_dt(bar["t"]) + timedelta(minutes=1) <= signal_time],
        key=lambda bar: bar["t"],
    )
    if len(completed) < 24:
        return None
    signal_time = parse_dt(completed[-1]["t"])
    tail = completed[-30:]
    closes = [float(bar["c"]) for bar in tail]
    highs = [float(bar["h"]) for bar in tail]
    total_volume = sum(float(bar.get("v") or 0) for bar in completed)
    vwap = (
        sum(float(bar.get("vw") or bar["c"]) * float(bar.get("v") or 0) for bar in completed) / total_volume
        if total_volume else mean(closes)
    )
    price = closes[-1]
    recent = completed[-12:]
    prior = completed[-24:-12]
    recent_volume = mean(float(bar.get("v") or 0) for bar in recent)
    prior_volume = mean(float(bar.get("v") or 0) for bar in prior) if prior else max(1.0, recent_volume)
    acceleration = recent_volume / max(1.0, prior_volume)
    low, high = min(closes), max(closes)
    span = max(1e-9, high - low)
    acceptance = clamp(100 * mean(1 if close >= low + 0.55 * span else 0 for close in closes))
    close_position = clamp(100 * (price - low) / span)
    demand = clamp(0.5 * close_position + 0.25 * acceptance + 0.25 * min(100, acceleration * 40))
    resistance = max(highs[-21:-1] or highs[-1:])
    reclaim = 100 if price >= resistance * 0.998 else clamp(50 + (price / resistance - 1) * 1000)
    pullback = clamp(100 - max(0, (high - price) / max(high, 1e-9) * 500))
    reference = float(completed[0]["o"])
    session_change = (price / reference - 1) * 100
    extension = clamp(max(0, session_change - 12) * 4 + max(0, (price / max(vwap, 1e-9) - 1) * 100 - 8) * 5)
    trajectory = clamp(50 + (closes[-1] / max(closes[max(0, len(closes) - 10)], 1e-9) - 1) * 700)
    continuity = clamp(mean(1 if float(bar.get("n") or 0) > 0 else 0 for bar in recent) * 100)
    spread_proxy = clamp(mean((float(bar["h"]) - float(bar["l"])) / max(float(bar["c"]), 1e-9) * 100 for bar in recent), 0, 25)
    spread_quality = clamp(100 - spread_proxy * 15)
    participation = clamp(mean([min(100, acceleration * 45), continuity, min(100, math.log10(total_volume + 1) * 18)]))
    persistence = clamp(mean([acceptance, pullback, trajectory]))
    liquidity = clamp(mean([spread_quality, continuity]))
    context = clamp(mean([100 - extension, trajectory]))
    opportunity = clamp(0.24 * participation + 0.34 * demand + 0.18 * persistence + 0.10 * liquidity + 0.14 * context)
    failure = clamp(0.30 * (100 - demand) + 0.25 * (100 - acceptance) + 0.20 * (100 - pullback) + 0.15 * (100 - reclaim) + 0.10 * (100 - spread_quality))

    window_start = signal_time - timedelta(minutes=45)
    window45 = [bar for bar in completed if parse_dt(bar["t"]) >= window_start]
    if len(window45) < 5:
        return None
    first_open = float(window45[0]["o"])
    price_change = (float(window45[-1]["c"]) / first_open - 1) * 100
    er45 = efficiency_ratio_45(window45)
    if er45 is None or price <= 0:
        return None
    local = signal_time.astimezone(NY)
    minutes_since_open = local.hour * 60 + local.minute - 570
    model_features = {
        "price_change_pct_last45m": float(price_change),
        "er45": float(er45),
        "price_change_x_er45": float(price_change * er45),
        "log_signal_price": float(math.log(price)),
        "opportunity": float(opportunity),
        "failure_pressure": float(failure),
        "minutes_since_regular_open": float(minutes_since_open),
    }
    diagnostics = {
        "price": price, "vwap": vwap, "resistance": resistance,
        "demand_efficiency": demand, "price_acceptance": acceptance,
        "volume_acceleration": acceleration, "spread_proxy_pct": spread_proxy,
        "bars_used": len(completed), "bars_used_last45m": len(window45),
        "base_ready": bool(
            opportunity >= 88 and failure <= 35 and price >= vwap
            and demand >= 65 and acceptance >= 62 and acceleration >= 1
        ),
    }
    return model_features, diagnostics


def confirmation_metrics(bar: dict[str, Any], resistance: float) -> dict[str, Any]:
    open_price = float(bar["o"])
    high = float(bar["h"])
    low = float(bar["l"])
    close = float(bar["c"])
    full_range = max(0.0, high - low)
    body = abs(close - open_price)
    upper_wick = max(0.0, high - max(open_price, close))
    wick_ratio = upper_wick / full_range if full_range > 0 else 0.0
    wick_limit = body
    close_pass = close > float(resistance)
    wick_pass = upper_wick <= wick_limit + 1e-12 and wick_ratio <= 0.35
    reasons = []
    if not close_pass:
        reasons.append("close_not_above_frozen_resistance")
    if not wick_pass:
        reasons.append("clear_upper_wick_rejection")
    return {
        "bar_ts": bar["t"], "open": open_price, "high": high, "low": low, "close": close,
        "real_body": body, "upper_wick": upper_wick, "full_range": full_range,
        "upper_wick_to_range": wick_ratio,
        "upper_wick_to_body": upper_wick / body if body > 1e-12 else None,
        "wick_limit": wick_limit, "close_above_resistance": close_pass,
        "upper_wick_pass": wick_pass, "confirmed": close_pass and wick_pass,
        "reasons": reasons,
    }


def outcome_metrics(bars: list[dict[str, Any]], start: datetime, entry_price: float, minutes: int = 60) -> dict[str, Any]:
    end = start + timedelta(minutes=minutes)
    future = [
        bar for bar in sorted(bars, key=lambda item: item["t"])
        if start < parse_dt(bar["t"]) <= end
    ]
    if not future or entry_price <= 0:
        return {"complete": False, "forward_bars": len(future)}
    highest = max(future, key=lambda bar: float(bar["h"]))
    lowest = min(future, key=lambda bar: float(bar["l"]))
    mfe = (float(highest["h"]) / entry_price - 1) * 100
    mae = (float(lowest["l"]) / entry_price - 1) * 100
    result: dict[str, Any] = {
        "complete": len(future) >= minutes or parse_dt(future[-1]["t"]) >= end - timedelta(minutes=1),
        "forward_bars": len(future), "mfe_pct": round(mfe, 5), "mae_pct": round(mae, 5),
        "highest_price": float(highest["h"]), "highest_ts": highest["t"],
        "lowest_price": float(lowest["l"]), "lowest_ts": lowest["t"],
        "last_price": float(future[-1]["c"]), "last_ts": future[-1]["t"],
        "close_return_pct": round((float(future[-1]["c"]) / entry_price - 1) * 100, 5),
    }
    for level in (2.0, 5.0, 10.0):
        first = next((bar for bar in future if float(bar["h"]) >= entry_price * (1 + level / 100)), None)
        name = str(int(level))
        result[f"reached_{name}pct"] = first is not None
        result[f"time_to_{name}pct_minutes"] = (
            round((parse_dt(first["t"]) - start).total_seconds() / 60, 2) if first else None
        )
    return result


def stop_target_path_metrics(
    bars: list[dict[str, Any]],
    start: datetime,
    entry_price: float,
    minutes: int = 60,
    stops: tuple[float, ...] = (1.0, 2.0, 3.0, 4.0, 5.0),
    targets: tuple[float, ...] = (2.0, 5.0, 10.0),
) -> dict[str, Any]:
    """Minute-causal path audit; a stop and target in one bar has unknown order."""
    end = start + timedelta(minutes=minutes)
    future = [
        bar for bar in sorted(bars, key=lambda item: item["t"])
        if start < parse_dt(bar["t"]) <= end
    ]
    if not future or entry_price <= 0:
        return {"complete": False, "forward_bars": len(future), "pairs": {}}

    first_stop: dict[float, dict[str, Any] | None] = {}
    first_target: dict[float, dict[str, Any] | None] = {}
    for level in stops:
        threshold = entry_price * (1.0 - level / 100.0)
        first_stop[level] = next((bar for bar in future if float(bar["l"]) <= threshold), None)
    for level in targets:
        threshold = entry_price * (1.0 + level / 100.0)
        first_target[level] = next((bar for bar in future if float(bar["h"]) >= threshold), None)

    pairs: dict[str, Any] = {}
    for stop in stops:
        for target in targets:
            stop_bar = first_stop[stop]
            target_bar = first_target[target]
            stop_ts = parse_dt(stop_bar["t"]) if stop_bar else None
            target_ts = parse_dt(target_bar["t"]) if target_bar else None
            if stop_ts is not None and target_ts is not None and stop_ts == target_ts:
                order = "AMBIGUOUS"
            elif stop_ts is not None and (target_ts is None or stop_ts < target_ts):
                order = "STOP_FIRST"
            elif target_ts is not None and (stop_ts is None or target_ts < stop_ts):
                order = "TARGET_FIRST"
            else:
                order = "NEITHER"
            pairs[f"stop_{int(stop)}_target_{int(target)}"] = {
                "order": order,
                "stop_ts": stop_bar["t"] if stop_bar else None,
                "target_ts": target_bar["t"] if target_bar else None,
            }

    highest = max(future, key=lambda bar: float(bar["h"]))
    highest_ts = parse_dt(highest["t"])
    through_peak = [bar for bar in future if parse_dt(bar["t"]) <= highest_ts]
    lowest_before_peak = min(through_peak, key=lambda bar: float(bar["l"]))
    return {
        "complete": len(future) >= minutes or parse_dt(future[-1]["t"]) >= end - timedelta(minutes=1),
        "forward_bars": len(future),
        "highest_price": float(highest["h"]),
        "highest_ts": highest["t"],
        "lowest_before_peak_price": float(lowest_before_peak["l"]),
        "lowest_before_peak_ts": lowest_before_peak["t"],
        "drawdown_before_peak_pct": round((float(lowest_before_peak["l"]) / entry_price - 1.0) * 100.0, 5),
        "pairs": pairs,
    }


def early_causal_features(
    bars: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any]] | None:
    """Build features using only bars known by the discovery-bar close."""
    signal_time = parse_dt(str(candidate["signal_ts"]))
    local_signal = signal_time.astimezone(NY)
    session_open_local = datetime.combine(local_signal.date(), dtime(9, 30), tzinfo=NY)
    session_open = session_open_local.astimezone(UTC)
    causal = sorted(
        [
            bar for bar in bars
            if bar.get("t") and session_open <= parse_dt(str(bar["t"])) <= signal_time
        ],
        key=lambda item: item["t"],
    )
    if len(causal) < 6:
        return None
    discovery = causal[-1]
    if parse_dt(str(discovery["t"])) != signal_time:
        return None

    open_price = float(discovery["o"])
    high = float(discovery["h"])
    low = float(discovery["l"])
    close = float(discovery["c"])
    if min(open_price, high, low, close) <= 0:
        return None
    full_range = max(0.0, high - low)
    body = abs(close - open_price)
    upper_wick = max(0.0, high - max(open_price, close))
    close_location = (close - low) / full_range if full_range > 1e-12 else 0.5
    volumes = [float(bar.get("v") or 0.0) for bar in causal]
    prior_five = volumes[-6:-1]
    recent_three = volumes[-3:]
    prior_three = volumes[-6:-3]
    vwap_denominator = sum(volumes)
    causal_vwap = (
        sum(float(bar.get("vw") or bar["c"]) * volume for bar, volume in zip(causal, volumes))
        / vwap_denominator
        if vwap_denominator > 0
        else mean(float(bar["c"]) for bar in causal)
    )
    closes = [float(bar["c"]) for bar in causal]

    def trailing_return(minutes: int) -> float:
        if len(closes) <= minutes or closes[-1 - minutes] <= 0:
            return 0.0
        return (closes[-1] / closes[-1 - minutes] - 1.0) * 100.0

    frozen = candidate.get("features") or {}
    if any(name not in frozen for name in FEATURE_NAMES):
        return None
    signal_price = float(candidate["signal_price"])
    resistance = float(candidate["frozen_resistance"])
    features = {
        "discovery_body_pct": (close - open_price) / open_price * 100.0,
        "discovery_range_pct": full_range / open_price * 100.0,
        "discovery_close_location": clamp(close_location, 0.0, 1.0),
        "discovery_upper_wick_to_range": upper_wick / full_range if full_range > 1e-12 else 0.0,
        "discovery_upper_wick_to_body": min(10.0, upper_wick / body) if body > 1e-12 else (10.0 if upper_wick else 0.0),
        "log_discovery_volume": math.log1p(max(0.0, volumes[-1])),
        "volume_ratio_to_prior5": volumes[-1] / max(1.0, mean(prior_five)),
        "volume_acceleration_3v3": mean(recent_three) / max(1.0, mean(prior_three)),
        "return_2m_pct": trailing_return(2),
        "return_3m_pct": trailing_return(3),
        "return_5m_pct": trailing_return(5),
        "distance_to_resistance_pct": (resistance / signal_price - 1.0) * 100.0,
        "distance_above_vwap_pct": (signal_price / max(causal_vwap, 1e-12) - 1.0) * 100.0,
    }
    features.update({name: float(frozen[name]) for name in FEATURE_NAMES})
    if any(not math.isfinite(float(value)) for value in features.values()):
        return None
    diagnostics = {
        "bars_available_at_discovery": len(causal),
        "discovery_bar_ts": discovery["t"],
        "causal_vwap": round(float(causal_vwap), 8),
        "discovery_close": close,
        "last_six_causal_bars": causal[-6:],
    }
    return {name: float(features[name]) for name in EARLY_CAUSAL_FEATURE_NAMES}, diagnostics


def exact_policy_return(
    path: dict[str, Any],
    outcome: dict[str, Any],
    stop_pct: float,
    target_pct: float,
    cost_pct: float = 0.25,
) -> float | None:
    """Conservative exact return: same-bar ambiguity is treated as stop first."""
    if not outcome.get("complete"):
        return None
    detail = (path.get("pairs") or {}).get(f"stop_{int(stop_pct)}_target_{int(target_pct)}") or {}
    order = detail.get("order")
    if order == "TARGET_FIRST":
        gross = float(target_pct)
    elif order in {"STOP_FIRST", "AMBIGUOUS"}:
        gross = -float(stop_pct)
    else:
        gross = float(outcome.get("close_return_pct") or 0.0)
    return gross - float(cost_pct)


def return_statistics(returns: list[float]) -> dict[str, Any]:
    values = [float(value) for value in returns if value is not None and math.isfinite(float(value))]
    if not values:
        return {
            "count": 0, "profit_factor": None, "average_return_pct": None,
            "median_return_pct": None, "total_return_points": None,
            "win_rate_pct": None, "maximum_drawdown_points": None,
        }
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    equity = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    return {
        "count": len(values),
        "wins": sum(value > 0 for value in values),
        "losses": sum(value < 0 for value in values),
        "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
        "average_return_pct": round(float(mean(values)), 6),
        "median_return_pct": round(float(median(values)), 6),
        "total_return_points": round(float(sum(values)), 6),
        "win_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100.0, 4),
        "maximum_drawdown_points": round(maximum_drawdown, 6),
    }


def update_live_tracking(
    tracking: dict[str, Any] | None,
    entry_price: float,
    price: float,
    captured_at: str,
    market_ts: str | None,
) -> tuple[dict[str, Any], bool]:
    """Update supplemental 5-second extrema; official MFE/MAE stays minute-bar based."""
    current = dict(tracking or {})
    if entry_price <= 0 or price <= 0:
        return current, False
    if current.get("last_market_ts") == market_ts and current.get("last_price") == price:
        return current, False
    gain_pct = (price / entry_price - 1) * 100
    samples = int(current.get("samples") or 0) + 1
    current.update({
        "official": False,
        "source": "Alpaca latestTrade snapshot sampled every 5 seconds",
        "samples_key": current.get("samples_key"),
        "samples": samples,
        "last_sample_at": captured_at,
        "last_market_ts": market_ts,
        "last_price": price,
        "last_return_pct": round(gain_pct, 5),
    })
    if current.get("peak_price") is None or price > float(current["peak_price"]):
        current.update({"peak_price": price, "peak_ts": market_ts or captured_at, "peak_gain_pct": round(gain_pct, 5)})
    if current.get("trough_price") is None or price < float(current["trough_price"]):
        current.update({"trough_price": price, "trough_ts": market_ts or captured_at, "drawdown_pct": round(gain_pct, 5)})
    return current, True


class IndependentPriorityRadar:
    def __init__(self, redis_client: RedisREST | None = None, alpaca: AlpacaClient | None = None):
        self.redis = redis_client or RedisREST()
        self.alpaca = alpaca or AlpacaClient()
        self.prefix = os.getenv("IPR_REDIS_PREFIX", "independent_priority_radar:v1")
        self.source_prefix = os.getenv("NDR_BT_REDIS_PREFIX", "next_day_radar_backtest_v3")
        self.scan_interval = max(15, int(os.getenv("IPR_SCAN_INTERVAL_SEC", "30")))
        self.pending_interval = max(5, int(os.getenv("IPR_PENDING_INTERVAL_SEC", "10")))
        self.live_sample_interval = max(5, int(os.getenv("IPR_LIVE_SAMPLE_INTERVAL_SEC", "5")))
        self.universe_refresh = max(300, int(os.getenv("IPR_UNIVERSE_REFRESH_SEC", "14400")))
        self.snapshot_refresh = max(60, int(os.getenv("IPR_SNAPSHOT_REFRESH_SEC", "300")))
        self.confirmation_window = max(1, int(os.getenv("IPR_CONFIRMATION_WINDOW_MIN", "15")))
        self.price_min = float(os.getenv("IPR_PRICE_MIN", "0.50"))
        self.price_max = float(os.getenv("IPR_PRICE_MAX", "40.00"))
        self.min_day_volume = int(os.getenv("IPR_MIN_DAY_VOLUME", "150000"))
        self.min_dollar_volume = float(os.getenv("IPR_MIN_DOLLAR_VOLUME", "500000"))
        self.max_deep_symbols = max(50, int(os.getenv("IPR_MAX_DEEP_SYMBOLS", "1200")))
        self.float_keys = [item.strip() for item in os.getenv(
            "IPR_FLOAT_KEYS", "market_radar:float,elite_catalyst:float"
        ).split(",") if item.strip()]
        self.news_key = os.getenv("IPR_NEWS_KEY", "market_radar:news")
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", os.getenv("BOT_TOKEN", "")))
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", os.getenv("CHAT_ID", ""))
        self.model: QualityModel | None = None
        self.model_artifact: dict[str, Any] | None = None
        self.universe: list[str] = []
        self.asset_metadata: dict[str, dict[str, Any]] = {}
        self.hot_symbols: list[str] = []
        self.last_universe_refresh: datetime | None = None
        self.last_snapshot_refresh: datetime | None = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.record_lock = threading.RLock()
        self.monitor_thread: threading.Thread | None = None
        self.live_sample_thread: threading.Thread | None = None
        self.export_lock = threading.RLock()
        self.export_thread: threading.Thread | None = None
        self.export_path: str | None = None
        self.export_state: dict[str, Any] = {
            "status": "IDLE",
            "message": "Historical export has not started",
            "read_only": True,
            "updated_at": iso(),
        }
        self.audit_lock = threading.RLock()
        self.audit_thread: threading.Thread | None = None
        self.audit_stop_event = threading.Event()
        self.audit_path: str | None = None
        self.audit_state: dict[str, Any] = {
            "status": "IDLE",
            "message": "Historical confirmation audit has not started",
            "audit_id": HISTORICAL_CONFIRMATION_AUDIT_SPEC["audit_id"],
            "alerts_enabled": False,
            "orders_enabled": False,
            "updated_at": iso(),
        }
        self.early_lock = threading.RLock()
        self.early_thread: threading.Thread | None = None
        self.early_stop_event = threading.Event()
        self.early_path: str | None = None
        self.early_state: dict[str, Any] = {
            "status": "IDLE",
            "message": "Early causal entry research has not started",
            "research_id": EARLY_CAUSAL_ENTRY_SPEC["research_id"],
            "alerts_enabled": False,
            "orders_enabled": False,
            "updated_at": iso(),
        }
        self.state = {
            "status": "STARTING", "message": "Waiting for model bootstrap",
            "version": VERSION, "build": BUILD, "protocol_id": PROTOCOL_ID,
            "protocol_sha256": PROTOCOL_SHA256, "orders_enabled": False,
            "updated_at": iso(), "last_scan_at": None, "last_error": None,
            "scan_interval_seconds": self.scan_interval,
            "pending_interval_seconds": self.pending_interval,
            "live_sample_interval_seconds": self.live_sample_interval,
        }

    def key(self, suffix: str) -> str:
        return f"{self.prefix}:{suffix}"

    def audit_key(self, suffix: str) -> str:
        return self.key(f"historical_confirmation:v1:{suffix}")

    def early_key(self, suffix: str) -> str:
        return self.key(f"early_causal_entry:v1:{suffix}")

    def save_state(self, **updates: Any) -> None:
        with self.lock:
            self.state.update(updates)
            self.state["updated_at"] = iso()
            snapshot = dict(self.state)
        if self.redis.configured:
            try:
                self.redis.set_json(self.key("status"), snapshot)
            except Exception:
                logging.exception("Unable to persist service state")

    @staticmethod
    def _decode_redis_value(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value

    def _historical_export_patterns(self) -> tuple[str, ...]:
        return (
            f"{self.source_prefix}:manifest",
            f"{self.source_prefix}:results",
            f"{self.source_prefix}:results:*",
            f"{self.source_prefix}:pcprofit:v2:cases",
            f"{self.source_prefix}:pcprofit_er45:v1:cases",
            f"{self.source_prefix}:micro_features:raw_bars",
            f"{self.prefix}:quality_model",
            f"{self.prefix}:protocol_lock",
            f"{self.prefix}:samples",
            f"{self.prefix}:sample_index",
            f"{self.prefix}:live5s:*",
        )

    def _matching_historical_keys(self) -> list[str]:
        patterns = self._historical_export_patterns()
        cursor = "0"
        keys: set[str] = set()
        while True:
            result = self.redis.command("SCAN", cursor, "COUNT", 1000) or ["0", []]
            cursor = str(result[0])
            for raw_key in result[1] or []:
                key = str(raw_key)
                if any(fnmatch.fnmatchcase(key, pattern) for pattern in patterns):
                    keys.add(key)
            if cursor == "0":
                return sorted(keys)

    def _set_export_progress(self, **updates: Any) -> None:
        with self.export_lock:
            self.export_state.update(updates)
            self.export_state["updated_at"] = iso()

    def _write_export_record(self, output: Any, value: Any, first: bool) -> bool:
        if not first:
            output.write(",")
        output.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return False

    def _write_export_key(self, output: Any, key: str) -> int:
        kind = str(self.redis.command("TYPE", key) or "none")
        output.write("{")
        output.write(f'"key":{json.dumps(key)},"type":{json.dumps(kind)},"records":[')
        first = True
        count = 0
        if kind == "string":
            raw = self.redis.command("GET", key)
            if raw is not None:
                first = self._write_export_record(output, self._decode_redis_value(raw), first)
                count = 1
        elif kind == "hash":
            cursor = "0"
            while True:
                result = self.redis.command("HSCAN", key, cursor, "COUNT", 500) or ["0", []]
                cursor = str(result[0])
                values = result[1] or []
                for index in range(0, len(values) - 1, 2):
                    row = {
                        "field": str(values[index]),
                        "value": self._decode_redis_value(values[index + 1]),
                    }
                    first = self._write_export_record(output, row, first)
                    count += 1
                self._set_export_progress(current_key=key, current_key_records=count)
                if cursor == "0":
                    break
        elif kind == "list":
            length = int(self.redis.command("LLEN", key) or 0)
            for start in range(0, length, 500):
                for raw in self.redis.command("LRANGE", key, start, min(length - 1, start + 499)) or []:
                    first = self._write_export_record(output, self._decode_redis_value(raw), first)
                    count += 1
                self._set_export_progress(current_key=key, current_key_records=count)
        elif kind == "set":
            cursor = "0"
            while True:
                result = self.redis.command("SSCAN", key, cursor, "COUNT", 500) or ["0", []]
                cursor = str(result[0])
                for raw in result[1] or []:
                    first = self._write_export_record(output, self._decode_redis_value(raw), first)
                    count += 1
                self._set_export_progress(current_key=key, current_key_records=count)
                if cursor == "0":
                    break
        elif kind == "zset":
            cursor = "0"
            while True:
                result = self.redis.command("ZSCAN", key, cursor, "COUNT", 500) or ["0", []]
                cursor = str(result[0])
                values = result[1] or []
                for index in range(0, len(values) - 1, 2):
                    row = {
                        "value": self._decode_redis_value(values[index]),
                        "score": float(values[index + 1]),
                    }
                    first = self._write_export_record(output, row, first)
                    count += 1
                self._set_export_progress(current_key=key, current_key_records=count)
                if cursor == "0":
                    break
        output.write(f'],"record_count":{count}}}')
        return count

    def historical_export_loop(self) -> None:
        temporary_path: str | None = None
        try:
            keys = self._matching_historical_keys()
            self._set_export_progress(
                status="RUNNING",
                message="Exporting complete targeted historical data",
                total_keys=len(keys),
                completed_keys=0,
                total_records=0,
            )
            with tempfile.NamedTemporaryFile(
                prefix="independent_priority_history_",
                suffix=".json.gz",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
            total_records = 0
            with gzip.open(temporary_path, "wt", encoding="utf-8", compresslevel=6) as output:
                output.write("{")
                output.write(f'"generated_at":{json.dumps(iso())},')
                output.write('"read_only":true,"complete_values":true,')
                output.write(f'"patterns":{json.dumps(list(self._historical_export_patterns()))},"keys":[')
                for index, key in enumerate(keys):
                    if index:
                        output.write(",")
                    count = self._write_export_key(output, key)
                    total_records += count
                    self._set_export_progress(
                        completed_keys=index + 1,
                        total_records=total_records,
                    )
                output.write("]}")
            with self.export_lock:
                old_path = self.export_path
                self.export_path = temporary_path
            if old_path and old_path != temporary_path and os.path.isfile(old_path):
                try:
                    os.unlink(old_path)
                except OSError:
                    logging.warning("Unable to remove previous temporary export: %s", old_path)
            self._set_export_progress(
                status="COMPLETED",
                message="Historical export is ready to download",
                completed_keys=len(keys),
                total_records=total_records,
                compressed_bytes=os.path.getsize(temporary_path),
                download_ready=True,
                current_key=None,
            )
        except Exception as exc:
            logging.exception("Historical export failed")
            if temporary_path and temporary_path != self.export_path and os.path.isfile(temporary_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
            self._set_export_progress(
                status="ERROR",
                message=f"{type(exc).__name__}: {exc}",
                download_ready=False,
            )
        finally:
            with self.export_lock:
                self.export_thread = None

    def start_historical_export(self) -> tuple[bool, str]:
        moment = now_utc()
        if self._within_monitoring_hours(moment):
            return False, "Export is blocked during monitoring hours; retry after 17:30 New York time"
        if self.early_thread and self.early_thread.is_alive():
            return False, "Early Causal Entry Research is running"
        with self.export_lock:
            if self.export_thread and self.export_thread.is_alive():
                return True, "already_running"
            self.export_state = {
                "status": "STARTING",
                "message": "Preparing targeted historical export",
                "read_only": True,
                "download_ready": False,
                "updated_at": iso(),
            }
            self.export_thread = threading.Thread(
                target=self.historical_export_loop,
                name="independent-priority-history-export",
                daemon=True,
            )
            self.export_thread.start()
        return True, "started"

    def _set_audit_progress(self, **updates: Any) -> None:
        with self.audit_lock:
            self.audit_state.update(updates)
            self.audit_state["updated_at"] = iso()
            snapshot = dict(self.audit_state)
        if self.redis.configured:
            self.redis.set_json(self.audit_key("status"), snapshot)

    @staticmethod
    def _audit_folds(rows: list[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        sessions = sorted({row["session"] for row in rows})
        if len(sessions) < 12:
            raise RuntimeError("At least 12 development sessions are required for OOF audit")
        boundaries = [int(round(len(sessions) * ratio)) for ratio in (0.55, 0.70, 0.85, 1.0)]
        boundaries = [max(1, min(len(sessions), value)) for value in boundaries]
        folds = []
        for start, end in zip(boundaries[:-1], boundaries[1:]):
            train_sessions = set(sessions[:start])
            valid_sessions = set(sessions[start:end])
            train = [row for row in rows if row["session"] in train_sessions]
            valid = [row for row in rows if row["session"] in valid_sessions]
            if train and valid:
                folds.append((train, valid))
        if len(folds) != 3:
            raise RuntimeError("Unable to construct the frozen three chronological folds")
        return folds

    @staticmethod
    def _audit_matrix(rows: list[dict[str, Any]]) -> np.ndarray:
        return np.asarray([[float(row["features"][name]) for name in FEATURE_NAMES] for row in rows], dtype=float)

    @staticmethod
    def _audit_predict(fitted: dict[str, Any], rows: list[dict[str, Any]]) -> np.ndarray:
        X = IndependentPriorityRadar._audit_matrix(rows)
        Z = (X - fitted["mean"]) / fitted["scale"]
        return 1.0 / (1.0 + np.exp(-np.clip(fitted["beta"][0] + Z @ fitted["beta"][1:], -35, 35)))

    def _historical_audit_rows(self) -> list[dict[str, Any]]:
        """Join immutable stored signal/features; no market data is fetched here."""
        manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {})
        development_sessions = set(manifest.get("development_sessions") or [])
        holdout_sessions = set(manifest.get("holdout_sessions") or [])
        if not development_sessions or not holdout_sessions:
            raise RuntimeError("Historical development/holdout session manifest is incomplete")
        pc_rows = dict(self.redis.scan_hash_json(f"{self.source_prefix}:pcprofit:v2:cases"))
        er_rows = dict(self.redis.scan_hash_json(f"{self.source_prefix}:pcprofit_er45:v1:cases"))
        common = set(pc_rows) & set(er_rows)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        result_keys = [f"{self.source_prefix}:results"] + [
            f"{self.source_prefix}:results:{session}" for session in manifest.get("sessions", [])
        ]
        for result_key in result_keys:
            for _, result in self.redis.scan_hash_json(result_key):
                signal = result.get("breakout_ready")
                if result.get("mode") != "approx" or not signal or signal.get("phase") != "REGULAR":
                    continue
                case_id = f"{result.get('session')}|{result.get('symbol')}|{signal.get('ts')}"
                if case_id in seen or case_id not in common:
                    continue
                pc = pc_rows[case_id]
                er = er_rows[case_id]
                if not pc.get("has_plan") or not er.get("has_plan"):
                    continue
                required = (
                    pc.get("price_change_pct_last45m"), er.get("er45"), pc.get("recomputed_mfe_pct"),
                    signal.get("price"), signal.get("opportunity"), signal.get("failure_pressure"),
                    signal.get("ts"), signal.get("resistance"),
                )
                if any(value is None for value in required) or float(signal["price"]) <= 0:
                    continue
                session = str(result.get("session"))
                partition = "development" if session in development_sessions else "holdout" if session in holdout_sessions else None
                if partition is None:
                    continue
                stamp = parse_dt(str(signal["ts"])).astimezone(NY)
                change = float(pc["price_change_pct_last45m"])
                efficiency = float(er["er45"])
                rows.append({
                    "case_id": case_id,
                    "session": session,
                    "partition": partition,
                    "symbol": str(result.get("symbol")),
                    "signal_ts": str(signal["ts"]),
                    "signal_price": float(signal["price"]),
                    "frozen_resistance": float(signal["resistance"]),
                    "stored_recomputed_mfe_pct": float(pc["recomputed_mfe_pct"]),
                    "stored_recomputed_mae_pct": float(pc["recomputed_mae_pct"]) if pc.get("recomputed_mae_pct") is not None else None,
                    "features": {
                        "price_change_pct_last45m": change,
                        "er45": efficiency,
                        "price_change_x_er45": change * efficiency,
                        "log_signal_price": math.log(float(signal["price"])),
                        "opportunity": float(signal["opportunity"]),
                        "failure_pressure": float(signal["failure_pressure"]),
                        "minutes_since_regular_open": float(stamp.hour * 60 + stamp.minute - 570),
                    },
                    "explosion_ge10": float(pc["recomputed_mfe_pct"]) >= 10.0,
                })
                seen.add(case_id)
        rows.sort(key=lambda row: (row["session"], row["signal_ts"], row["symbol"]))
        return rows

    def _historical_audit_candidates(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows = self._historical_audit_rows()
        development = [row for row in rows if row["partition"] == "development"]
        holdout = [row for row in rows if row["partition"] == "holdout"]
        if len(development) != 16894 or sum(row["explosion_ge10"] for row in development) != 160:
            raise RuntimeError(
                "Frozen development join mismatch: "
                f"rows={len(development)}, positives={sum(row['explosion_ge10'] for row in development)}"
            )

        selected: list[dict[str, Any]] = []
        fold_report = []
        for fold_index, (train, valid) in enumerate(self._audit_folds(development), 1):
            fitted = fit_logistic(
                self._audit_matrix(train),
                np.asarray([float(row["explosion_ge10"]) for row in train], dtype=float),
                l2=1.0,
            )
            probabilities = self._audit_predict(fitted, valid)
            cutoff = float(np.quantile(probabilities, 0.95))
            chosen = []
            for row, probability in zip(valid, probabilities.tolist()):
                if probability >= cutoff:
                    item = dict(row)
                    item.update({"selection": "development_oof_top5", "fold": fold_index, "probability": float(probability), "fold_cutoff": cutoff})
                    chosen.append(item)
            selected.extend(chosen)
            fold_report.append({
                "fold": fold_index,
                "train_first_session": train[0]["session"],
                "train_last_session": train[-1]["session"],
                "validation_first_session": valid[0]["session"],
                "validation_last_session": valid[-1]["session"],
                "train_rows": len(train),
                "validation_rows": len(valid),
                "probability_cutoff": cutoff,
                "selected_rows": len(chosen),
            })

        artifact = self.model_artifact or self.redis.get_json(self.key("quality_model"), None)
        if not artifact or artifact.get("protocol_sha256") != PROTOCOL_SHA256:
            artifact = self._fit_artifact(development)
        frozen_model = QualityModel(artifact)
        for row in holdout:
            probability = frozen_model.probability(row["features"])
            if probability >= frozen_model.cutoff:
                item = dict(row)
                item.update({"selection": "legacy_holdout_frozen_cutoff", "fold": None, "probability": probability, "fold_cutoff": frozen_model.cutoff})
                selected.append(item)
        selected.sort(key=lambda row: (row["session"], row["signal_ts"], row["symbol"]))
        context = {
            "joined_rows": len(rows),
            "development_rows": len(development),
            "development_positive_count": int(sum(row["explosion_ge10"] for row in development)),
            "legacy_holdout_rows": len(holdout),
            "development_oof_candidates": sum(row["partition"] == "development" for row in selected),
            "legacy_holdout_candidates": sum(row["partition"] == "holdout" for row in selected),
            "folds": fold_report,
            "frozen_holdout_cutoff": frozen_model.cutoff,
        }
        return selected, context

    @staticmethod
    def _historical_candidate_result(candidate: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
        signal_time = parse_dt(candidate["signal_ts"])
        eligible = [
            bar for bar in sorted(bars, key=lambda item: item["t"])
            if signal_time <= parse_dt(bar["t"]) <= signal_time + timedelta(minutes=15)
        ]
        record = dict(candidate)
        record["market_data"] = {
            "source": "Alpaca SIP one-minute raw bars",
            "bars_received": len(bars),
            "confirmation_window_bars": len(eligible),
        }
        if not eligible:
            record.update({
                "coverage": "MISSING_BARS",
                "confirmation": {"status": "MISSING_BARS", "confirmed": False},
                "candidate_outcome_60m": outcome_metrics(bars, signal_time, float(candidate["signal_price"]), 60),
                "candidate_path_60m": stop_target_path_metrics(bars, signal_time, float(candidate["signal_price"]), 60),
                "confirmation_outcome_60m": None,
                "confirmation_path_60m": None,
            })
            return record

        confirmation = None
        for bar in eligible:
            metrics = confirmation_metrics(bar, float(candidate["frozen_resistance"]))
            if metrics["confirmed"]:
                confirmation = metrics
                break
        if confirmation is None:
            record.update({
                "coverage": "AVAILABLE",
                "confirmation": {"status": "UNCONFIRMED", "confirmed": False, "bars_evaluated": len(eligible)},
                "candidate_outcome_60m": outcome_metrics(bars, signal_time, float(candidate["signal_price"]), 60),
                "candidate_path_60m": stop_target_path_metrics(bars, signal_time, float(candidate["signal_price"]), 60),
                "confirmation_outcome_60m": None,
                "confirmation_path_60m": None,
            })
            return record

        confirmation_time = parse_dt(confirmation["bar_ts"])
        entry_price = float(confirmation["close"])
        confirmation["status"] = "CONFIRMED"
        confirmation["entry_price"] = entry_price
        record.update({
            "coverage": "AVAILABLE",
            "confirmation": confirmation,
            "candidate_outcome_60m": outcome_metrics(bars, signal_time, float(candidate["signal_price"]), 60),
            "candidate_path_60m": stop_target_path_metrics(bars, signal_time, float(candidate["signal_price"]), 60),
            "confirmation_outcome_60m": outcome_metrics(bars, confirmation_time, entry_price, 60),
            "confirmation_path_60m": stop_target_path_metrics(bars, confirmation_time, entry_price, 60),
        })
        return record

    @staticmethod
    def _summarize_audit_group(records: list[dict[str, Any]]) -> dict[str, Any]:
        available = [row for row in records if row.get("coverage") == "AVAILABLE"]
        confirmed = [row for row in available if (row.get("confirmation") or {}).get("confirmed")]

        def outcome_summary(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
            outcomes = [row.get(field) for row in rows if (row.get(field) or {}).get("forward_bars")]
            return {
                "count": len(outcomes),
                "complete_count": sum(bool(item.get("complete")) for item in outcomes),
                "mfe_median_pct": round(float(median(item["mfe_pct"] for item in outcomes)), 5) if outcomes else None,
                "mae_median_pct": round(float(median(item["mae_pct"] for item in outcomes)), 5) if outcomes else None,
                "reached_2pct": sum(bool(item.get("reached_2pct")) for item in outcomes),
                "reached_5pct": sum(bool(item.get("reached_5pct")) for item in outcomes),
                "reached_10pct": sum(bool(item.get("reached_10pct")) for item in outcomes),
            }

        pair_counts: dict[str, dict[str, int]] = {}
        for row in confirmed:
            for pair, detail in ((row.get("confirmation_path_60m") or {}).get("pairs") or {}).items():
                counts = pair_counts.setdefault(pair, {"TARGET_FIRST": 0, "STOP_FIRST": 0, "AMBIGUOUS": 0, "NEITHER": 0})
                counts[str(detail.get("order") or "NEITHER")] += 1
        return {
            "selected_candidates": len(records),
            "available_bars": len(available),
            "missing_bars": len(records) - len(available),
            "confirmed": len(confirmed),
            "unconfirmed": len(available) - len(confirmed),
            "confirmation_rate_pct": round(len(confirmed) / len(available) * 100.0, 4) if available else None,
            "candidate_entry_outcomes": outcome_summary(available, "candidate_outcome_60m"),
            "confirmed_entry_outcomes": outcome_summary(confirmed, "confirmation_outcome_60m"),
            "confirmed_entry_stop_target_order": pair_counts,
        }

    def _build_historical_audit_report(self, context: dict[str, Any]) -> dict[str, Any]:
        records = [value for _, value in self.redis.scan_hash_json(self.audit_key("cases"))]
        records.sort(key=lambda row: (row["session"], row["signal_ts"], row["symbol"]))
        development = [row for row in records if row["partition"] == "development"]
        holdout = [row for row in records if row["partition"] == "holdout"]
        return {
            "schema": 1,
            "generated_at": iso(),
            "version": VERSION,
            "build": BUILD,
            "protocol_id": PROTOCOL_ID,
            "protocol_sha256": PROTOCOL_SHA256,
            "audit_spec": HISTORICAL_CONFIRMATION_AUDIT_SPEC,
            "selection_context": context,
            "development_oof": self._summarize_audit_group(development),
            "legacy_holdout_historical_audit_only": self._summarize_audit_group(holdout),
            "safety": {
                "alerts_enabled": False,
                "orders_enabled": False,
                "live_model_or_cutoff_changed": False,
                "note": "Historical audit is diagnostic and cannot approve live trading.",
            },
        }

    def _materialize_historical_audit_download(self, report: dict[str, Any] | None = None) -> str:
        report = report or self.redis.get_json(self.audit_key("report"), None)
        if not report:
            raise RuntimeError("Historical confirmation report is not ready")
        records = [value for _, value in self.redis.scan_hash_json(self.audit_key("cases"))]
        with tempfile.NamedTemporaryFile(prefix="ipr_historical_confirmation_", suffix=".json.gz", delete=False) as temporary:
            path = temporary.name
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as output:
            json.dump({"report": report, "cases": records}, output, ensure_ascii=False, separators=(",", ":"))
        with self.audit_lock:
            old_path = self.audit_path
            self.audit_path = path
        if old_path and old_path != path and os.path.isfile(old_path):
            try:
                os.unlink(old_path)
            except OSError:
                pass
        return path

    def historical_confirmation_audit_loop(self) -> None:
        try:
            candidates, context = self._historical_audit_candidates()
            self.redis.set_json(self.audit_key("selection_context"), context)
            completed = set(self.redis.command("SMEMBERS", self.audit_key("completed")) or [])
            self._set_audit_progress(
                status="RUNNING",
                message="Fetching causal historical minute bars from Alpaca",
                total_candidates=len(candidates),
                completed_candidates=len(completed),
                selection_context=context,
            )
            by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for candidate in candidates:
                if candidate["case_id"] not in completed:
                    by_session[candidate["session"]].append(candidate)

            for session_index, (session, session_candidates) in enumerate(sorted(by_session.items()), 1):
                if self.audit_stop_event.is_set():
                    self._set_audit_progress(status="PAUSED", message="Paused safely; press start to resume")
                    return
                symbols = sorted({row["symbol"] for row in session_candidates})
                start = min(parse_dt(row["signal_ts"]) for row in session_candidates) - timedelta(minutes=1)
                end = max(parse_dt(row["signal_ts"]) for row in session_candidates) + timedelta(minutes=77)
                bars_by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
                for symbol_batch in chunks(symbols, 100):
                    fetched = self.alpaca.bars(symbol_batch, start, end, feed="sip", adjustment="raw")
                    for symbol, bars in fetched.items():
                        bars_by_symbol.setdefault(symbol, []).extend(bars)
                for candidate in session_candidates:
                    result = self._historical_candidate_result(candidate, bars_by_symbol.get(candidate["symbol"], []))
                    self.redis.hset_json(self.audit_key("cases"), candidate["case_id"], result)
                    self.redis.command("SADD", self.audit_key("completed"), candidate["case_id"])
                    completed.add(candidate["case_id"])
                self._set_audit_progress(
                    status="RUNNING",
                    message=f"Completed historical session {session}",
                    completed_candidates=len(completed),
                    completed_sessions=session_index,
                    remaining_sessions=len(by_session) - session_index,
                )

            report = self._build_historical_audit_report(context)
            self.redis.set_json(self.audit_key("report"), report)
            path = self._materialize_historical_audit_download(report)
            self._set_audit_progress(
                status="COMPLETED",
                message="Historical confirmation audit is complete",
                completed_candidates=len(candidates),
                total_candidates=len(candidates),
                result_ready=True,
                download_ready=True,
                compressed_bytes=os.path.getsize(path),
            )
        except Exception as exc:
            logging.exception("Historical confirmation audit failed")
            self._set_audit_progress(status="ERROR", message=f"{type(exc).__name__}: {exc}", result_ready=False)
        finally:
            with self.audit_lock:
                self.audit_thread = None

    def start_historical_confirmation_audit(self) -> tuple[bool, str]:
        if self._within_monitoring_hours(now_utc()):
            return False, "Audit is blocked during monitoring hours; retry after 17:30 New York time"
        if not self.redis.configured or not self.alpaca.configured:
            return False, "Redis and Alpaca credentials are required"
        if self.early_thread and self.early_thread.is_alive():
            return False, "Early Causal Entry Research is running"
        with self.audit_lock:
            if self.audit_thread and self.audit_thread.is_alive():
                return True, "already_running"
            self.audit_stop_event.clear()
            stored = self.redis.get_json(self.audit_key("status"), None)
            if stored and stored.get("status") == "COMPLETED":
                self.audit_state = stored
                return False, "already_completed"
            self.audit_state = {
                "status": "STARTING",
                "message": "Joining frozen historical cases and preparing OOF selection",
                "audit_id": HISTORICAL_CONFIRMATION_AUDIT_SPEC["audit_id"],
                "alerts_enabled": False,
                "orders_enabled": False,
                "result_ready": False,
                "updated_at": iso(),
            }
            self.audit_thread = threading.Thread(
                target=self.historical_confirmation_audit_loop,
                name="independent-priority-historical-confirmation-audit",
                daemon=True,
            )
            self.audit_thread.start()
        return True, "started"

    def pause_historical_confirmation_audit(self) -> tuple[bool, str]:
        with self.audit_lock:
            if not self.audit_thread or not self.audit_thread.is_alive():
                return False, "not_running"
            self.audit_stop_event.set()
        return True, "pause_requested"

    def _set_early_progress(self, **updates: Any) -> None:
        with self.early_lock:
            self.early_state.update(updates)
            self.early_state["updated_at"] = iso()
            snapshot = dict(self.early_state)
        if self.redis.configured:
            self.redis.set_json(self.early_key("status"), snapshot)

    @staticmethod
    def _early_temporal_folds(records: list[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        ordered = sorted(records, key=lambda row: (row["session"], row["signal_ts"], row["symbol"]))
        sessions = sorted({row["session"] for row in ordered})
        if len(sessions) < 10:
            raise RuntimeError("At least 10 Development sessions are required for early-entry OOF research")
        initial = max(4, int(round(len(sessions) * 0.40)))
        remaining = len(sessions) - initial
        if remaining < 6:
            raise RuntimeError("Insufficient later Development sessions for three validation folds")
        cuts = [initial + int(round(remaining * ratio)) for ratio in (0.0, 1 / 3, 2 / 3, 1.0)]
        cuts[0] = initial
        cuts[-1] = len(sessions)
        folds = []
        for start, end in zip(cuts[:-1], cuts[1:]):
            if end <= start:
                continue
            train_sessions = set(sessions[:start])
            valid_sessions = set(sessions[start:end])
            train = [row for row in ordered if row["session"] in train_sessions]
            valid = [row for row in ordered if row["session"] in valid_sessions]
            if train and valid:
                folds.append((train, valid))
        if len(folds) != 3:
            raise RuntimeError("Unable to create three expanding early-entry validation folds")
        return folds

    @staticmethod
    def _early_feature_screen(records: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(records, key=lambda row: (row["session"], row["signal_ts"], row["symbol"]))
        sessions = sorted({row["session"] for row in ordered})
        session_blocks = [list(block) for block in np.array_split(np.asarray(sessions, dtype=object), 3) if len(block)]
        feature_report: dict[str, Any] = {}
        stable: list[tuple[str, float]] = []
        for name in EARLY_CAUSAL_FEATURE_NAMES:
            effects = []
            blocks = []
            for session_block in session_blocks:
                allowed = set(str(value) for value in session_block)
                rows = [row for row in ordered if row["session"] in allowed]
                positive = [float(row["early_features"][name]) for row in rows if row["primary_profitable"]]
                negative = [float(row["early_features"][name]) for row in rows if not row["primary_profitable"]]
                all_values = positive + negative
                scale = float(np.std(np.asarray(all_values, dtype=float))) if all_values else 0.0
                effect = (float(mean(positive)) - float(mean(negative))) / scale if positive and negative and scale > 1e-12 else 0.0
                effects.append(effect)
                blocks.append({
                    "first_session": min(allowed),
                    "last_session": max(allowed),
                    "profitable": len(positive),
                    "not_profitable": len(negative),
                    "standardized_mean_difference": round(effect, 6),
                })
            pooled_positive = [float(row["early_features"][name]) for row in ordered if row["primary_profitable"]]
            pooled_negative = [float(row["early_features"][name]) for row in ordered if not row["primary_profitable"]]
            pooled_values = pooled_positive + pooled_negative
            pooled_scale = float(np.std(np.asarray(pooled_values, dtype=float))) if pooled_values else 0.0
            pooled_effect = (
                (float(mean(pooled_positive)) - float(mean(pooled_negative))) / pooled_scale
                if pooled_positive and pooled_negative and pooled_scale > 1e-12 else 0.0
            )
            nonzero = all(abs(value) > 1e-12 for value in effects)
            same_direction = nonzero and (all(value > 0 for value in effects) or all(value < 0 for value in effects))
            median_abs_effect = float(median(abs(value) for value in effects)) if effects else 0.0
            is_stable = same_direction and median_abs_effect >= 0.10
            feature_report[name] = {
                "blocks": blocks,
                "pooled_standardized_mean_difference": round(pooled_effect, 6),
                "median_absolute_block_effect": round(median_abs_effect, 6),
                "same_direction_all_blocks": same_direction,
                "stable": is_stable,
            }
            if is_stable:
                stable.append((name, abs(pooled_effect)))
        stable.sort(key=lambda item: (-item[1], item[0]))
        return {
            "features": feature_report,
            "stable_features": [name for name, _ in stable[:6]],
            "stable_feature_count_before_cap": len(stable),
            "maximum_features": 6,
        }

    @staticmethod
    def _fit_early_model(records: list[dict[str, Any]], feature_names: list[str]) -> dict[str, Any]:
        if not feature_names:
            raise RuntimeError("No stable early features are available")
        X = np.asarray([[float(row["early_features"][name]) for name in feature_names] for row in records], dtype=float)
        y = np.asarray([1.0 if row["primary_profitable"] else 0.0 for row in records], dtype=float)
        if len(set(y.tolist())) < 2:
            raise RuntimeError("Early-entry training data contains only one class")
        fitted = fit_logistic(X, y, l2=1.0)
        Z = (X - fitted["mean"]) / fitted["scale"]
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(fitted["beta"][0] + Z @ fitted["beta"][1:], -35, 35)))
        return {
            "feature_names": list(feature_names),
            "mean": fitted["mean"],
            "scale": fitted["scale"],
            "beta": fitted["beta"],
            "threshold": float(np.median(probabilities)),
            "converged": bool(fitted["converged"]),
            "iterations": int(fitted["iterations"]),
        }

    @staticmethod
    def _predict_early_model(model: dict[str, Any], records: list[dict[str, Any]]) -> np.ndarray:
        names = model["feature_names"]
        X = np.asarray([[float(row["early_features"][name]) for name in names] for row in records], dtype=float)
        Z = (X - model["mean"]) / model["scale"]
        return 1.0 / (1.0 + np.exp(-np.clip(model["beta"][0] + Z @ model["beta"][1:], -35, 35)))

    @staticmethod
    def _serializable_early_model(model: dict[str, Any]) -> dict[str, Any]:
        return {
            "feature_names": list(model["feature_names"]),
            "standardization_mean": [float(value) for value in model["mean"]],
            "standardization_scale": [float(value) for value in model["scale"]],
            "intercept_and_standardized_coefficients": [float(value) for value in model["beta"]],
            "selection_threshold": float(model["threshold"]),
            "threshold_rule": "median training probability; no threshold search",
            "l2_penalty": 1.0,
            "converged": bool(model["converged"]),
            "iterations": int(model["iterations"]),
        }

    @staticmethod
    def _early_case_result(candidate: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
        record = dict(candidate)
        calculated = early_causal_features(bars, candidate)
        signal_time = parse_dt(candidate["signal_ts"])
        outcome = outcome_metrics(bars, signal_time, float(candidate["signal_price"]), 60)
        path = stop_target_path_metrics(bars, signal_time, float(candidate["signal_price"]), 60)
        confirmation_benchmark = IndependentPriorityRadar._historical_candidate_result(candidate, bars)
        record["candidate_outcome_60m"] = outcome
        record["candidate_path_60m"] = path
        record["current_confirmation_benchmark"] = {
            "confirmation": confirmation_benchmark.get("confirmation"),
            "outcome_60m": confirmation_benchmark.get("confirmation_outcome_60m"),
            "path_60m": confirmation_benchmark.get("confirmation_path_60m"),
        }
        if calculated is None:
            record.update({"coverage": "MISSING_CAUSAL_BARS", "early_features": None})
            return record
        features, diagnostics = calculated
        record.update({
            "coverage": "AVAILABLE" if outcome.get("forward_bars") else "MISSING_FORWARD_BARS",
            "early_features": features,
            "early_diagnostics": diagnostics,
        })
        if outcome.get("complete"):
            net_return = float(outcome["close_return_pct"]) - 0.25
            record["net_time_exit_return_pct"] = round(net_return, 6)
            record["primary_profitable"] = net_return > 0
            record["diagnostic_policy_returns"] = {
                "stop_4_target_2": exact_policy_return(path, outcome, 4.0, 2.0, 0.25),
                "stop_5_target_2": exact_policy_return(path, outcome, 5.0, 2.0, 0.25),
            }
        else:
            record.update({
                "net_time_exit_return_pct": None,
                "primary_profitable": None,
                "diagnostic_policy_returns": {},
            })
        return record

    @staticmethod
    def _early_policy_summaries(records: list[dict[str, Any]]) -> dict[str, Any]:
        complete = [row for row in records if row.get("primary_profitable") is not None]
        return {
            "primary_time_exit_after_cost": return_statistics([row["net_time_exit_return_pct"] for row in complete]),
            "diagnostic_stop_4_target_2": return_statistics([
                row["diagnostic_policy_returns"]["stop_4_target_2"] for row in complete
            ]),
            "diagnostic_stop_5_target_2": return_statistics([
                row["diagnostic_policy_returns"]["stop_5_target_2"] for row in complete
            ]),
        }

    @staticmethod
    def _current_confirmation_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
        confirmed = [
            row for row in records
            if ((row.get("current_confirmation_benchmark") or {}).get("confirmation") or {}).get("confirmed")
        ]
        exact = [
            row for row in confirmed
            if (((row.get("current_confirmation_benchmark") or {}).get("outcome_60m") or {}).get("complete"))
        ]
        time_returns = [
            float(row["current_confirmation_benchmark"]["outcome_60m"]["close_return_pct"]) - 0.25
            for row in exact
        ]
        stop4 = [
            exact_policy_return(
                row["current_confirmation_benchmark"]["path_60m"],
                row["current_confirmation_benchmark"]["outcome_60m"],
                4.0, 2.0, 0.25,
            )
            for row in exact
        ]
        stop5 = [
            exact_policy_return(
                row["current_confirmation_benchmark"]["path_60m"],
                row["current_confirmation_benchmark"]["outcome_60m"],
                5.0, 2.0, 0.25,
            )
            for row in exact
        ]
        return {
            "candidate_count": len(records),
            "confirmed_count": len(confirmed),
            "confirmation_rate_pct": round(len(confirmed) / len(records) * 100.0, 4) if records else None,
            "complete_confirmation_outcomes": len(exact),
            "primary_time_exit_after_cost": return_statistics(time_returns),
            "diagnostic_stop_4_target_2": return_statistics(stop4),
            "diagnostic_stop_5_target_2": return_statistics(stop5),
        }

    def _evaluate_early_development(self, development: list[dict[str, Any]]) -> dict[str, Any]:
        complete = [row for row in development if row.get("primary_profitable") is not None]
        outer_reports = []
        selected_oof: list[dict[str, Any]] = []
        validation_oof: list[dict[str, Any]] = []
        for fold_index, (train, valid) in enumerate(self._early_temporal_folds(complete), 1):
            screen = self._early_feature_screen(train)
            feature_names = screen["stable_features"]
            fold: dict[str, Any] = {
                "fold": fold_index,
                "train_first_session": train[0]["session"],
                "train_last_session": train[-1]["session"],
                "validation_first_session": valid[0]["session"],
                "validation_last_session": valid[-1]["session"],
                "train_count": len(train),
                "validation_count": len(valid),
                "training_only_stable_features": feature_names,
                "baseline": return_statistics([row["net_time_exit_return_pct"] for row in valid]),
                "current_confirmation_benchmark": self._current_confirmation_summary(valid),
            }
            validation_oof.extend(valid)
            if not feature_names:
                fold.update({"model_built": False, "selected_count": 0, "selected": return_statistics([])})
                outer_reports.append(fold)
                continue
            model = self._fit_early_model(train, feature_names)
            probabilities = self._predict_early_model(model, valid)
            selected = [row for row, probability in zip(valid, probabilities.tolist()) if probability >= model["threshold"]]
            selected_oof.extend(selected)
            fold.update({
                "model_built": True,
                "training_probability_threshold": round(float(model["threshold"]), 8),
                "selected_count": len(selected),
                "selection_rate_pct": round(len(selected) / len(valid) * 100.0, 4) if valid else None,
                "selected": return_statistics([row["net_time_exit_return_pct"] for row in selected]),
            })
            outer_reports.append(fold)

        pooled_baseline = return_statistics([row["net_time_exit_return_pct"] for row in validation_oof])
        pooled_selected = return_statistics([row["net_time_exit_return_pct"] for row in selected_oof])
        all_folds_profitable = bool(outer_reports) and all(
            fold.get("model_built")
            and fold["selected"].get("profit_factor") is not None
            and float(fold["selected"]["profit_factor"]) > 1.0
            and float(fold["selected"]["average_return_pct"]) > 0.0
            for fold in outer_reports
        )
        full_screen = self._early_feature_screen(complete)
        final_model = None
        if full_screen["stable_features"]:
            final_model = self._fit_early_model(complete, full_screen["stable_features"])
        judgment = "PROMISING" if all_folds_profitable else "NO_STABLE_SIGNAL"
        return {
            "complete_development_count": len(complete),
            "incomplete_development_count": len(development) - len(complete),
            "full_development_descriptive_screen": full_screen,
            "outer_folds": outer_reports,
            "pooled_outer_validation_baseline": pooled_baseline,
            "pooled_outer_validation_selected": pooled_selected,
            "pooled_outer_validation_current_confirmation": self._current_confirmation_summary(validation_oof),
            "all_three_outer_folds_profitable_after_cost": all_folds_profitable,
            "judgment": judgment,
            "final_shadow_model": self._serializable_early_model(final_model) if final_model else None,
            "_runtime_model": final_model,
        }

    def _build_early_report(self, context: dict[str, Any]) -> dict[str, Any]:
        records = [value for _, value in self.redis.scan_hash_json(self.early_key("cases"))]
        records.sort(key=lambda row: (row["session"], row["signal_ts"], row["symbol"]))
        development = [row for row in records if row["partition"] == "development"]
        holdout = [row for row in records if row["partition"] == "holdout"]
        evaluation = self._evaluate_early_development(development)
        runtime_model = evaluation.pop("_runtime_model")
        complete_holdout = [row for row in holdout if row.get("primary_profitable") is not None]
        holdout_selected: list[dict[str, Any]] = []
        if runtime_model and complete_holdout:
            probabilities = self._predict_early_model(runtime_model, complete_holdout)
            holdout_selected = [
                row for row, probability in zip(complete_holdout, probabilities.tolist())
                if probability >= runtime_model["threshold"]
            ]
        return {
            "schema": 1,
            "generated_at": iso(),
            "version": VERSION,
            "build": BUILD,
            "protocol_id": PROTOCOL_ID,
            "protocol_sha256": PROTOCOL_SHA256,
            "research_spec": EARLY_CAUSAL_ENTRY_SPEC,
            "selection_context": context,
            "coverage": {
                "total_cases": len(records),
                "development_cases": len(development),
                "legacy_holdout_cases": len(holdout),
                "available_early_features": sum(row.get("early_features") is not None for row in records),
                "complete_primary_outcomes": sum(row.get("primary_profitable") is not None for row in records),
            },
            "development_only_research": {
                "all_candidate_policies": self._early_policy_summaries(development),
                "current_confirmation_benchmark": self._current_confirmation_summary(development),
                "causal_model_evaluation": evaluation,
            },
            "legacy_holdout_audit_only": {
                "can_approve_live": False,
                "all_candidate_policies": self._early_policy_summaries(holdout),
                "current_confirmation_benchmark": self._current_confirmation_summary(holdout),
                "shadow_model_selected_count": len(holdout_selected),
                "shadow_model_selected": return_statistics([
                    row["net_time_exit_return_pct"] for row in holdout_selected
                ]),
                "note": "Previously inspected Legacy Holdout is descriptive only and cannot approve this model.",
            },
            "final_judgment": evaluation["judgment"],
            "safety": {
                "alerts_enabled": False,
                "orders_enabled": False,
                "live_model_or_cutoff_changed": False,
                "live_confirmation_changed": False,
                "deployment_approved": False,
            },
        }

    def _materialize_early_download(self, report: dict[str, Any] | None = None) -> str:
        report = report or self.redis.get_json(self.early_key("report"), None)
        if not report:
            raise RuntimeError("Early causal entry report is not ready")
        records = [value for _, value in self.redis.scan_hash_json(self.early_key("cases"))]
        with tempfile.NamedTemporaryFile(prefix="ipr_early_causal_entry_", suffix=".json.gz", delete=False) as temporary:
            path = temporary.name
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as output:
            json.dump({"report": report, "cases": records}, output, ensure_ascii=False, separators=(",", ":"))
        with self.early_lock:
            old_path = self.early_path
            self.early_path = path
        if old_path and old_path != path and os.path.isfile(old_path):
            try:
                os.unlink(old_path)
            except OSError:
                pass
        return path

    def early_causal_entry_loop(self) -> None:
        try:
            candidates, context = self._historical_audit_candidates()
            expected = (
                EARLY_CAUSAL_ENTRY_SPEC["candidate_count_expected"],
                EARLY_CAUSAL_ENTRY_SPEC["development_candidates_expected"],
                EARLY_CAUSAL_ENTRY_SPEC["legacy_holdout_candidates_expected"],
            )
            actual = (
                len(candidates),
                sum(row["partition"] == "development" for row in candidates),
                sum(row["partition"] == "holdout" for row in candidates),
            )
            if actual != expected:
                raise RuntimeError(f"Frozen early-entry candidate mismatch: expected={expected}, actual={actual}")
            self.redis.set_json(self.early_key("selection_context"), context)
            completed = set(self.redis.command("SMEMBERS", self.early_key("completed")) or [])
            self._set_early_progress(
                status="RUNNING",
                message="Fetching causal discovery and forward minute bars from Alpaca",
                total_candidates=len(candidates),
                completed_candidates=len(completed),
                selection_context=context,
            )
            by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for candidate in candidates:
                if candidate["case_id"] not in completed:
                    by_session[candidate["session"]].append(candidate)
            for session_index, (session, session_candidates) in enumerate(sorted(by_session.items()), 1):
                if self.early_stop_event.is_set():
                    self._set_early_progress(status="PAUSED", message="Paused safely; press start to resume")
                    return
                symbols = sorted({row["symbol"] for row in session_candidates})
                local_day = parse_dt(session_candidates[0]["signal_ts"]).astimezone(NY).date()
                start = datetime.combine(local_day, dtime(9, 30), tzinfo=NY).astimezone(UTC)
                end = max(parse_dt(row["signal_ts"]) for row in session_candidates) + timedelta(minutes=77)
                bars_by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
                for symbol_batch in chunks(symbols, 100):
                    fetched = self.alpaca.bars(symbol_batch, start, end, feed="sip", adjustment="raw")
                    for symbol, bars in fetched.items():
                        bars_by_symbol.setdefault(symbol, []).extend(bars)
                for candidate in session_candidates:
                    result = self._early_case_result(candidate, bars_by_symbol.get(candidate["symbol"], []))
                    self.redis.hset_json(self.early_key("cases"), candidate["case_id"], result)
                    self.redis.command("SADD", self.early_key("completed"), candidate["case_id"])
                    completed.add(candidate["case_id"])
                self._set_early_progress(
                    status="RUNNING",
                    message=f"Completed early-entry historical session {session}",
                    completed_candidates=len(completed),
                    completed_sessions=session_index,
                    remaining_sessions=len(by_session) - session_index,
                )
            report = self._build_early_report(context)
            self.redis.set_json(self.early_key("report"), report)
            path = self._materialize_early_download(report)
            self._set_early_progress(
                status="COMPLETED",
                message="Early Causal Entry Research is complete",
                completed_candidates=len(candidates),
                total_candidates=len(candidates),
                result_ready=True,
                download_ready=True,
                final_judgment=report["final_judgment"],
                compressed_bytes=os.path.getsize(path),
            )
        except Exception as exc:
            logging.exception("Early causal entry research failed")
            self._set_early_progress(status="ERROR", message=f"{type(exc).__name__}: {exc}", result_ready=False)
        finally:
            with self.early_lock:
                self.early_thread = None

    def start_early_causal_entry(self) -> tuple[bool, str]:
        if self._within_monitoring_hours(now_utc()):
            return False, "Research is blocked during monitoring hours; retry after 17:30 New York time"
        if not self.redis.configured or not self.alpaca.configured:
            return False, "Redis and Alpaca credentials are required"
        with self.early_lock:
            if self.early_thread and self.early_thread.is_alive():
                return True, "already_running"
            if (self.audit_thread and self.audit_thread.is_alive()) or (self.export_thread and self.export_thread.is_alive()):
                return False, "another historical job is running"
            self.early_stop_event.clear()
            stored = self.redis.get_json(self.early_key("status"), None)
            if stored and stored.get("status") == "COMPLETED":
                self.early_state = stored
                return False, "already_completed"
            self.early_state = {
                "status": "STARTING",
                "message": "Reconstructing the frozen 583 historical candidates",
                "research_id": EARLY_CAUSAL_ENTRY_SPEC["research_id"],
                "alerts_enabled": False,
                "orders_enabled": False,
                "result_ready": False,
                "updated_at": iso(),
            }
            self.early_thread = threading.Thread(
                target=self.early_causal_entry_loop,
                name="independent-priority-early-causal-entry",
                daemon=True,
            )
            self.early_thread.start()
        return True, "started"

    def pause_early_causal_entry(self) -> tuple[bool, str]:
        with self.early_lock:
            if not self.early_thread or not self.early_thread.is_alive():
                return False, "not_running"
            self.early_stop_event.set()
        return True, "pause_requested"

    @staticmethod
    def _fit_artifact(rows: list[dict[str, Any]]) -> dict[str, Any]:
        X = np.asarray([[row["features"][name] for name in FEATURE_NAMES] for row in rows], dtype=float)
        y = np.asarray([1.0 if row["explosion_ge10"] else 0.0 for row in rows], dtype=float)
        fitted = fit_logistic(X, y, l2=1.0)
        Z = (X - fitted["mean"]) / fitted["scale"]
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(fitted["beta"][0] + Z @ fitted["beta"][1:], -35, 35)))
        cutoff = float(np.quantile(probabilities, 0.95))
        return {
            "schema": 1, "created_at": iso(), "protocol_id": PROTOCOL_ID,
            "protocol_sha256": PROTOCOL_SHA256, "source": "original 45 development sessions",
            "feature_names": list(FEATURE_NAMES), "l2_penalty": 1.0,
            "selection_fraction": 0.05, "development_rows": len(rows),
            "development_positive_count": int(y.sum()),
            "development_positive_rate_pct": round(float(y.mean()) * 100, 6),
            "standardization_mean": [float(value) for value in fitted["mean"]],
            "standardization_scale": [float(value) for value in fitted["scale"]],
            "intercept_and_standardized_coefficients": [float(value) for value in fitted["beta"]],
            "frozen_probability_cutoff": cutoff,
            "converged": bool(fitted["converged"]), "iterations": int(fitted["iterations"]),
            "historical_oof_top5_explosion_rate_pct": 5.397727,
            "historical_oof_baseline_rate_pct": 0.947082,
            "historical_oof_lift": 5.6993,
            "warning": "Ranking evidence only. No profitable entry/exit policy was validated.",
        }

    def _historical_training_rows(self) -> list[dict[str, Any]]:
        manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {})
        development_sessions = set(manifest.get("development_sessions") or [])
        if not development_sessions:
            raise RuntimeError("Historical development session manifest is missing")
        pc_rows = dict(self.redis.scan_hash_json(f"{self.source_prefix}:pcprofit:v2:cases"))
        self.save_state(
            status="BOOTSTRAPPING_MODEL", message="Loaded historical price-change cases",
            historical_price_change_cases=len(pc_rows),
        )
        er_rows = dict(self.redis.scan_hash_json(f"{self.source_prefix}:pcprofit_er45:v1:cases"))
        self.save_state(
            status="BOOTSTRAPPING_MODEL", message="Loaded historical ER45 cases",
            historical_price_change_cases=len(pc_rows), historical_er45_cases=len(er_rows),
        )
        common = set(pc_rows) & set(er_rows)
        if not common:
            raise RuntimeError("Historical price-change/ER45 case intersection is empty")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        result_keys = [f"{self.source_prefix}:results"] + [
            f"{self.source_prefix}:results:{session}" for session in manifest.get("sessions", [])
        ]
        for result_key in result_keys:
            for _, result in self.redis.scan_hash_json(result_key):
                if result.get("mode") != "approx":
                    continue
                signal = result.get("breakout_ready")
                if not signal or signal.get("phase") != "REGULAR":
                    continue
                case_id = f"{result.get('session')}|{result.get('symbol')}|{signal.get('ts')}"
                if case_id in seen or case_id not in common:
                    continue
                pc = pc_rows[case_id]
                er = er_rows[case_id]
                if pc.get("session") not in development_sessions or not pc.get("has_plan") or not er.get("has_plan"):
                    continue
                required = [
                    pc.get("price_change_pct_last45m"), er.get("er45"), pc.get("recomputed_mfe_pct"),
                    signal.get("price"), signal.get("opportunity"), signal.get("failure_pressure"), signal.get("ts"),
                ]
                if any(value is None for value in required) or float(signal["price"]) <= 0:
                    continue
                stamp = parse_dt(str(signal["ts"])).astimezone(NY)
                change = float(pc["price_change_pct_last45m"])
                efficiency = float(er["er45"])
                rows.append({
                    "features": {
                        "price_change_pct_last45m": change, "er45": efficiency,
                        "price_change_x_er45": change * efficiency,
                        "log_signal_price": math.log(float(signal["price"])),
                        "opportunity": float(signal["opportunity"]),
                        "failure_pressure": float(signal["failure_pressure"]),
                        "minutes_since_regular_open": float(stamp.hour * 60 + stamp.minute - 570),
                    },
                    "explosion_ge10": float(pc["recomputed_mfe_pct"]) >= 10.0,
                })
                seen.add(case_id)
                if len(rows) % 2500 == 0:
                    self.save_state(
                        status="BOOTSTRAPPING_MODEL", message="Joining historical causal signal rows",
                        historical_joined_rows=len(rows),
                    )
        expected_rows = int(os.getenv("IPR_EXPECTED_DEVELOPMENT_ROWS", "16894"))
        positive_count = sum(bool(row["explosion_ge10"]) for row in rows)
        expected_positive = int(os.getenv("IPR_EXPECTED_DEVELOPMENT_POSITIVES", "160"))
        if len(rows) != expected_rows or positive_count != expected_positive:
            raise RuntimeError(
                "Historical training dataset mismatch: "
                f"found rows={len(rows)}, positives={positive_count}; "
                f"expected rows={expected_rows}, positives={expected_positive}"
            )
        return rows

    def load_or_bootstrap_model(self) -> None:
        stored = self.redis.get_json(self.key("quality_model"), None)
        if stored is not None:
            if stored.get("protocol_sha256") != PROTOCOL_SHA256:
                raise RuntimeError("Stored model protocol mismatch; use a new Redis prefix")
            self.model_artifact = stored
            self.model = QualityModel(stored)
            self.save_state(status="READY", message="Frozen quality model loaded")
            return
        self.save_state(status="BOOTSTRAPPING_MODEL", message="Fitting one frozen model from historical Development data")
        rows = self._historical_training_rows()
        artifact = self._fit_artifact(rows)
        self.redis.set_json(self.key("quality_model"), artifact)
        self.redis.set_json(self.key("protocol_lock"), {"protocol": PROTOCOL, "protocol_sha256": PROTOCOL_SHA256})
        self.model_artifact = artifact
        self.model = QualityModel(artifact)
        self.save_state(status="READY", message="Frozen quality model created and locked")

    @staticmethod
    def _allowed_asset(asset: dict[str, Any]) -> bool:
        symbol = str(asset.get("symbol") or "").upper()
        if not SYMBOL_RE.fullmatch(symbol) or not asset.get("tradable", False):
            return False
        name = str(asset.get("name") or "").lower()
        excluded = (
            " etf", "exchange traded fund", "etn", "warrant", " right", " unit",
            "preferred", "depositary shares", "acquisition corp", "blank check",
            "casino", "gaming", "betting", "wager", "alcohol", "brew", "distiller",
            "tobacco", "cannabis", "marijuana", "pork", "swine",
        )
        return not any(term in f" {name}" for term in excluded)

    def refresh_universe(self) -> None:
        assets = [asset for asset in self.alpaca.assets() if self._allowed_asset(asset)]
        metadata = {str(asset["symbol"]).upper(): asset for asset in assets}
        with self.lock:
            self.asset_metadata = metadata
            self.universe = sorted(metadata)
            self.last_universe_refresh = now_utc()
        self.redis.set_json(self.key("universe"), {
            "updated_at": iso(), "symbols": self.universe, "count": len(self.universe),
            "source": "Alpaca active tradable US-equity assets", "sharia_keyword_exclusions": True,
        })
        logging.info("Universe refreshed: %s symbols", len(self.universe))

    def refresh_hot_symbols(self) -> None:
        scored: list[tuple[float, str]] = []
        for batch in chunks(self.universe, 400):
            for symbol, snapshot in self.alpaca.snapshots(batch).items():
                daily = snapshot.get("dailyBar") or {}
                trade = snapshot.get("latestTrade") or {}
                minute = snapshot.get("minuteBar") or {}
                price = float(trade.get("p") or minute.get("c") or daily.get("c") or 0)
                volume = float(daily.get("v") or 0)
                dollar_volume = price * volume
                if self.price_min <= price <= self.price_max and volume >= self.min_day_volume and dollar_volume >= self.min_dollar_volume:
                    scored.append((dollar_volume, str(symbol).upper()))
        scored.sort(reverse=True)
        with self.lock:
            self.hot_symbols = [symbol for _, symbol in scored[:self.max_deep_symbols]]
            self.last_snapshot_refresh = now_utc()
        logging.info("Hot universe refreshed: %s symbols", len(self.hot_symbols))

    @staticmethod
    def _session_start(moment: datetime) -> datetime:
        local = moment.astimezone(NY)
        previous_day = local.date() - timedelta(days=1)
        return datetime.combine(previous_day, dtime(16, 0), tzinfo=NY).astimezone(UTC)

    @staticmethod
    def merge_bars(sip_rows: list[dict[str, Any]], boats_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for feed, rows in (("sip", sip_rows), ("boats", boats_rows)):
            for raw in rows:
                if not raw.get("t"):
                    continue
                local = parse_dt(raw["t"]).astimezone(NY)
                minute = local.hour * 60 + local.minute
                overnight = minute >= 1200 or minute < 240
                if (feed == "boats") == overnight:
                    merged[raw["t"]] = {**raw, "feed": feed}
        return sorted(merged.values(), key=lambda bar: bar["t"])

    def _float_snapshot(self, symbol: str) -> dict[str, Any]:
        for key in self.float_keys:
            raw = self.redis.command("GET", key)
            if raw is None:
                continue
            try:
                document = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            value = find_symbol_record(document, symbol)
            if value is not None:
                if isinstance(value, dict):
                    float_value = (
                        value.get("float") or value.get("float_shares") or value.get("shares_float")
                        or value.get("floatShares") or value.get("sharesFloat")
                    )
                    updated = value.get("updated_at") or value.get("timestamp") or document.get("updated_at")
                else:
                    float_value, updated = value, document.get("updated_at") if isinstance(document, dict) else None
                return {"available": True, "value": float_value, "source_key": key, "source_updated_at": updated, "raw": value}
        return {"available": False, "value": None, "source_key": None, "source_updated_at": None}

    def _news_snapshot(self, symbol: str) -> dict[str, Any]:
        raw = self.redis.command("HGET", self.news_key, symbol)
        if raw is None:
            return {"available": False, "source_key": self.news_key, "items": []}
        try:
            document = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"available": False, "source_key": self.news_key, "items": [], "parse_error": True}
        articles = document.get("articles") if isinstance(document, dict) else None
        if not isinstance(articles, list):
            articles = [document]
        return {
            "available": bool(articles), "source_key": self.news_key,
            "captured_at": iso(), "analysis": document.get("analysis") if isinstance(document, dict) else None,
            "items": articles[:5],
        }

    def _candidate_id(self, symbol: str, bar_ts: str) -> str:
        return f"{parse_dt(bar_ts).date().isoformat()}|{symbol}|{bar_ts}"

    def _save_candidate(
        self,
        symbol: str,
        bar: dict[str, Any],
        history: list[dict[str, Any]],
        features: dict[str, float],
        diagnostics: dict[str, Any],
        probability: float,
    ) -> dict[str, Any] | None:
        candidate_id = self._candidate_id(symbol, bar["t"])
        session = parse_dt(bar["t"]).astimezone(NY).date().isoformat()
        session_symbol = f"{session}|{symbol}"
        if self.redis.command("SISMEMBER", self.key("seen_session_symbols"), session_symbol):
            return None
        record = {
            "schema": 1, "candidate_id": candidate_id, "symbol": symbol, "session": session,
            "created_at": iso(), "candidate_ts": bar["t"], "candidate_price": diagnostics["price"],
            "frozen_resistance": diagnostics["resistance"], "quality_probability": probability,
            "quality_cutoff": self.model.cutoff if self.model else None, "quality_top5": True,
            "features": features, "diagnostics": diagnostics,
            "point_in_time": {
                "pre_bars": history[-90:], "float": self._float_snapshot(symbol),
                "news": self._news_snapshot(symbol), "asset": self.asset_metadata.get(symbol, {}),
            },
            "confirmation": {"status": "PENDING", "deadline": iso(parse_dt(bar["t"]) + timedelta(minutes=self.confirmation_window))},
            "candidate_outcome_60m": None, "confirmation_outcome_60m": None,
            "live_5s_tracking": None, "session_close_outcome": None,
            "telegram": {"sent": False},
        }
        created = int(self.redis.command("HSETNX", self.key("samples"), candidate_id, json_compact(record)) or 0)
        if not created:
            return None
        self.redis.command("SADD", self.key("seen_session_symbols"), session_symbol)
        self.redis.command("ZADD", self.key("sample_index"), int(parse_dt(bar["t"]).timestamp()), candidate_id)
        self.redis.command("SADD", self.key("open_candidates"), candidate_id)
        logging.info(
            "PRIORITY_SAVED symbol=%s probability=%.6f cutoff=%.6f price=%.4f resistance=%.4f float=%s news=%s",
            symbol, probability, self.model.cutoff if self.model else -1, diagnostics["price"], diagnostics["resistance"],
            record["point_in_time"]["float"].get("value"), record["point_in_time"]["news"].get("available"),
        )
        return record

    def _telegram(self, text: str) -> tuple[bool, str | None]:
        if not self.telegram_token or not self.telegram_chat_id:
            return False, "telegram_environment_missing"
        body = urlencode({"chat_id": self.telegram_chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
        req = Request(f"https://api.telegram.org/bot{self.telegram_token}/sendMessage", data=body, method="POST")
        try:
            with urlopen(req, timeout=30) as response:
                payload = json.load(response)
            return bool(payload.get("ok")), None if payload.get("ok") else str(payload)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _format_float(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "غير متوفر"
        if number >= 1_000_000:
            return f"{number / 1_000_000:.2f} مليون"
        if number >= 1_000:
            return f"{number / 1_000:.1f} ألف"
        return f"{number:.0f}"

    def _send_confirmation(self, record: dict[str, Any]) -> None:
        news = record["point_in_time"]["news"]
        analysis = news.get("analysis") if isinstance(news, dict) else None
        headline = None
        if news.get("items"):
            headline = news["items"][0].get("headline") or news["items"][0].get("title")
        lines = [
            "🚨 تأكيد مراقبة — Independent Priority Radar",
            f"الرمز: {record['symbol']}",
            f"السعر: ${record['confirmation']['price']:.4f}",
            f"المقاومة المخترقة: ${record['frozen_resistance']:.4f}",
            f"تصنيف النموذج: أعلى 5% ({record['quality_probability'] * 100:.2f}%)",
            f"الفلوت: {self._format_float(record['point_in_time']['float'].get('value'))}",
            f"الخبر: {'موجود' if news.get('available') else 'غير موجود في الكاش المشترك'}",
        ]
        if isinstance(analysis, dict) and analysis.get("category"):
            lines.append(f"تصنيف الخبر: {analysis.get('category')}")
        if headline:
            lines.append(f"العنوان: {str(headline)[:180]}")
        lines.extend([
            "التأكيد: إغلاق دقيقة فوق المقاومة دون رفض علوي واضح.",
            "⚠️ تنبيه مراقبة تجريبي وليس أمراً بالشراء.",
        ])
        sent, error = self._telegram("\n".join(lines))
        record["telegram"] = {"sent": sent, "attempted_at": iso(), "error": error}
        logging.info("TELEGRAM_CONFIRMATION symbol=%s sent=%s error=%s", record["symbol"], sent, error)

    def _process_candidate_record(self, record: dict[str, Any], bars: list[dict[str, Any]], moment: datetime) -> dict[str, Any]:
        candidate_time = parse_dt(record["candidate_ts"])
        resistance = float(record["frozen_resistance"])
        confirmation = record.get("confirmation") or {"status": "PENDING"}
        if confirmation.get("status") == "PENDING":
            eligible = [
                bar for bar in sorted(bars, key=lambda item: item["t"])
                if candidate_time <= parse_dt(bar["t"]) <= candidate_time + timedelta(minutes=self.confirmation_window)
                and parse_dt(bar["t"]) + timedelta(minutes=1) <= moment
            ]
            attempts = [confirmation_metrics(bar, resistance) for bar in eligible]
            passed = next((item for item in attempts if item["confirmed"]), None)
            if passed:
                confirmation = {
                    "status": "CONFIRMED", "confirmed_at": passed["bar_ts"],
                    "price": passed["close"], "metrics": passed, "attempts": attempts,
                }
                record["confirmation"] = confirmation
                logging.info(
                    "CONFIRMATION_PASSED symbol=%s bar=%s close=%.4f resistance=%.4f upper_wick_ratio=%.4f",
                    record["symbol"], passed["bar_ts"], passed["close"], resistance, passed["upper_wick_to_range"],
                )
                self._send_confirmation(record)
            elif moment >= candidate_time + timedelta(minutes=self.confirmation_window + 1):
                record["confirmation"] = {
                    "status": "EXPIRED_UNCONFIRMED", "expired_at": iso(moment),
                    "attempts": attempts, "last_reasons": attempts[-1]["reasons"] if attempts else ["no_completed_bar"],
                }
                logging.info(
                    "CONFIRMATION_EXPIRED symbol=%s reasons=%s",
                    record["symbol"], record["confirmation"]["last_reasons"],
                )
            else:
                record["confirmation"] = {**confirmation, "attempts": attempts}

        record["candidate_outcome_60m"] = outcome_metrics(
            bars, candidate_time, float(record["candidate_price"]), 60
        )
        if moment >= candidate_time + timedelta(minutes=61) and record["candidate_outcome_60m"].get("forward_bars"):
            record["candidate_outcome_60m"]["complete"] = True
        if record["confirmation"].get("status") == "CONFIRMED":
            confirmation_time = parse_dt(record["confirmation"]["confirmed_at"])
            record["confirmation_outcome_60m"] = outcome_metrics(
                bars, confirmation_time, float(record["confirmation"]["price"]), 60
            )
            if moment >= confirmation_time + timedelta(minutes=61) and record["confirmation_outcome_60m"].get("forward_bars"):
                record["confirmation_outcome_60m"]["complete"] = True
                if record.get("live_5s_tracking"):
                    record["live_5s_tracking"]["complete"] = True
                    record["live_5s_tracking"]["completed_at"] = iso(moment)
        if moment.astimezone(NY).time() >= dtime(16, 0):
            same_session = [bar for bar in bars if parse_dt(bar["t"]).astimezone(NY).date().isoformat() == record["session"]]
            if same_session:
                final = same_session[-1]
                record["session_close_outcome"] = {
                    "price": float(final["c"]), "ts": final["t"],
                    "return_from_candidate_pct": round((float(final["c"]) / float(record["candidate_price"]) - 1) * 100, 5),
                }
        candidate_complete = bool((record.get("candidate_outcome_60m") or {}).get("complete"))
        confirmation_complete = (
            record["confirmation"].get("status") != "CONFIRMED"
            or bool((record.get("confirmation_outcome_60m") or {}).get("complete"))
        )
        if candidate_complete and confirmation_complete and record["confirmation"].get("status") != "PENDING":
            record["finalized_at"] = iso(moment)
            self.redis.command("SREM", self.key("open_candidates"), record["candidate_id"])
        return record

    def _bars_for_symbols(
        self, symbols: list[str], moment: datetime, start_override: datetime | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = {}
        start = start_override or self._session_start(moment)
        for batch in chunks(sorted(set(symbols)), 100):
            sip = self.alpaca.bars(batch, start, moment, "sip", "raw")
            try:
                boats = self.alpaca.bars(batch, start, moment, "boats", "raw")
            except Exception as exc:
                logging.warning("BOATS unavailable for live merge; continuing with SIP: %s", exc)
                boats = {symbol: [] for symbol in batch}
            for symbol in batch:
                output[symbol] = self.merge_bars(sip.get(symbol, []), boats.get(symbol, []))
        return output

    def scan_once(self, moment: datetime | None = None) -> dict[str, Any]:
        moment = (moment or now_utc()).astimezone(UTC)
        local = moment.astimezone(NY)
        if local.weekday() >= 5 or not (dtime(9, 30) <= local.time() < dtime(16, 0)):
            return {"scanned": 0, "priority_saved": 0, "reason": "outside_regular_session"}
        if self.model is None:
            raise RuntimeError("Quality model is not loaded")
        if not self.universe or self.last_universe_refresh is None or moment - self.last_universe_refresh >= timedelta(seconds=self.universe_refresh):
            self.refresh_universe()
        if not self.hot_symbols or self.last_snapshot_refresh is None or moment - self.last_snapshot_refresh >= timedelta(seconds=self.snapshot_refresh):
            self.refresh_hot_symbols()
        bars_by_symbol = self._bars_for_symbols(self.hot_symbols, moment)
        saved = 0
        ready_count = 0
        for symbol, history in bars_by_symbol.items():
            calculated = phase2_features(history, moment)
            if calculated is None:
                continue
            features, diagnostics = calculated
            if not diagnostics["base_ready"]:
                continue
            ready_count += 1
            probability = self.model.probability(features)
            if probability < self.model.cutoff:
                logging.info("BASE_READY_NOT_PRIORITY symbol=%s probability=%.6f cutoff=%.6f", symbol, probability, self.model.cutoff)
                continue
            completed = [bar for bar in history if parse_dt(bar["t"]) + timedelta(minutes=1) <= moment]
            if completed and self._save_candidate(symbol, completed[-1], completed, features, diagnostics, probability):
                saved += 1
        self.save_state(
            status="RUNNING", message="Independent scan completed", last_scan_at=iso(moment),
            hot_symbols=len(self.hot_symbols), base_ready=ready_count, priority_saved=saved, last_error=None,
        )
        return {"scanned": len(self.hot_symbols), "base_ready": ready_count, "priority_saved": saved}

    def monitor_open_candidates(self, moment: datetime | None = None, cached_bars: dict[str, list[dict[str, Any]]] | None = None) -> int:
        moment = (moment or now_utc()).astimezone(UTC)
        candidate_ids = list(self.redis.command("SMEMBERS", self.key("open_candidates")) or [])
        if not candidate_ids:
            return 0
        records = [self.redis.hget_json(self.key("samples"), candidate_id) for candidate_id in candidate_ids]
        records = [record for record in records if record]
        symbols = sorted({record["symbol"] for record in records})
        bars_by_symbol = dict(cached_bars or {})
        missing = [symbol for symbol in symbols if symbol not in bars_by_symbol]
        if missing:
            earliest = min(parse_dt(record["candidate_ts"]) for record in records if record["symbol"] in missing)
            bars_by_symbol.update(self._bars_for_symbols(missing, moment, earliest - timedelta(minutes=60)))
        for record in records:
            candidate_id = record["candidate_id"]
            with self.record_lock:
                latest = self.redis.hget_json(self.key("samples"), candidate_id, record)
                updated = self._process_candidate_record(
                    latest,
                    bars_by_symbol.get(latest["symbol"], []),
                    moment,
                )
                self.redis.hset_json(self.key("samples"), candidate_id, updated)
        return len(records)

    @staticmethod
    def _snapshot_price(snapshot: dict[str, Any]) -> tuple[float, str | None]:
        trade = snapshot.get("latestTrade") or {}
        minute = snapshot.get("minuteBar") or {}
        daily = snapshot.get("dailyBar") or {}
        price = float(trade.get("p") or minute.get("c") or daily.get("c") or 0)
        market_ts = trade.get("t") or minute.get("t") or daily.get("t")
        return price, str(market_ts) if market_ts else None

    def sample_confirmed_live(self, moment: datetime | None = None) -> int:
        """Save supplemental five-second samples for confirmed alerts for 60 minutes."""
        moment = (moment or now_utc()).astimezone(UTC)
        candidate_ids = list(self.redis.command("SMEMBERS", self.key("open_candidates")) or [])
        records = [self.redis.hget_json(self.key("samples"), candidate_id) for candidate_id in candidate_ids]
        records = [
            record for record in records
            if record and (record.get("confirmation") or {}).get("status") == "CONFIRMED"
            and moment <= parse_dt(record["confirmation"]["confirmed_at"]) + timedelta(minutes=61)
        ]
        if not records:
            return 0
        snapshots: dict[str, Any] = {}
        symbols = sorted({record["symbol"] for record in records})
        for batch in chunks(symbols, 400):
            snapshots.update(self.alpaca.snapshots(batch))
        saved = 0
        for record in records:
            snapshot = snapshots.get(record["symbol"]) or {}
            price, market_ts = self._snapshot_price(snapshot)
            if price <= 0:
                continue
            candidate_id = record["candidate_id"]
            sample_key = self.key(f"live5s:{candidate_id}")
            captured_at = iso(moment)
            with self.record_lock:
                latest = self.redis.hget_json(self.key("samples"), candidate_id, record)
                tracking = dict(latest.get("live_5s_tracking") or {})
                tracking.setdefault("samples_key", sample_key)
                updated_tracking, changed = update_live_tracking(
                    tracking,
                    float(latest["confirmation"]["price"]),
                    price,
                    captured_at,
                    market_ts,
                )
                if not changed:
                    continue
                sample = {
                    "captured_at": captured_at,
                    "market_ts": market_ts,
                    "price": price,
                    "return_pct": updated_tracking["last_return_pct"],
                }
                self.redis.command("RPUSH", sample_key, json_compact(sample))
                self.redis.command("LTRIM", sample_key, -1000, -1)
                latest["live_5s_tracking"] = updated_tracking
                self.redis.hset_json(self.key("samples"), candidate_id, latest)
                saved += 1
                logging.info(
                    "LIVE_5S_SAMPLE symbol=%s price=%.4f return_pct=%.5f samples=%s",
                    latest["symbol"],
                    price,
                    updated_tracking["last_return_pct"],
                    updated_tracking["samples"],
                )
        return saved

    @staticmethod
    def _within_monitoring_hours(moment: datetime) -> bool:
        local = moment.astimezone(NY)
        return local.weekday() < 5 and dtime(4, 0) <= local.time() <= dtime(17, 30)

    def _monitor_forever(self) -> None:
        while not self.stop_event.is_set():
            moment = now_utc()
            try:
                monitored = self.monitor_open_candidates(moment) if self._within_monitoring_hours(moment) else 0
                with self.lock:
                    self.state["last_pending_check_at"] = iso(moment)
                    self.state["pending_records_checked"] = monitored
                if monitored:
                    logging.info("PENDING_MONITOR checked=%s", monitored)
            except Exception as exc:
                logging.exception("Pending confirmation monitor failed")
                with self.lock:
                    self.state["last_monitor_error"] = f"{type(exc).__name__}: {exc}"
            self.stop_event.wait(self.pending_interval)

    def _live_sample_forever(self) -> None:
        while not self.stop_event.is_set():
            moment = now_utc()
            try:
                saved = self.sample_confirmed_live(moment) if self._within_monitoring_hours(moment) else 0
                with self.lock:
                    self.state["last_live_sample_check_at"] = iso(moment)
                    self.state["live_samples_saved"] = saved
                if saved:
                    logging.info("LIVE_5S_CYCLE saved=%s", saved)
            except Exception as exc:
                logging.exception("Confirmed live sampler failed")
                with self.lock:
                    self.state["last_live_sample_error"] = f"{type(exc).__name__}: {exc}"
            self.stop_event.wait(self.live_sample_interval)

    @staticmethod
    def _group_stats(records: list[dict[str, Any]], confirmed_only: bool) -> dict[str, Any]:
        selected = [record for record in records if (record.get("confirmation") or {}).get("status") == "CONFIRMED"] if confirmed_only else records
        outcomes = []
        for record in selected:
            outcome = record.get("confirmation_outcome_60m") if confirmed_only else record.get("candidate_outcome_60m")
            if outcome and outcome.get("complete"):
                outcomes.append((record, outcome))
        mfes = [float(outcome["mfe_pct"]) for _, outcome in outcomes if outcome.get("mfe_pct") is not None]
        maes = [float(outcome["mae_pct"]) for _, outcome in outcomes if outcome.get("mae_pct") is not None]
        return {
            "records": len(selected), "evaluable": len(outcomes),
            "reached_2pct": sum(bool(outcome.get("reached_2pct")) for _, outcome in outcomes),
            "reached_5pct": sum(bool(outcome.get("reached_5pct")) for _, outcome in outcomes),
            "reached_10pct": sum(bool(outcome.get("reached_10pct")) for _, outcome in outcomes),
            "average_mfe_pct": round(mean(mfes), 4) if mfes else None,
            "median_mfe_pct": round(median(mfes), 4) if mfes else None,
            "average_mae_pct": round(mean(maes), 4) if maes else None,
            "median_mae_pct": round(median(maes), 4) if maes else None,
        }

    @staticmethod
    def _float_band(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "missing"
        if number < 10_000_000:
            return "under_10m"
        if number < 50_000_000:
            return "10m_to_50m"
        return "50m_plus"

    def weekly_summary(self, start: datetime, end: datetime) -> dict[str, Any]:
        ids = self.redis.command(
            "ZRANGEBYSCORE", self.key("sample_index"), int(start.timestamp()), int(end.timestamp())
        ) or []
        records = [self.redis.hget_json(self.key("samples"), candidate_id) for candidate_id in ids]
        records = [record for record in records if record]
        confirmed = [record for record in records if (record.get("confirmation") or {}).get("status") == "CONFIRMED"]
        by_news = defaultdict(list)
        by_float = defaultdict(list)
        for record in records:
            by_news["with_news" if record["point_in_time"]["news"].get("available") else "without_news"].append(record)
            by_float[self._float_band(record["point_in_time"]["float"].get("value"))].append(record)
        ranked_confirmed = [
            (record, record.get("confirmation_outcome_60m") or {}) for record in confirmed
            if (record.get("confirmation_outcome_60m") or {}).get("mfe_pct") is not None
        ]
        ranked_confirmed.sort(key=lambda item: float(item[1]["mfe_pct"]))
        report = {
            "schema": 1, "generated_at": iso(), "period_start": iso(start), "period_end": iso(end),
            "priority_candidates": len(records), "confirmed_alerts": len(confirmed),
            "confirmation_rate_pct": round(len(confirmed) / max(1, len(records)) * 100, 2),
            "all_priority": self._group_stats(records, False),
            "confirmed": self._group_stats(records, True),
            "unconfirmed": self._group_stats(
                [record for record in records if (record.get("confirmation") or {}).get("status") != "CONFIRMED"], False
            ),
            "news_comparison": {name: self._group_stats(group, False) for name, group in sorted(by_news.items())},
            "float_comparison": {name: self._group_stats(group, False) for name, group in sorted(by_float.items())},
            "best_confirmed": ({"symbol": ranked_confirmed[-1][0]["symbol"], "mfe_pct": ranked_confirmed[-1][1]["mfe_pct"]} if ranked_confirmed else None),
            "worst_confirmed": ({"symbol": ranked_confirmed[0][0]["symbol"], "mfe_pct": ranked_confirmed[0][1]["mfe_pct"], "mae_pct": ranked_confirmed[0][1].get("mae_pct")} if ranked_confirmed else None),
        }
        if len(records) < 100:
            verdict = "العينة غير كافية للحكم"
        else:
            all_mfe = report["all_priority"].get("average_mfe_pct")
            confirmed_mfe = report["confirmed"].get("average_mfe_pct")
            verdict = "تحسن" if confirmed_mfe is not None and all_mfe is not None and confirmed_mfe > all_mfe else "دون تحسن مثبت"
        report["verdict"] = verdict
        return report

    def send_weekly_summary_if_due(self, moment: datetime | None = None) -> bool:
        moment = (moment or now_utc()).astimezone(UTC)
        local = moment.astimezone(NY)
        if local.weekday() != 4 or local.time() < dtime(17, 15):
            return False
        week_id = f"{local.isocalendar().year}-W{local.isocalendar().week:02d}"
        if self.redis.command("SISMEMBER", self.key("weekly_sent"), week_id):
            return False
        week_start_date = local.date() - timedelta(days=local.weekday())
        start = datetime.combine(week_start_date, dtime(0, 0), tzinfo=NY).astimezone(UTC)
        report = self.weekly_summary(start, moment)
        confirmed = report["confirmed"]
        unconfirmed = report["unconfirmed"]
        with_news = report["news_comparison"].get("with_news", {})
        without_news = report["news_comparison"].get("without_news", {})
        float_rows = [
            (name, block.get("average_mfe_pct")) for name, block in report["float_comparison"].items()
            if block.get("average_mfe_pct") is not None
        ]
        best_float = max(float_rows, key=lambda item: item[1]) if float_rows else None
        lines = [
            f"📊 ملخص Independent Priority Radar — {week_id}",
            f"مرشحو الأولوية: {report['priority_candidates']}",
            f"التنبيهات المؤكدة: {report['confirmed_alerts']} ({report['confirmation_rate_pct']}%)",
            f"حقق +2%: {confirmed['reached_2pct']} | +5%: {confirmed['reached_5pct']} | +10%: {confirmed['reached_10pct']}",
            f"MFE المتوسط/الوسيط: {confirmed['average_mfe_pct']}% / {confirmed['median_mfe_pct']}%",
            f"MAE المتوسط/الوسيط: {confirmed['average_mae_pct']}% / {confirmed['median_mae_pct']}%",
            f"MFE المؤكد/غير المؤكد: {confirmed['average_mfe_pct']}% / {unconfirmed['average_mfe_pct']}%",
            f"MFE مع خبر/بدون خبر: {with_news.get('average_mfe_pct')}% / {without_news.get('average_mfe_pct')}%",
            f"الحكم: {report['verdict']}",
        ]
        if best_float:
            lines.insert(-1, f"أفضل شريحة فلوت هذا الأسبوع: {best_float[0]} (MFE {best_float[1]}%)")
        if report.get("best_confirmed"):
            lines.insert(-1, f"الأفضل: {report['best_confirmed']['symbol']} ({report['best_confirmed']['mfe_pct']}%)")
        if report.get("worst_confirmed"):
            lines.insert(-1, f"الأضعف: {report['worst_confirmed']['symbol']} (MFE {report['worst_confirmed']['mfe_pct']}%)")
        sent, error = self._telegram("\n".join(lines))
        report["telegram"] = {"sent": sent, "error": error}
        self.redis.set_json(self.key(f"weekly:{week_id}"), report)
        if sent:
            self.redis.command("SADD", self.key("weekly_sent"), week_id)
        logging.info("WEEKLY_SUMMARY week=%s sent=%s error=%s", week_id, sent, error)
        return sent

    def run_forever(self) -> None:
        try:
            self.load_or_bootstrap_model()
            self.monitor_thread = threading.Thread(
                target=self._monitor_forever,
                name="independent-priority-pending-monitor",
                daemon=True,
            )
            self.live_sample_thread = threading.Thread(
                target=self._live_sample_forever,
                name="independent-priority-live-sampler",
                daemon=True,
            )
            self.monitor_thread.start()
            self.live_sample_thread.start()
            logging.info(
                "RUNTIME_LOOPS_STARTED scan=%ss pending=%ss live=%ss official_outcomes=one-minute-bars",
                self.scan_interval,
                self.pending_interval,
                self.live_sample_interval,
            )
            while not self.stop_event.is_set():
                try:
                    moment = now_utc()
                    self.scan_once(moment)
                    self.send_weekly_summary_if_due(moment)
                except Exception as exc:
                    logging.exception("Radar cycle failed")
                    self.save_state(status="ERROR", message="Cycle failed", last_error=f"{type(exc).__name__}: {exc}")
                self.stop_event.wait(self.scan_interval)
        except Exception as exc:
            logging.exception("Radar startup failed")
            self.save_state(status="ERROR", message="Startup failed", last_error=f"{type(exc).__name__}: {exc}")


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__)
radar = IndependentPriorityRadar()
worker: threading.Thread | None = None


def start_worker() -> None:
    global worker
    if os.getenv("IPR_DISABLE_BACKGROUND", "false").lower() in {"1", "true", "yes"}:
        return
    if worker and worker.is_alive():
        return
    worker = threading.Thread(target=radar.run_forever, name="independent-priority-radar", daemon=True)
    worker.start()


def export_authorized() -> bool:
    expected = os.getenv("IPR_ADMIN_TOKEN", os.getenv("NDR_BT_ADMIN_TOKEN", ""))
    supplied = (
        request.headers.get("X-Admin-Token")
        or request.form.get("token")
        or request.args.get("token")
        or ""
    )
    return bool(expected and supplied and supplied == expected)


@app.get("/")
def home():
    return jsonify({
        "service": "Independent Priority Radar", "version": VERSION, "build": BUILD,
        "purpose": "Priority ranking + simple confirmation + complete shadow samples",
        "telegram_policy": "Only confirmed alerts and the Friday weekly summary",
        "orders_enabled": False,
        "monitoring": MONITORING_SPEC,
        "links": {
            "health": "/health", "ready": "/ready", "status": "/status",
            "recent": "/api/candidates/recent", "weekly": "/api/weekly/latest",
            "protocol": "/protocol", "historical_export": "/historical-export",
            "historical_confirmation_audit": "/historical-confirmation",
            "early_causal_entry_research": "/early-causal-entry",
        },
    })


@app.get("/historical-export")
def historical_export_page():
    return """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>تصدير بيانات Independent Priority Radar</title>
    <style>
        body { font-family: system-ui; background: #101114; color: #eee; max-width: 760px; margin: 30px auto; padding: 18px; }
        .box { background: #191b20; border: 1px solid #343741; border-radius: 14px; padding: 20px; margin: 14px 0; }
        input, button { width: 100%; box-sizing: border-box; font-size: 17px; padding: 13px; margin: 7px 0; border-radius: 9px; border: 1px solid #555; }
        button { background: #6d28d9; color: white; font-weight: 700; }
        a { color: #a78bfa; }
    </style>
</head>
<body>
    <h1>تصدير تاريخي موجّه — قراءة فقط</h1>
    <div class="box">
        <p>التصدير ممنوع أثناء فترة مراقبة السوق، ويعمل بعد 17:30 بتوقيت نيويورك أو خلال الويكند.</p>
        <form method="post" action="/historical-export/start">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">ابدأ التصدير التاريخي</button>
        </form>
        <p><a href="/historical-export/status">متابعة حالة التصدير</a></p>
        <form method="post" action="/historical-export/download">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">تنزيل الملف المكتمل JSON.GZ</button>
        </form>
    </div>
</body>
</html>
"""


@app.post("/historical-export/start")
def historical_export_start():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    started, message = radar.start_historical_export()
    payload = {
        "ok": started,
        "status": message,
        "read_only": True,
        "status_url": "/historical-export/status",
        "download_url": "/historical-export/download",
    }
    return jsonify(payload), (202 if started else 409)


@app.get("/historical-export/status")
def historical_export_status():
    with radar.export_lock:
        payload = dict(radar.export_state)
        payload["worker_alive"] = bool(radar.export_thread and radar.export_thread.is_alive())
        payload["download_ready"] = bool(
            radar.export_path
            and os.path.isfile(radar.export_path)
            and payload.get("status") == "COMPLETED"
        )
    payload["read_only"] = True
    payload["download_url"] = "/historical-export/download"
    return jsonify(payload)


@app.post("/historical-export/download")
def historical_export_download():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    with radar.export_lock:
        path = radar.export_path
        ready = radar.export_state.get("status") == "COMPLETED"
    if not ready or not path or not os.path.isfile(path):
        return jsonify({"ready": False, "status_url": "/historical-export/status"}), 202
    filename = f"independent_priority_history_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    return send_file(path, mimetype="application/gzip", as_attachment=True, download_name=filename)


@app.get("/historical-confirmation")
def historical_confirmation_page():
    return """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>اختبار التأكيد التاريخي</title>
    <style>
        body { font-family: system-ui; background: #101114; color: #eee; max-width: 760px; margin: 30px auto; padding: 18px; }
        .box { background: #191b20; border: 1px solid #343741; border-radius: 14px; padding: 20px; margin: 14px 0; }
        input, button { width: 100%; box-sizing: border-box; font-size: 17px; padding: 13px; margin: 7px 0; border-radius: 9px; border: 1px solid #555; }
        button { background: #6d28d9; color: white; font-weight: 700; }
        a { color: #a78bfa; }
    </style>
</head>
<body>
    <h1>اختبار التأكيد التاريخي المستقل</h1>
    <div class="box">
        <p>يستخدم مرشحي OOF التاريخيين وبيانات Alpaca الدقيقة. لا يرسل تنبيهات ولا أوامر ولا يغيّر النموذج الحي.</p>
        <p>يُمنع البدء أثناء مراقبة السوق. التقدم محفوظ في مساحة Redis جديدة ويمكن استكماله.</p>
        <form method="post" action="/historical-confirmation/start">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">ابدأ أو استكمل الاختبار</button>
        </form>
        <p><a href="/historical-confirmation/status">متابعة التقدم</a> · <a href="/historical-confirmation/result">النتيجة المختصرة</a></p>
        <form method="post" action="/historical-confirmation/pause">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">إيقاف آمن بعد الدفعة الحالية</button>
        </form>
        <form method="post" action="/historical-confirmation/download">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">تنزيل النتيجة الكاملة JSON.GZ</button>
        </form>
    </div>
</body>
</html>
"""


@app.post("/historical-confirmation/start")
def historical_confirmation_start():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    started, message = radar.start_historical_confirmation_audit()
    return jsonify({
        "ok": started,
        "status": message,
        "status_url": "/historical-confirmation/status",
        "result_url": "/historical-confirmation/result",
        "download_url": "/historical-confirmation/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    }), (202 if started else 409)


@app.post("/historical-confirmation/pause")
def historical_confirmation_pause():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    ok, message = radar.pause_historical_confirmation_audit()
    return jsonify({"ok": ok, "status": message}), (202 if ok else 409)


@app.get("/historical-confirmation/status")
def historical_confirmation_status():
    stored = radar.redis.get_json(radar.audit_key("status"), None) if radar.redis.configured else None
    with radar.audit_lock:
        payload = dict(stored or radar.audit_state)
        payload["worker_alive"] = bool(radar.audit_thread and radar.audit_thread.is_alive())
    payload.update({
        "status_url": "/historical-confirmation/status",
        "result_url": "/historical-confirmation/result",
        "download_url": "/historical-confirmation/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    })
    return jsonify(payload)


@app.get("/historical-confirmation/result")
def historical_confirmation_result():
    report = radar.redis.get_json(radar.audit_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/historical-confirmation/status"}), 202
    return jsonify(report)


@app.post("/historical-confirmation/download")
def historical_confirmation_download():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    report = radar.redis.get_json(radar.audit_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/historical-confirmation/status"}), 202
    with radar.audit_lock:
        path = radar.audit_path
    if not path or not os.path.isfile(path):
        path = radar._materialize_historical_audit_download(report)
    filename = f"ipr_historical_confirmation_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    return send_file(path, mimetype="application/gzip", as_attachment=True, download_name=filename)


@app.get("/early-causal-entry")
def early_causal_entry_page():
    return """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>بحث الدخول السببي المبكر</title>
    <style>
        body { font-family: system-ui; background: #101114; color: #eee; max-width: 760px; margin: 30px auto; padding: 18px; }
        .box { background: #191b20; border: 1px solid #343741; border-radius: 14px; padding: 20px; margin: 14px 0; }
        input, button { width: 100%; box-sizing: border-box; font-size: 17px; padding: 13px; margin: 7px 0; border-radius: 9px; border: 1px solid #555; }
        button { background: #0f766e; color: white; font-weight: 700; }
        a { color: #5eead4; }
    </style>
</head>
<body>
    <h1>Early Causal Entry Research</h1>
    <div class="box">
        <p>يفحص معلومات كانت متاحة بنهاية شمعة الاكتشاف فقط، ويقيّم العائد الصافي بعد تكلفة 0.25% عبر Development زمنيًا.</p>
        <p>لا يغيّر البوت الحي أو التأكيد أو Telegram، ولا يرسل أوامر. يبدأ بعد 17:30 نيويورك أو خلال الويكند.</p>
        <form method="post" action="/early-causal-entry/start">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">ابدأ أو استكمل البحث</button>
        </form>
        <p><a href="/early-causal-entry/status">متابعة التقدم</a> · <a href="/early-causal-entry/result">النتيجة المختصرة</a></p>
        <form method="post" action="/early-causal-entry/pause">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">إيقاف آمن بعد الجلسة الحالية</button>
        </form>
        <form method="post" action="/early-causal-entry/download">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">تنزيل النتيجة الكاملة JSON.GZ</button>
        </form>
    </div>
</body>
</html>
"""


@app.post("/early-causal-entry/start")
def early_causal_entry_start():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    started, message = radar.start_early_causal_entry()
    return jsonify({
        "ok": started,
        "status": message,
        "status_url": "/early-causal-entry/status",
        "result_url": "/early-causal-entry/result",
        "download_url": "/early-causal-entry/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    }), (202 if started else 409)


@app.post("/early-causal-entry/pause")
def early_causal_entry_pause():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    ok, message = radar.pause_early_causal_entry()
    return jsonify({"ok": ok, "status": message}), (202 if ok else 409)


@app.get("/early-causal-entry/status")
def early_causal_entry_status():
    stored = radar.redis.get_json(radar.early_key("status"), None) if radar.redis.configured else None
    with radar.early_lock:
        payload = dict(stored or radar.early_state)
        payload["worker_alive"] = bool(radar.early_thread and radar.early_thread.is_alive())
    payload.update({
        "status_url": "/early-causal-entry/status",
        "result_url": "/early-causal-entry/result",
        "download_url": "/early-causal-entry/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    })
    return jsonify(payload)


@app.get("/early-causal-entry/result")
def early_causal_entry_result():
    report = radar.redis.get_json(radar.early_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/early-causal-entry/status"}), 202
    return jsonify(report)


@app.post("/early-causal-entry/download")
def early_causal_entry_download():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    report = radar.redis.get_json(radar.early_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/early-causal-entry/status"}), 202
    with radar.early_lock:
        path = radar.early_path
    if not path or not os.path.isfile(path):
        path = radar._materialize_early_download(report)
    filename = f"ipr_early_causal_entry_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    return send_file(path, mimetype="application/gzip", as_attachment=True, download_name=filename)


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "version": VERSION,
        "build": BUILD,
        "worker_alive": bool(worker and worker.is_alive()),
        "pending_monitor_alive": bool(radar.monitor_thread and radar.monitor_thread.is_alive()),
        "live_sampler_alive": bool(radar.live_sample_thread and radar.live_sample_thread.is_alive()),
        "historical_audit_alive": bool(radar.audit_thread and radar.audit_thread.is_alive()),
        "early_causal_entry_alive": bool(radar.early_thread and radar.early_thread.is_alive()),
    })


@app.get("/ready")
def ready():
    payload = {
        "ready": radar.model is not None and radar.redis.configured and radar.alpaca.configured,
        "model_loaded": radar.model is not None, "redis_configured": radar.redis.configured,
        "alpaca_configured": radar.alpaca.configured, "telegram_configured": bool(radar.telegram_token and radar.telegram_chat_id),
        "orders_enabled": False,
    }
    return jsonify(payload), (200 if payload["ready"] else 503)


@app.get("/status")
def status():
    with radar.lock:
        payload = dict(radar.state)
    payload.update({
        "worker_alive": bool(worker and worker.is_alive()), "universe_count": len(radar.universe),
        "pending_monitor_alive": bool(radar.monitor_thread and radar.monitor_thread.is_alive()),
        "live_sampler_alive": bool(radar.live_sample_thread and radar.live_sample_thread.is_alive()),
        "hot_symbols": len(radar.hot_symbols), "model": radar.model_artifact,
        "monitoring": MONITORING_SPEC,
    })
    return jsonify(payload)


@app.get("/protocol")
def protocol():
    return jsonify({
        "protocol": PROTOCOL,
        "protocol_sha256": PROTOCOL_SHA256,
        "runtime_monitoring": MONITORING_SPEC,
    })


@app.get("/api/candidates/recent")
def recent_candidates():
    limit = min(200, max(1, int(request.args.get("limit", "50"))))
    ids = radar.redis.command("ZREVRANGE", radar.key("sample_index"), 0, limit - 1) or []
    records = [radar.redis.hget_json(radar.key("samples"), candidate_id) for candidate_id in ids]
    return jsonify({"count": len([record for record in records if record]), "records": [record for record in records if record]})


@app.get("/api/candidate/<path:candidate_id>")
def candidate_detail(candidate_id: str):
    record = radar.redis.hget_json(radar.key("samples"), candidate_id)
    return (jsonify(record), 200) if record else (jsonify({"error": "not_found"}), 404)


@app.get("/api/live-samples/<path:candidate_id>")
def candidate_live_samples(candidate_id: str):
    limit = min(1000, max(1, int(request.args.get("limit", "1000"))))
    raw = radar.redis.command("LRANGE", radar.key(f"live5s:{candidate_id}"), -limit, -1) or []
    samples = []
    for item in raw:
        try:
            samples.append(json.loads(item))
        except (TypeError, json.JSONDecodeError):
            continue
    return jsonify({"candidate_id": candidate_id, "count": len(samples), "official": False, "samples": samples})


@app.get("/api/weekly/latest")
def weekly_latest():
    local = now_utc().astimezone(NY)
    for offset in range(0, 8):
        probe = local - timedelta(weeks=offset)
        week_id = f"{probe.isocalendar().year}-W{probe.isocalendar().week:02d}"
        report = radar.redis.get_json(radar.key(f"weekly:{week_id}"), None)
        if report:
            return jsonify(report)
    return jsonify({"status": "not_ready", "message": "No weekly summary has been generated yet"}), 404


@app.post("/admin/scan-once")
def admin_scan_once():
    expected = os.getenv("IPR_ADMIN_TOKEN", os.getenv("NDR_BT_ADMIN_TOKEN", ""))
    supplied = request.headers.get("X-Admin-Token") or request.form.get("token") or request.args.get("token") or ""
    if not expected or supplied != expected:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(radar.scan_once())


start_worker()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
