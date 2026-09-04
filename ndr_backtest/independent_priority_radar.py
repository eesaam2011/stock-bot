from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
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
from flask import Flask, jsonify, request


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

VERSION = "1.1.0"
BUILD = "INDEPENDENT-PRIORITY-RADAR-2026-09-04-B"
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


@app.get("/")
def home():
    return jsonify({
        "service": "Independent Priority Radar", "version": VERSION, "build": BUILD,
        "purpose": "Priority ranking + simple confirmation + complete shadow samples",
        "telegram_policy": "Only confirmed alerts and the Friday weekly summary",
        "orders_enabled": False,
        "monitoring": MONITORING_SPEC,
        "links": {"health": "/health", "ready": "/ready", "status": "/status", "recent": "/api/candidates/recent", "weekly": "/api/weekly/latest", "protocol": "/protocol"},
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "version": VERSION,
        "build": BUILD,
        "worker_alive": bool(worker and worker.is_alive()),
        "pending_monitor_alive": bool(radar.monitor_thread and radar.monitor_thread.is_alive()),
        "live_sampler_alive": bool(radar.live_sample_thread and radar.live_sample_thread.is_alive()),
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
