from __future__ import annotations

import hashlib
import base64
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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, time as dtime, timedelta, timezone
from statistics import mean, median
from typing import Any, Callable, Iterable
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

VERSION = "1.8.0"
BUILD = "INDEPENDENT-PRIORITY-RADAR-2026-09-05-I"
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

# Independent after-hours research.  The paper-rule reference is descriptive;
# the only user-executable variant is long-only, $10-$60, and capped at three
# equally weighted slots.  Nothing here is consumed by the live radar.
LIQUID_DAILY_ORB_SPEC = {
    "research_id": "IPR-LIQUID-DAILY-ORB-2026-09-05-A",
    "paper": {
        "title": "A Profitable Day Trading Strategy For The U.S. Equity Market",
        "opening_range_minutes": 5,
        "price_min_exclusive": 5.0,
        "average_share_volume_lookback_sessions": 14,
        "minimum_average_share_volume": 1_000_000,
        "atr_lookback_sessions": 14,
        "minimum_atr_exclusive": 0.50,
        "minimum_opening_relative_volume": 1.0,
        "daily_rank_count": 20,
        "directions": ["LONG", "SHORT"],
        "stop_distance": "10 percent of ATR14",
        "exit": "16:00 New York if stop is not hit",
        "commission_per_share_per_side_usd": 0.0035,
        "role": "paper-rule reference only; not deployable policy",
    },
    "user_primary": {
        "price_min_inclusive": 10.0,
        "price_max_inclusive": 60.0,
        "minimum_average_dollar_volume_60_sessions": 20_000_000,
        "minimum_atr_exclusive": 0.50,
        "intended_minimum_market_cap_usd": 2_000_000_000,
        "market_cap_handling": "reported when a stored fundamental snapshot exists; never backfilled with current data into a historical decision",
        "sharia_keyword_exclusions": True,
        "direction": "LONG_ONLY",
        "opening_relative_volume_lookback_sessions": 14,
        "minimum_opening_relative_volume": 1.0,
        "primary_daily_rank_count": 3,
        "diagnostic_daily_rank_count": 1,
        "stop_distance": "10 percent of ATR14",
        "exit": "16:00 New York if stop is not hit",
        "decision_cost_pct_round_trip": 0.25,
        "capital_sar_reference": 2000.0,
        "leverage": 1.0,
        "allocation": "equal slot weights; an untriggered slot remains cash",
    },
    "evaluation": {
        "sessions": "the frozen 60-session source manifest",
        "development_sessions": 45,
        "legacy_holdout_sessions": 15,
        "development_stability": "three consecutive 15-session blocks",
        "minimum_active_days_per_development_block": 5,
        "primary_judgment": "Top-3 must have PF > 1, average daily return > 0, and at least five active days in every Development block",
        "legacy_holdout_can_approve_live": False,
        "promising_wording": "PROMISING_SHADOW_ONLY",
        "failure_wording": "NO_STABLE_EDGE",
    },
    "safety": {
        "alerts_enabled": False,
        "orders_enabled": False,
        "changes_live_model": False,
        "changes_live_cutoff": False,
        "changes_live_confirmation": False,
    },
}

# A separate end-of-day signal research path.  Both holding policies are
# frozen before the run and receive independent judgments; neither is selected
# merely because it looks better after the fact.
DAILY_BREAKOUT_SPEC = {
    "research_id": "IPR-DAILY-BREAKOUT-VOLUME-2026-09-05-A",
    "signal": {
        "direction": "LONG_ONLY",
        "signal_time": "after the completed regular-session daily bar",
        "price_min_inclusive": 10.0,
        "price_max_inclusive": 60.0,
        "breakout": "signal close strictly above every high in the previous 20 sessions",
        "volume": "signal volume at least 1.5 times the previous 20-session average",
        "minimum_volume_ratio": 1.5,
        "minimum_average_dollar_volume_60_sessions": 20_000_000,
        "ranking": "descending signal-volume ratio, then symbol",
        "daily_rank_count": 3,
    },
    "universe": {
        "source": "frozen manifest symbols intersected with current active tradable Alpaca assets",
        "allowed": "ordinary operating-company shares and ADR descriptions",
        "excluded": "ETF, ETN, fund, trust, preferred, warrant, right, unit, blank-check/SPAC and explicit prohibited-business keywords",
        "classification_is_point_in_time": False,
        "sharia_scope": "explicit product and business-name exclusions only; not a full financial-ratio Sharia audit",
        "historical_market_cap_filter_applied": False,
        "market_cap_note": "No historical point-in-time market cap is available; average dollar volume is the causal liquidity screen.",
        "explicit_symbol_exclusions": [
            "ACB", "ACEL", "BALY", "BF.A", "BF.B", "BTI", "BUD", "BYD",
            "CGC", "CHDN", "CNTY", "CRON", "CZR", "DEO", "DKNG", "EVRI",
            "FLUT", "FLL", "GAN", "GDEN", "GENI", "HRL", "IGT", "JBS",
            "LNW", "LVS", "MGM", "MO", "NAPA", "OGI", "PENN", "PM",
            "RRR", "RSI", "SAM", "SEAT", "SGHC", "SNDL", "SRAD", "STZ",
            "TAP", "TLRY", "TPB", "TSN", "UVV", "VFF", "VWE", "WYNN",
        ],
    },
    "execution": {
        "entry": "next regular session 09:30 New York opening print",
        "entry_price_must_remain_between_10_and_60": True,
        "out_of_range_entry": "cancel selected slot; it remains cash and is not replaced",
        "stop": "one signal-day ATR14 below actual entry",
        "decision_cost_pct_round_trip": 0.25,
        "allocation": "Daily-1 uses three equal slots; Daily-2 uses six equal slots for two overlapping three-stock cohorts; unused or cancelled slots remain cash",
        "target": None,
        "gap_handling": "exit at worse opening print when a later session opens below the stop",
        "same_bar_entry_stop": "STOP",
    },
    "policies": {
        "daily_1": "enter next session open; exit that session close unless stopped",
        "daily_2": "enter next session open; exit the following session close unless stopped",
        "independent_judgments": True,
        "best_policy_selection_after_results": False,
    },
    "evaluation": {
        "sessions": "the frozen 60 signal-session source manifest",
        "development_sessions": 45,
        "legacy_holdout_sessions": 15,
        "development_blocks": "three consecutive 15-signal-session blocks",
        "minimum_active_days_per_block": 8,
        "minimum_active_days_full_development": 30,
        "minimum_pooled_profit_factor": 1.20,
        "minimum_pooled_average_net_return_pct": 0.10,
        "block_rule": "PF > 1 and average net return > 0 in every Development block",
        "legacy_holdout_can_approve_live": False,
        "forward_sessions_required_after_promising_result": 20,
        "failure_wording": "NO_STABLE_EDGE",
    },
    "safety": {
        "alerts_enabled": False,
        "orders_enabled": False,
        "changes_live_model": False,
        "changes_live_cutoff": False,
        "changes_live_confirmation": False,
    },
}

# Frozen before the first run.  This is an independent multi-year daily
# reversal study; it never changes the live Phase-2 radar or sends alerts.
RSI2_REVERSAL_SPEC = {
    "research_id": "IPR-RSI2-REVERSAL-2026-09-05-A",
    "data": {
        "feed": "sip",
        "timeframe": "1Day",
        "warmup_start": "2019-08-15",
        "minimum_warmup_sessions": 250,
        "development_start": "2020-09-01",
        "development_end": "2024-08-30",
        "legacy_holdout_start": "2024-09-03",
        "legacy_holdout_end": "2026-08-31",
        "adjusted_stream": "split",
        "raw_stream_role": "point-in-time price range and reported execution prices",
        "historical_depth_source": "Alpaca documents US equity history since 2016",
    },
    "universe": {
        "source": "frozen manifest symbols intersected with current active tradable Alpaca assets",
        "price_min_inclusive": 10.0,
        "price_max_inclusive": 60.0,
        "minimum_average_dollar_volume_60_prior_sessions": 20_000_000,
        "same_product_and_prohibited_business_exclusions_as_daily_breakout": True,
        "historical_market_cap_filter_applied": False,
        "survivorship_warning": "retrospective current-universe test; delisted historical securities are absent",
    },
    "signal": {
        "direction": "LONG_ONLY",
        "time": "after a completed regular-session daily bar",
        "trend": "split-adjusted close strictly above inclusive SMA200",
        "rsi_method": "Wilder RSI with period 2",
        "primary_threshold": "RSI2 < 5",
        "diagnostic_threshold": "RSI2 < 10; cannot rescue or replace the primary policy",
        "ranking": "lowest RSI2, then highest prior-60-session average dollar volume, then symbol",
        "daily_rank_count": 3,
    },
    "execution": {
        "entry": "next regular session daily open",
        "entry_price_must_remain_between_10_and_60": True,
        "exit": "if entry-day completed close is above inclusive SMA5, exit next-session open; otherwise exit next-session close",
        "maximum_holding_sessions": 2,
        "fixed_stop": None,
        "decision_cost_pct_round_trip": 0.25,
        "capital_slots": 6,
        "allocation": "three new equal candidates per signal day; six slots prevent hidden leverage across overlapping two-session cohorts",
    },
    "evaluation": {
        "development_blocks": [
            ["2020-09-01", "2021-08-31"],
            ["2021-09-01", "2022-08-31"],
            ["2022-09-01", "2023-08-31"],
            ["2023-09-01", "2024-08-30"],
        ],
        "minimum_trades_per_development_block": 30,
        "block_rule": "net PF > 1 and average net trade return > 0 in every Development block",
        "pooled_rule": "net PF > 1, average net trade return > 0, and at least 200 completed Development trades",
        "primary_policy_controls_final_judgment": True,
        "legacy_holdout_can_approve_live": False,
        "promising_wording": "PROMISING_SHADOW_ONLY",
        "failure_wording": "NO_STABLE_EDGE",
        "forward_minimum_sessions": 10,
        "forward_minimum_completed_trades": 30,
        "forward_maximum_sessions_if_trade_minimum_not_met": 15,
    },
    "throughput": {
        "alpaca_page_limit": 10000,
        "default_symbols_per_batch": 12,
        "default_parallel_workers": 24,
        "raw_daily_bars_saved_to_redis": False,
        "resume_unit": "completed compact symbol batch",
        "probe_before_full_run": True,
    },
    "safety": {
        "alerts_enabled": False,
        "orders_enabled": False,
        "changes_live_model": False,
        "changes_live_cutoff": False,
        "changes_live_confirmation": False,
    },
}

RSI2_SANITY_SPEC = {
    "audit_id": "IPR-RSI2-SANITY-CHECK-2026-09-05-A",
    "purpose": "Determine whether RSI2 adds value versus matched random selection and verify the daily-bar engine.",
    "source_policy": RSI2_REVERSAL_SPEC["research_id"],
    "matched_baseline": {
        "same_signal_sessions": True,
        "same_number_of_daily_slots": True,
        "same_current_clean_universe": True,
        "same_price_range": [10.0, 60.0],
        "same_prior_average_dollar_volume_floor": 20_000_000,
        "same_above_sma200_trend_filter": True,
        "removed_condition_only": "RSI2 threshold and RSI-based ranking",
        "same_entry_and_exit_policy": True,
    },
    "simulation": {
        "iterations": 1000,
        "seed": 20260905,
        "sampling": "without replacement within each signal session",
        "deterministic_uniform_reservoir_per_session": 64,
        "cost_sensitivity_pct_round_trip": [0.0, 0.10, 0.25],
        "primary_comparison_cost_pct": 0.25,
    },
    "integrity": {
        "independent_rsi2_recomputation": True,
        "direct_sma200_recomputation": True,
        "return_identity_check": "net equals gross minus cost",
        "maximum_allowed_indicator_difference": 1e-9,
    },
    "storage": {
        "raw_daily_bars_saved_to_redis": False,
        "checkpoint_every_completed_batches": 10,
        "compressed_year_shards": True,
    },
    "interpretation": {
        "rsi_below_random": "RSI filter/ranking harms selection under this policy",
        "rsi_matches_random_both_lose_after_cost": "no edge large enough to pay execution cost",
        "random_abnormally_negative_before_cost": "inspect simulation or execution engine before further research",
        "diagnostic_only": True,
    },
    "safety": RSI2_REVERSAL_SPEC["safety"],
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
        timeframe: str = "1Min",
        on_page: Callable[[int], None] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        output = {symbol: [] for symbol in symbols}
        page_token = None
        while True:
            params: dict[str, Any] = {
                "symbols": ",".join(symbols), "timeframe": timeframe,
                "start": iso(start), "end": iso(end), "feed": feed,
                "adjustment": adjustment, "limit": 10000, "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            page = self.get(f"{self.data_base}/v2/stocks/bars", params)
            page_points = 0
            for symbol, rows in (page.get("bars") or {}).items():
                output.setdefault(symbol, []).extend(rows or [])
                page_points += len(rows or [])
            if on_page is not None:
                on_page(page_points)
            page_token = page.get("next_page_token")
            if not page_token:
                return output

    def calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        result = self.get(
            f"{self.trading_base}/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
        )
        return list(result or [])


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


def orb_opening_snapshot(bars: list[dict[str, Any]], session: str) -> dict[str, Any] | None:
    """Return the five completed 09:30-09:34 New York bars only."""
    expected_date = date.fromisoformat(session)
    selected: dict[int, dict[str, Any]] = {}
    for bar in bars:
        if not bar.get("t"):
            continue
        local = parse_dt(str(bar["t"])).astimezone(NY)
        minute = local.hour * 60 + local.minute
        if local.date() == expected_date and 570 <= minute < 575:
            selected[minute] = bar
    if sorted(selected) != list(range(570, 575)):
        return None
    ordered = [selected[minute] for minute in range(570, 575)]
    opening = float(ordered[0]["o"])
    close = float(ordered[-1]["c"])
    if opening <= 0 or close <= 0:
        return None
    direction = "LONG" if close > opening else "SHORT" if close < opening else "DOJI"
    return {
        "session": session,
        "open": opening,
        "close": close,
        "high": max(float(bar["h"]) for bar in ordered),
        "low": min(float(bar["l"]) for bar in ordered),
        "volume": sum(float(bar.get("v") or 0.0) for bar in ordered),
        "direction": direction,
        "bar_timestamps": [str(bar["t"]) for bar in ordered],
    }


def orb_trade_result(
    bars: list[dict[str, Any]],
    session: str,
    direction: str,
    entry_price: float,
    atr14: float,
    cost_pct_round_trip: float | None = None,
    commission_per_share_per_side: float | None = None,
    session_close: dtime = dtime(16, 0),
) -> dict[str, Any]:
    """Conservative one-minute ORB execution after the completed opening range."""
    if direction not in {"LONG", "SHORT"} or entry_price <= 0 or atr14 <= 0:
        return {"complete": False, "triggered": False, "reason": "invalid_trade_definition"}
    session_date = date.fromisoformat(session)
    close_minute = session_close.hour * 60 + session_close.minute
    regular = []
    for bar in sorted(bars, key=lambda item: str(item.get("t") or "")):
        if not bar.get("t"):
            continue
        local = parse_dt(str(bar["t"])).astimezone(NY)
        minute = local.hour * 60 + local.minute
        if local.date() == session_date and 575 <= minute < close_minute:
            regular.append(bar)
    expected_last_minute = close_minute - 1
    last_local = parse_dt(str(regular[-1]["t"])).astimezone(NY) if regular else None
    complete = bool(last_local) and (last_local.hour * 60 + last_local.minute) >= expected_last_minute
    if not complete:
        return {"complete": False, "triggered": False, "reason": "incomplete_regular_session", "bars": len(regular)}

    trigger_price = float(entry_price)
    stop_distance = 0.10 * float(atr14)
    executed_entry = None
    stop_price = None
    trigger_index = None
    exit_price = None
    exit_reason = None
    conservative_same_bar_stop = False
    for index, bar in enumerate(regular):
        high = float(bar["h"])
        low = float(bar["l"])
        open_price = float(bar["o"])
        triggered = high >= trigger_price if direction == "LONG" else low <= trigger_price
        if trigger_index is None and triggered:
            trigger_index = index
            executed_entry = max(trigger_price, open_price) if direction == "LONG" else min(trigger_price, open_price)
            stop_price = executed_entry - stop_distance if direction == "LONG" else executed_entry + stop_distance
        if trigger_index is not None:
            assert stop_price is not None
            stopped = low <= stop_price if direction == "LONG" else high >= stop_price
            if stopped:
                exit_price = min(stop_price, open_price) if direction == "LONG" else max(stop_price, open_price)
                exit_reason = "STOP"
                conservative_same_bar_stop = index == trigger_index
                break
    if trigger_index is None:
        return {"complete": True, "triggered": False, "reason": "entry_not_triggered", "bars": len(regular)}
    assert executed_entry is not None and stop_price is not None
    if exit_price is None:
        exit_price = float(regular[-1]["c"])
        exit_reason = "END_OF_DAY"

    gross_per_share = exit_price - executed_entry if direction == "LONG" else executed_entry - exit_price
    if cost_pct_round_trip is not None:
        cost_per_share = executed_entry * float(cost_pct_round_trip) / 100.0
        cost_rule = f"{float(cost_pct_round_trip):g}% round trip"
    else:
        per_side = float(commission_per_share_per_side or 0.0)
        cost_per_share = 2.0 * per_side
        cost_rule = f"${per_side:g} per share per side"
    net_per_share = gross_per_share - cost_per_share
    return {
        "complete": True,
        "triggered": True,
        "direction": direction,
        "entry_trigger_price": round(float(trigger_price), 8),
        "entry_price": round(float(executed_entry), 8),
        "entry_ts": str(regular[trigger_index]["t"]),
        "stop_price": round(float(stop_price), 8),
        "stop_distance": round(float(stop_distance), 8),
        "exit_price": round(float(exit_price), 8),
        "exit_reason": exit_reason,
        "exit_ts": str(regular[-1]["t"] if exit_reason == "END_OF_DAY" else regular[index]["t"]),
        "conservative_same_bar_stop": conservative_same_bar_stop,
        "gross_pnl_per_share": round(float(gross_per_share), 8),
        "cost_per_share": round(float(cost_per_share), 8),
        "cost_rule": cost_rule,
        "net_pnl_per_share": round(float(net_per_share), 8),
        "net_return_pct": round(float(net_per_share / executed_entry * 100.0), 6),
        "net_r_multiple": round(float(net_per_share / stop_distance), 6),
        "bars": len(regular),
    }


def orb_slot_daily_return(selected_trades: list[dict[str, Any]], slots: int) -> float | None:
    """Equal-weight selected slots; untriggered selections remain cash."""
    if slots <= 0 or any(not trade.get("complete") for trade in selected_trades):
        return None
    return sum(float(trade.get("net_return_pct") or 0.0) for trade in selected_trades if trade.get("triggered")) / slots


def daily_return_statistics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        [row for row in rows if row.get("daily_return_pct") is not None],
        key=lambda row: str(row.get("session") or ""),
    )
    values = [float(row["daily_return_pct"]) for row in ordered]
    result = return_statistics(values)
    result.update({
        "sessions": len(ordered),
        "active_days": sum(abs(value) > 1e-12 for value in values),
        "positive_days": sum(value > 0 for value in values),
        "negative_days": sum(value < 0 for value in values),
        "flat_days": sum(abs(value) <= 1e-12 for value in values),
        "first_session": ordered[0]["session"] if ordered else None,
        "last_session": ordered[-1]["session"] if ordered else None,
    })
    return result


def daily_breakout_signal_metrics(
    bars: list[dict[str, Any]],
    signal_session: str,
) -> dict[str, Any] | None:
    """Build a completed-day breakout signal without reading a future row."""
    target = date.fromisoformat(signal_session)
    dated: list[tuple[date, dict[str, Any]]] = []
    for bar in bars:
        if not bar.get("t"):
            continue
        local_date = parse_dt(str(bar["t"])).astimezone(NY).date()
        if local_date <= target:
            dated.append((local_date, bar))
    dated.sort(key=lambda item: item[0])
    current_matches = [bar for row_date, bar in dated if row_date == target]
    prior = [bar for row_date, bar in dated if row_date < target]
    if len(current_matches) != 1 or len(prior) < 60:
        return None
    current = current_matches[0]
    previous60 = prior[-60:]
    previous20 = prior[-20:]
    signal_close = float(current["c"])
    signal_volume = float(current.get("v") or 0.0)
    if signal_close <= 0 or signal_volume < 0:
        return None
    average_volume20 = mean(float(row.get("v") or 0.0) for row in previous20)
    if average_volume20 <= 0:
        return None
    prior_high20 = max(float(row["h"]) for row in previous20)
    average_dollar_volume60 = mean(
        float(row.get("v") or 0.0) * float(row.get("c") or 0.0)
        for row in previous60
    )
    atr_rows = prior[-14:] + [current]
    true_ranges = []
    for previous, row in zip(atr_rows, atr_rows[1:]):
        previous_close = float(previous["c"])
        high = float(row["h"])
        low = float(row["l"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(true_ranges) != 14:
        return None
    volume_ratio = signal_volume / average_volume20
    return {
        "signal_session": signal_session,
        "signal_open": float(current["o"]),
        "signal_high": float(current["h"]),
        "signal_low": float(current["l"]),
        "signal_close": signal_close,
        "signal_volume": signal_volume,
        "prior_high20": prior_high20,
        "average_volume20": average_volume20,
        "volume_ratio20": volume_ratio,
        "average_dollar_volume60": average_dollar_volume60,
        "atr14": float(mean(true_ranges)),
        "price_pass": (
            DAILY_BREAKOUT_SPEC["signal"]["price_min_inclusive"]
            <= signal_close
            <= DAILY_BREAKOUT_SPEC["signal"]["price_max_inclusive"]
        ),
        "liquidity_pass": (
            average_dollar_volume60
            >= DAILY_BREAKOUT_SPEC["signal"]["minimum_average_dollar_volume_60_sessions"]
        ),
        "breakout_pass": signal_close > prior_high20,
        "volume_pass": volume_ratio >= DAILY_BREAKOUT_SPEC["signal"]["minimum_volume_ratio"],
    }


def daily_breakout_trade_result(
    bars: list[dict[str, Any]],
    entry_session: str,
    final_session: str,
    atr14: float,
    session_closes: dict[str, dtime],
    cost_pct_round_trip: float = 0.25,
) -> dict[str, Any]:
    """Enter at the next regular open and follow a one-ATR stop minute by minute."""
    if atr14 <= 0 or entry_session > final_session:
        return {"complete": False, "triggered": False, "reason": "invalid_trade_definition"}
    entry_day = date.fromisoformat(entry_session)
    final_day = date.fromisoformat(final_session)
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bar in sorted(bars, key=lambda item: str(item.get("t") or "")):
        if not bar.get("t"):
            continue
        local = parse_dt(str(bar["t"])).astimezone(NY)
        session = local.date().isoformat()
        close = session_closes.get(session)
        if close is None or not (entry_day <= local.date() <= final_day):
            continue
        minute = local.hour * 60 + local.minute
        close_minute = close.hour * 60 + close.minute
        if 570 <= minute < close_minute:
            by_session[session].append(bar)
    required_sessions = sorted(
        session for session in session_closes
        if entry_session <= session <= final_session
    )
    if not required_sessions or required_sessions[0] != entry_session or required_sessions[-1] != final_session:
        return {"complete": False, "triggered": False, "reason": "missing_calendar_session"}
    for session in required_sessions:
        rows = by_session.get(session, [])
        close = session_closes[session]
        expected_last = close.hour * 60 + close.minute - 1
        if not rows:
            return {"complete": False, "triggered": False, "reason": f"missing_session_bars:{session}"}
        first_local = parse_dt(str(rows[0]["t"])).astimezone(NY)
        last_local = parse_dt(str(rows[-1]["t"])).astimezone(NY)
        if session == entry_session and first_local.hour * 60 + first_local.minute != 570:
            return {
                "complete": True,
                "triggered": False,
                "reason": "no_executable_0930_opening_print",
            }
        if last_local.hour * 60 + last_local.minute < expected_last:
            return {"complete": False, "triggered": False, "reason": f"incomplete_session:{session}"}

    entry_bar = by_session[entry_session][0]
    entry_price = float(entry_bar["o"])
    if not (
        DAILY_BREAKOUT_SPEC["signal"]["price_min_inclusive"]
        <= entry_price
        <= DAILY_BREAKOUT_SPEC["signal"]["price_max_inclusive"]
    ):
        return {
            "complete": True,
            "triggered": False,
            "reason": "entry_open_outside_price_range",
            "entry_open": entry_price,
        }
    stop_price = entry_price - float(atr14)
    exit_price = None
    exit_ts = None
    exit_reason = None
    for session in required_sessions:
        for bar in by_session[session]:
            open_price = float(bar["o"])
            low = float(bar["l"])
            if low <= stop_price:
                exit_price = min(stop_price, open_price)
                exit_ts = str(bar["t"])
                exit_reason = "STOP"
                break
        if exit_price is not None:
            break
    if exit_price is None:
        final_bar = by_session[final_session][-1]
        exit_price = float(final_bar["c"])
        exit_ts = str(final_bar["t"])
        exit_reason = "TIME_EXIT"
    gross_per_share = exit_price - entry_price
    cost_per_share = entry_price * float(cost_pct_round_trip) / 100.0
    net_per_share = gross_per_share - cost_per_share
    return {
        "complete": True,
        "triggered": True,
        "direction": "LONG",
        "entry_session": entry_session,
        "final_session": final_session,
        "entry_price": round(entry_price, 8),
        "entry_ts": str(entry_bar["t"]),
        "stop_price": round(stop_price, 8),
        "stop_distance": round(float(atr14), 8),
        "exit_price": round(float(exit_price), 8),
        "exit_ts": exit_ts,
        "exit_reason": exit_reason,
        "gross_pnl_per_share": round(float(gross_per_share), 8),
        "cost_per_share": round(float(cost_per_share), 8),
        "cost_rule": f"{float(cost_pct_round_trip):g}% round trip",
        "net_pnl_per_share": round(float(net_per_share), 8),
        "net_return_pct": round(float(net_per_share / entry_price * 100.0), 6),
        "net_r_multiple": round(float(net_per_share / atr14), 6),
    }


def daily_breakout_policy_slots(policy: str) -> int:
    if policy == "daily_1":
        return int(DAILY_BREAKOUT_SPEC["signal"]["daily_rank_count"])
    if policy == "daily_2":
        return int(DAILY_BREAKOUT_SPEC["signal"]["daily_rank_count"]) * 2
    raise ValueError(f"Unknown daily-breakout policy: {policy}")


def wilder_rsi(values: list[float], period: int = 2) -> list[float | None]:
    """Wilder RSI aligned to values; no future observation is consulted."""
    output: list[float | None] = [None] * len(values)
    if period < 1 or len(values) <= period:
        return output
    gains = [max(0.0, values[index] - values[index - 1]) for index in range(1, period + 1)]
    losses = [max(0.0, values[index - 1] - values[index]) for index in range(1, period + 1)]
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    def value() -> float:
        if average_loss <= 1e-15:
            return 100.0 if average_gain > 1e-15 else 50.0
        relative_strength = average_gain / average_loss
        return 100.0 - 100.0 / (1.0 + relative_strength)

    output[period] = value()
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
        output[index] = value()
    return output


def _daily_bar_session(row: dict[str, Any]) -> str:
    return str(row.get("t") or "")[:10]


def rsi2_candidate_records(
    symbol: str,
    adjusted_rows: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    session_map: dict[str, dict[str, str]],
    maximum_rsi_exclusive: float = 10.0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build compact causal RSI(2)<10 candidates; RSI<5 is filtered later."""
    adjusted = sorted(adjusted_rows, key=lambda row: str(row.get("t") or ""))
    raw_by_session = {_daily_bar_session(row): row for row in raw_rows}
    adjusted_by_session = {_daily_bar_session(row): row for row in adjusted}
    index_by_session = {_daily_bar_session(row): index for index, row in enumerate(adjusted)}
    closes = [float(row["c"]) for row in adjusted]
    close_prefix = [0.0]
    dollar_volume_prefix = [0.0]
    for row, close_value in zip(adjusted, closes):
        close_prefix.append(close_prefix[-1] + close_value)
        dollar_volume_prefix.append(
            dollar_volume_prefix[-1] + close_value * float(row.get("v") or 0.0)
        )
    rsi_values = wilder_rsi(closes, 2)
    price_min = float(RSI2_REVERSAL_SPEC["universe"]["price_min_inclusive"])
    price_max = float(RSI2_REVERSAL_SPEC["universe"]["price_max_inclusive"])
    minimum_dollar_volume = float(
        RSI2_REVERSAL_SPEC["universe"]["minimum_average_dollar_volume_60_prior_sessions"]
    )
    minimum_history = int(RSI2_REVERSAL_SPEC["data"]["minimum_warmup_sessions"])
    development_start = str(RSI2_REVERSAL_SPEC["data"]["development_start"])
    holdout_end = str(RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"])
    cost = float(RSI2_REVERSAL_SPEC["execution"]["decision_cost_pct_round_trip"])
    counters = defaultdict(int)
    records: list[dict[str, Any]] = []

    for signal_session, mapping in session_map.items():
        if signal_session < development_start or signal_session > holdout_end:
            continue
        index = index_by_session.get(signal_session)
        if index is None:
            counters["missing_signal_bar"] += 1
            continue
        if index + 1 < minimum_history or index < 199 or index < 60:
            counters["insufficient_warmup"] += 1
            continue
        close = closes[index]
        sma200 = (close_prefix[index + 1] - close_prefix[index - 199]) / 200.0
        rsi2 = rsi_values[index]
        if close <= sma200 or rsi2 is None or rsi2 >= maximum_rsi_exclusive:
            continue
        average_dollar_volume60 = (
            dollar_volume_prefix[index] - dollar_volume_prefix[index - 60]
        ) / 60.0
        if average_dollar_volume60 < minimum_dollar_volume:
            counters["liquidity_rejected"] += 1
            continue

        entry_session = mapping["entry_session"]
        final_session = mapping["final_session"]
        entry_index = index_by_session.get(entry_session)
        final_index = index_by_session.get(final_session)
        signal_raw = raw_by_session.get(signal_session)
        entry_raw = raw_by_session.get(entry_session)
        final_raw = raw_by_session.get(final_session)
        entry_adjusted = adjusted_by_session.get(entry_session)
        final_adjusted = adjusted_by_session.get(final_session)
        if any(item is None for item in (signal_raw, entry_raw, final_raw, entry_adjusted, final_adjusted)):
            counters["missing_execution_bar"] += 1
            continue
        if entry_index is None or final_index is None or entry_index < 4:
            counters["missing_execution_index"] += 1
            continue
        signal_raw_close = float(signal_raw["c"])
        entry_raw_open = float(entry_raw["o"])
        if not (price_min <= signal_raw_close <= price_max):
            counters["signal_price_rejected"] += 1
            continue
        if not (price_min <= entry_raw_open <= price_max):
            counters["entry_price_rejected"] += 1
            continue

        entry_adjusted_open = float(entry_adjusted["o"])
        entry_day_sma5 = (
            close_prefix[entry_index + 1] - close_prefix[entry_index - 4]
        ) / 5.0
        exit_on_next_open = float(entry_adjusted["c"]) > entry_day_sma5
        if exit_on_next_open:
            exit_adjusted = float(final_adjusted["o"])
            exit_raw = float(final_raw["o"])
            exit_rule = "NEXT_OPEN_AFTER_ENTRY_CLOSE_ABOVE_SMA5"
            observed_highs = [float(entry_adjusted["h"]), exit_adjusted]
            observed_lows = [float(entry_adjusted["l"]), exit_adjusted]
        else:
            exit_adjusted = float(final_adjusted["c"])
            exit_raw = float(final_raw["c"])
            exit_rule = "FORCED_SECOND_SESSION_CLOSE"
            observed_highs = [float(entry_adjusted["h"]), float(final_adjusted["h"])]
            observed_lows = [float(entry_adjusted["l"]), float(final_adjusted["l"])]
        if entry_adjusted_open <= 0 or exit_adjusted <= 0:
            counters["invalid_execution_price"] += 1
            continue
        gross_return = (exit_adjusted / entry_adjusted_open - 1.0) * 100.0
        records.append({
            "symbol": symbol,
            "signal_session": signal_session,
            "entry_session": entry_session,
            "exit_session": final_session,
            "rsi2": round(float(rsi2), 8),
            "signal_adjusted_close": round(close, 8),
            "signal_raw_close": round(signal_raw_close, 8),
            "sma200": round(sma200, 8),
            "average_dollar_volume60": round(average_dollar_volume60, 2),
            "entry_raw_open": round(entry_raw_open, 8),
            "entry_adjusted_open": round(entry_adjusted_open, 8),
            "entry_day_adjusted_close": round(float(entry_adjusted["c"]), 8),
            "entry_day_sma5": round(entry_day_sma5, 8),
            "exit_raw_price": round(exit_raw, 8),
            "exit_adjusted_price": round(exit_adjusted, 8),
            "exit_rule": exit_rule,
            "gross_return_pct": round(gross_return, 8),
            "net_return_pct": round(gross_return - cost, 8),
            "mfe_pct": round((max(observed_highs) / entry_adjusted_open - 1.0) * 100.0, 8),
            "mae_pct": round((min(observed_lows) / entry_adjusted_open - 1.0) * 100.0, 8),
            "cost_pct_round_trip": cost,
        })
        counters["eligible_rsi_below_10"] += 1
        if rsi2 < 5.0:
            counters["eligible_rsi_below_5"] += 1
    return records, dict(counters)


def select_rsi2_trades(candidates: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        if float(row["rsi2"]) < threshold:
            by_session[str(row["signal_session"])].append(row)
    selected: list[dict[str, Any]] = []
    limit = int(RSI2_REVERSAL_SPEC["signal"]["daily_rank_count"])
    for session in sorted(by_session):
        ordered = sorted(
            by_session[session],
            key=lambda row: (float(row["rsi2"]), -float(row["average_dollar_volume60"]), row["symbol"]),
        )
        for rank, row in enumerate(ordered[:limit], 1):
            selected.append({**row, "rank": rank, "policy_rsi_threshold": threshold})
    return selected


def rsi2_trade_statistics(trades: list[dict[str, Any]], capital_slots: int = 6) -> dict[str, Any]:
    returns = [float(row["net_return_pct"]) for row in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    by_exit: dict[str, float] = defaultdict(float)
    for row in trades:
        by_exit[str(row["exit_session"])] += float(row["net_return_pct"]) / capital_slots
    daily_values = list(by_exit.values())
    return {
        "trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(len(wins) / len(returns) * 100.0, 6) if returns else None,
        "average_net_trade_return_pct": round(mean(returns), 8) if returns else None,
        "median_net_trade_return_pct": round(median(returns), 8) if returns else None,
        "profit_factor": round(gross_profit / gross_loss, 8) if gross_loss > 1e-15 else None,
        "gross_profit_points": round(gross_profit, 8),
        "gross_loss_points": round(gross_loss, 8),
        "active_exit_days": len(daily_values),
        "average_active_day_portfolio_return_pct": round(mean(daily_values), 8) if daily_values else None,
        "total_portfolio_return_points": round(sum(daily_values), 8),
        "average_mfe_pct": round(mean(float(row["mfe_pct"]) for row in trades), 8) if trades else None,
        "average_mae_pct": round(mean(float(row["mae_pct"]) for row in trades), 8) if trades else None,
        "exit_rule_counts": {
            "next_open_after_sma5": sum(row["exit_rule"] == "NEXT_OPEN_AFTER_ENTRY_CLOSE_ABOVE_SMA5" for row in trades),
            "forced_second_close": sum(row["exit_rule"] == "FORCED_SECOND_SESSION_CLOSE" for row in trades),
        },
        "capital_slots": capital_slots,
    }


def sanity_hash_key(session: str, symbol: str) -> int:
    digest = hashlib.sha256(f"IPR-RSI2-SANITY|{session}|{symbol}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def sanity_add_candidate(
    reservoirs: dict[str, list[dict[str, Any]]],
    moments: dict[str, dict[str, Any]],
    row: dict[str, Any],
    reservoir_size: int = 64,
) -> None:
    session = str(row["signal_session"])
    gross = float(row["gross_return_pct"])
    moment = moments.setdefault(session, {
        "eligible_count": 0,
        "sum_gross_return_pct": 0.0,
        "sum_gross_return_sq": 0.0,
        "costs": {
            str(cost): {"sum": 0.0, "positive": 0.0, "negative": 0.0, "wins": 0}
            for cost in RSI2_SANITY_SPEC["simulation"]["cost_sensitivity_pct_round_trip"]
        },
    })
    moment["eligible_count"] += 1
    moment["sum_gross_return_pct"] += gross
    moment["sum_gross_return_sq"] += gross * gross
    for cost in RSI2_SANITY_SPEC["simulation"]["cost_sensitivity_pct_round_trip"]:
        net = gross - float(cost)
        block = moment["costs"][str(cost)]
        block["sum"] += net
        if net > 0:
            block["positive"] += net
            block["wins"] += 1
        elif net < 0:
            block["negative"] += net

    compact = {
        "symbol": row["symbol"],
        "signal_session": session,
        "gross_return_pct": gross,
        "rsi2": float(row["rsi2"]),
        "exit_rule": row["exit_rule"],
        "hash_key": sanity_hash_key(session, str(row["symbol"])),
    }
    current = {item["symbol"]: item for item in reservoirs.get(session, [])}
    current[str(compact["symbol"])] = compact
    reservoirs[session] = sorted(current.values(), key=lambda item: (item["hash_key"], item["symbol"]))[:reservoir_size]


def sanity_exact_baseline_statistics(
    moments: dict[str, dict[str, Any]],
    slot_counts: dict[str, int],
    start: str,
    end: str,
    cost: float,
) -> dict[str, Any]:
    draws = 0
    expected_sum = 0.0
    expected_positive = 0.0
    expected_negative = 0.0
    expected_wins = 0.0
    missing_sessions = []
    for session, slots in slot_counts.items():
        if not (start <= session <= end):
            continue
        block = moments.get(session)
        if not block or int(block.get("eligible_count") or 0) < slots:
            missing_sessions.append(session)
            continue
        count = int(block["eligible_count"])
        cost_block = block["costs"][str(cost)]
        draws += slots
        expected_sum += slots * float(cost_block["sum"]) / count
        expected_positive += slots * float(cost_block["positive"]) / count
        expected_negative += slots * float(cost_block["negative"]) / count
        expected_wins += slots * int(cost_block["wins"]) / count
    return {
        "expected_draws": draws,
        "missing_or_too_small_sessions": len(missing_sessions),
        "average_net_trade_return_pct": round(expected_sum / draws, 8) if draws else None,
        "expected_win_rate_pct": round(expected_wins / draws * 100.0, 8) if draws else None,
        "expected_profit_factor": round(expected_positive / -expected_negative, 8) if expected_negative < -1e-15 else None,
        "expected_total_six_slot_portfolio_points": round(expected_sum / 6.0, 8),
    }


def sanity_strategy_statistics(
    records: list[dict[str, Any]], start: str, end: str, cost: float
) -> dict[str, Any]:
    values = [
        float(row["gross_return_pct"]) - cost
        for row in records
        if start <= str(row["signal_session"]) <= end
    ]
    positives = [value for value in values if value > 0]
    negatives = [value for value in values if value < 0]
    return {
        "trades": len(values),
        "average_net_trade_return_pct": round(mean(values), 8) if values else None,
        "win_rate_pct": round(len(positives) / len(values) * 100.0, 8) if values else None,
        "profit_factor": round(sum(positives) / -sum(negatives), 8) if negatives else None,
        "total_six_slot_portfolio_points": round(sum(values) / 6.0, 8),
    }


def sanity_monte_carlo(
    reservoirs: dict[str, list[dict[str, Any]]],
    slot_counts: dict[str, int],
    strategy_records: list[dict[str, Any]],
    start: str,
    end: str,
    iterations: int = 1000,
    seed: int = 20260905,
) -> dict[str, Any]:
    sessions = [
        session for session in sorted(slot_counts)
        if start <= session <= end and len(reservoirs.get(session, [])) >= slot_counts[session]
    ]
    covered_sessions = set(sessions)
    matched_strategy_records = [
        row for row in strategy_records if str(row["signal_session"]) in covered_sessions
    ]
    costs = [float(value) for value in RSI2_SANITY_SPEC["simulation"]["cost_sensitivity_pct_round_trip"]]
    simulated: dict[str, dict[str, list[float]]] = {
        str(cost): {"average": [], "profit_factor": [], "portfolio_points": []} for cost in costs
    }
    rng = np.random.default_rng(seed)
    for _ in range(iterations):
        gross_returns = []
        for session in sessions:
            pool = reservoirs[session]
            count = int(slot_counts[session])
            indices = rng.choice(len(pool), size=count, replace=False)
            gross_returns.extend(float(pool[int(index)]["gross_return_pct"]) for index in indices)
        for cost in costs:
            values = [value - cost for value in gross_returns]
            positives = sum(value for value in values if value > 0)
            negative = -sum(value for value in values if value < 0)
            block = simulated[str(cost)]
            block["average"].append(mean(values) if values else float("nan"))
            block["profit_factor"].append(positives / negative if negative > 1e-15 else float("inf"))
            block["portfolio_points"].append(sum(values) / 6.0)
    output = {}
    for cost in costs:
        key = str(cost)
        averages = np.asarray(simulated[key]["average"], dtype=float)
        factors = np.asarray(simulated[key]["profit_factor"], dtype=float)
        points = np.asarray(simulated[key]["portfolio_points"], dtype=float)
        finite_factors = factors[np.isfinite(factors)]
        observed = sanity_strategy_statistics(matched_strategy_records, start, end, cost)
        observed_average = observed["average_net_trade_return_pct"]
        observed_pf = observed["profit_factor"]
        output[key] = {
            "strategy_observed": observed,
            "random_iterations": iterations,
            "random_average_net_trade_return_pct": {
                "mean": round(float(np.mean(averages)), 8),
                "median": round(float(np.median(averages)), 8),
                "p05": round(float(np.quantile(averages, .05)), 8),
                "p95": round(float(np.quantile(averages, .95)), 8),
            },
            "random_profit_factor": {
                "mean_finite": round(float(np.mean(finite_factors)), 8) if len(finite_factors) else None,
                "median_finite": round(float(np.median(finite_factors)), 8) if len(finite_factors) else None,
                "p05_finite": round(float(np.quantile(finite_factors, .05)), 8) if len(finite_factors) else None,
                "p95_finite": round(float(np.quantile(finite_factors, .95)), 8) if len(finite_factors) else None,
                "infinite_iterations": int(np.isinf(factors).sum()),
            },
            "random_total_six_slot_portfolio_points": {
                "mean": round(float(np.mean(points)), 8),
                "p05": round(float(np.quantile(points, .05)), 8),
                "p95": round(float(np.quantile(points, .95)), 8),
            },
            "strategy_average_percentile_vs_random": round(
                float(np.mean(averages <= float(observed_average))) * 100.0, 4
            ) if observed_average is not None else None,
            "strategy_pf_percentile_vs_random": round(
                float(np.mean(factors <= float(observed_pf))) * 100.0, 4
            ) if observed_pf is not None else None,
        }
    return {
        "covered_signal_sessions": len(sessions),
        "excluded_signal_sessions": sum(
            start <= session <= end and session not in covered_sessions for session in slot_counts
        ),
        "reservoir_size": RSI2_SANITY_SPEC["simulation"]["deterministic_uniform_reservoir_per_session"],
        "cost_sensitivity": output,
    }


def pack_checkpoint(value: Any) -> str:
    raw = json_compact(value).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=6)).decode("ascii")


def unpack_checkpoint(value: str) -> Any:
    return json.loads(gzip.decompress(base64.b64decode(value)).decode("utf-8"))


def reference_wilder_rsi_at(values: list[float], index: int, period: int = 2) -> float | None:
    if index < period:
        return None
    gains = []
    losses = []
    for position in range(1, period + 1):
        change = values[position] - values[position - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    for position in range(period + 1, index + 1):
        change = values[position] - values[position - 1]
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
    if average_loss <= 1e-15:
        return 100.0 if average_gain > 1e-15 else 50.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def sanity_indicator_check(symbol: str, adjusted_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(adjusted_rows, key=lambda row: str(row.get("t") or ""))
    if len(rows) < 260:
        return {"symbol": symbol, "checked_points": 0, "reason": "insufficient_rows"}
    closes = [float(row["c"]) for row in rows]
    production_rsi = wilder_rsi(closes, 2)
    span = len(rows) - 250
    base = sanity_hash_key("INDICATOR", symbol) % span
    indices = sorted({249 + int((base + offset * 193) % span) for offset in range(3)})
    rsi_differences = []
    sma_differences = []
    for index in indices:
        reference_rsi = reference_wilder_rsi_at(closes, index, 2)
        rsi_differences.append(abs(float(production_rsi[index]) - float(reference_rsi)))
        direct_sma = sum(closes[index - 199:index + 1]) / 200.0
        prefix = [0.0]
        for value in closes[:index + 1]:
            prefix.append(prefix[-1] + value)
        prefix_sma = (prefix[index + 1] - prefix[index - 199]) / 200.0
        sma_differences.append(abs(direct_sma - prefix_sma))
    return {
        "symbol": symbol,
        "checked_points": len(indices),
        "max_rsi2_difference": max(rsi_differences, default=None),
        "max_sma200_difference": max(sma_differences, default=None),
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
        self.orb_lock = threading.RLock()
        self.orb_thread: threading.Thread | None = None
        self.orb_stop_event = threading.Event()
        self.orb_path: str | None = None
        self.orb_state: dict[str, Any] = {
            "status": "IDLE",
            "phase": "NOT_STARTED",
            "message": "Liquid daily ORB research has not started",
            "research_id": LIQUID_DAILY_ORB_SPEC["research_id"],
            "alerts_enabled": False,
            "orders_enabled": False,
            "updated_at": iso(),
        }
        self.breakout_lock = threading.RLock()
        self.breakout_thread: threading.Thread | None = None
        self.breakout_stop_event = threading.Event()
        self.breakout_path: str | None = None
        self.breakout_state: dict[str, Any] = {
            "status": "IDLE",
            "phase": "NOT_STARTED",
            "message": "Daily breakout with volume research has not started",
            "research_id": DAILY_BREAKOUT_SPEC["research_id"],
            "alerts_enabled": False,
            "orders_enabled": False,
            "updated_at": iso(),
        }
        self.rsi2_lock = threading.RLock()
        self.rsi2_thread: threading.Thread | None = None
        self.rsi2_stop_event = threading.Event()
        self.rsi2_path: str | None = None
        self.rsi2_request_pages = 0
        self.rsi2_bar_points = 0
        self.rsi2_state: dict[str, Any] = {
            "status": "IDLE",
            "phase": "NOT_STARTED",
            "message": "Multi-year RSI(2) reversal research has not started",
            "research_id": RSI2_REVERSAL_SPEC["research_id"],
            "alerts_enabled": False,
            "orders_enabled": False,
            "updated_at": iso(),
        }
        self.sanity_lock = threading.RLock()
        self.sanity_thread: threading.Thread | None = None
        self.sanity_stop_event = threading.Event()
        self.sanity_path: str | None = None
        self.sanity_request_pages = 0
        self.sanity_bar_points = 0
        self.sanity_state: dict[str, Any] = {
            "status": "IDLE", "phase": "NOT_STARTED",
            "message": "RSI2 matched-random sanity check has not started",
            "audit_id": RSI2_SANITY_SPEC["audit_id"],
            "alerts_enabled": False, "orders_enabled": False,
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

    def orb_key(self, suffix: str) -> str:
        return self.key(f"liquid_daily_orb:v1:{suffix}")

    def breakout_key(self, suffix: str) -> str:
        return self.key(f"daily_breakout_volume:v1:{suffix}")

    def rsi2_key(self, suffix: str) -> str:
        return self.key(f"rsi2_short_term_reversal:v1:{suffix}")

    def sanity_key(self, suffix: str) -> str:
        return self.key(f"rsi2_sanity_check:v1:{suffix}")

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
        if self.orb_thread and self.orb_thread.is_alive():
            return False, "Liquid Daily ORB Research is running"
        if self.breakout_thread and self.breakout_thread.is_alive():
            return False, "Daily Breakout Research is running"
        if self.rsi2_thread and self.rsi2_thread.is_alive():
            return False, "RSI2 Reversal Research is running"
        if self.sanity_thread and self.sanity_thread.is_alive():
            return False, "RSI2 Sanity Check is running"
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
        if self.orb_thread and self.orb_thread.is_alive():
            return False, "Liquid Daily ORB Research is running"
        if self.breakout_thread and self.breakout_thread.is_alive():
            return False, "Daily Breakout Research is running"
        if self.rsi2_thread and self.rsi2_thread.is_alive():
            return False, "RSI2 Reversal Research is running"
        if self.sanity_thread and self.sanity_thread.is_alive():
            return False, "RSI2 Sanity Check is running"
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
            if (
                (self.audit_thread and self.audit_thread.is_alive())
                or (self.export_thread and self.export_thread.is_alive())
                or (self.orb_thread and self.orb_thread.is_alive())
                or (self.breakout_thread and self.breakout_thread.is_alive())
                or (self.rsi2_thread and self.rsi2_thread.is_alive())
                or (self.sanity_thread and self.sanity_thread.is_alive())
            ):
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

    def _set_orb_progress(self, **updates: Any) -> None:
        with self.orb_lock:
            self.orb_state.update(updates)
            self.orb_state["updated_at"] = iso()
            snapshot = dict(self.orb_state)
        if self.redis.configured:
            self.redis.set_json(self.orb_key("status"), snapshot)

    @staticmethod
    def _orb_daily_metrics(rows: list[dict[str, Any]], session: str) -> dict[str, float] | None:
        target = date.fromisoformat(session)
        prior = []
        for row in rows:
            if not row.get("t"):
                continue
            row_date = parse_dt(str(row["t"])).astimezone(NY).date()
            if row_date < target:
                prior.append((row_date, row))
        prior.sort(key=lambda item: item[0])
        if len(prior) < 61:
            return None
        latest = [row for _, row in prior[-61:]]
        atr_rows = latest[-15:]
        true_ranges = []
        for previous, current in zip(atr_rows, atr_rows[1:]):
            previous_close = float(previous["c"])
            high = float(current["h"])
            low = float(current["l"])
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        if len(true_ranges) != 14:
            return None
        last60 = latest[-60:]
        average_volume14 = mean(float(row.get("v") or 0.0) for row in latest[-14:])
        average_dollar_volume60 = mean(
            float(row.get("v") or 0.0) * float(row.get("c") or 0.0) for row in last60
        )
        return {
            "atr14": float(mean(true_ranges)),
            "average_share_volume14": float(average_volume14),
            "average_dollar_volume60": float(average_dollar_volume60),
            "previous_close": float(latest[-1]["c"]),
            "history_sessions": float(len(prior)),
        }

    @staticmethod
    def _orb_manifest_parts(manifest: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        symbols = sorted({str(symbol).upper() for symbol in manifest.get("symbols", []) if SYMBOL_RE.fullmatch(str(symbol).upper())})
        sessions = [str(value) for value in manifest.get("sessions", [])]
        development = [str(value) for value in manifest.get("development_sessions", [])]
        holdout = [str(value) for value in manifest.get("holdout_sessions", [])]
        if len(sessions) != 60 or len(development) != 45 or len(holdout) != 15:
            raise RuntimeError("Liquid ORB requires the frozen 60-session manifest with 45 Development and 15 Holdout sessions")
        if sessions != development + holdout:
            raise RuntimeError("Manifest session order does not match the frozen Development/Holdout partition")
        if not symbols:
            raise RuntimeError("Full-universe symbols are missing from the source manifest")
        return symbols, development, holdout

    def _orb_daily_candidate_batches(
        self,
        symbols: list[str],
        sessions: list[str],
    ) -> list[dict[str, list[dict[str, Any]]]]:
        batches = list(chunks(symbols, 100))
        first_day = date.fromisoformat(sessions[0])
        last_day = date.fromisoformat(sessions[-1])
        start = datetime.combine(first_day - timedelta(days=120), dtime(0, 0), tzinfo=NY).astimezone(UTC)
        end = datetime.combine(last_day + timedelta(days=1), dtime(0, 0), tzinfo=NY).astimezone(UTC)
        payloads: list[dict[str, list[dict[str, Any]]]] = []
        for batch_index, symbol_batch in enumerate(batches):
            if self.orb_stop_event.is_set():
                raise InterruptedError("pause_requested")
            key = self.orb_key(f"daily_batch:{batch_index}")
            stored = self.redis.get_json(key, None)
            if stored is not None:
                payloads.append(stored)
                continue
            fetched = self.alpaca.bars(
                symbol_batch, start, end, feed="sip", adjustment="raw", timeframe="1Day"
            )
            payload: dict[str, list[dict[str, Any]]] = {session: [] for session in sessions}
            for symbol in symbol_batch:
                rows = fetched.get(symbol, [])
                for session in sessions:
                    metrics = self._orb_daily_metrics(rows, session)
                    if metrics is None:
                        continue
                    paper_possible = (
                        metrics["average_share_volume14"] >= LIQUID_DAILY_ORB_SPEC["paper"]["minimum_average_share_volume"]
                        and metrics["atr14"] > LIQUID_DAILY_ORB_SPEC["paper"]["minimum_atr_exclusive"]
                    )
                    user_possible = (
                        metrics["average_dollar_volume60"]
                        >= LIQUID_DAILY_ORB_SPEC["user_primary"]["minimum_average_dollar_volume_60_sessions"]
                        and metrics["atr14"] > LIQUID_DAILY_ORB_SPEC["user_primary"]["minimum_atr_exclusive"]
                    )
                    if paper_possible or user_possible:
                        payload[session].append({"symbol": symbol, **metrics})
            self.redis.set_json(key, payload)
            payloads.append(payload)
            self._set_orb_progress(
                status="RUNNING",
                phase="DAILY_SCREEN",
                message="Building causal daily liquidity and ATR screens",
                completed_daily_batches=batch_index + 1,
                total_daily_batches=len(batches),
            )
        return payloads

    def _orb_opening_sessions(
        self,
        symbols: list[str],
        evaluation_sessions: list[str],
    ) -> list[str]:
        first_day = date.fromisoformat(evaluation_sessions[0])
        last_day = date.fromisoformat(evaluation_sessions[-1])
        calendar = self.alpaca.calendar(first_day - timedelta(days=35), last_day)
        all_sessions = sorted(str(item.get("date")) for item in calendar if item.get("date"))
        self.redis.set_json(
            self.orb_key("calendar"),
            {
                str(item["date"]): {"open": str(item.get("open") or "09:30"), "close": str(item.get("close") or "16:00")}
                for item in calendar if item.get("date")
            },
        )
        prior = [session for session in all_sessions if session < evaluation_sessions[0]][-14:]
        if len(prior) != 14 or any(session not in all_sessions for session in evaluation_sessions):
            raise RuntimeError("Unable to build 14 exact opening-volume warmup sessions")
        research_sessions = prior + evaluation_sessions
        for index, session in enumerate(research_sessions):
            if self.orb_stop_event.is_set():
                raise InterruptedError("pause_requested")
            key = self.orb_key(f"opening:{session}")
            if self.redis.get_json(key, None) is not None:
                continue
            local_day = date.fromisoformat(session)
            start = datetime.combine(local_day, dtime(9, 30), tzinfo=NY).astimezone(UTC)
            end = datetime.combine(local_day, dtime(9, 35), tzinfo=NY).astimezone(UTC)
            snapshots: dict[str, dict[str, Any]] = {}
            for symbol_batch in chunks(symbols, 100):
                fetched = self.alpaca.bars(
                    symbol_batch, start, end, feed="sip", adjustment="raw", timeframe="1Min"
                )
                for symbol, rows in fetched.items():
                    snapshot = orb_opening_snapshot(rows, session)
                    if snapshot is not None:
                        snapshots[symbol] = snapshot
            self.redis.set_json(key, snapshots)
            self._set_orb_progress(
                status="RUNNING",
                phase="OPENING_RANGES",
                message=f"Collected causal five-minute opening ranges for {session}",
                completed_opening_sessions=index + 1,
                total_opening_sessions=len(research_sessions),
                opening_symbols=len(symbols),
            )
        return research_sessions

    @staticmethod
    def _orb_sharia_allowed_symbols(assets: list[dict[str, Any]]) -> set[str]:
        return {
            str(asset.get("symbol") or "").upper()
            for asset in assets
            if IndependentPriorityRadar._allowed_asset(asset)
        }

    @staticmethod
    def _orb_market_cap_from_record(record: Any) -> float | None:
        if not isinstance(record, dict):
            return None
        for name in ("market_cap", "marketCap", "market_capitalization", "marketCapitalization"):
            value = record.get(name)
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
        return None

    def _orb_current_market_caps(self, symbols: set[str]) -> dict[str, float]:
        """Diagnostic only. Current snapshots must never decide a historical trade."""
        output: dict[str, float] = {}
        for key in self.float_keys:
            raw = self.redis.command("GET", key)
            if raw is None:
                continue
            try:
                document = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            for symbol in symbols - set(output):
                value = find_symbol_record(document, symbol)
                market_cap = self._orb_market_cap_from_record(value)
                if market_cap is not None:
                    output[symbol] = market_cap
        return output

    def _orb_session_result(
        self,
        session: str,
        daily_rows: list[dict[str, Any]],
        opening_sessions: list[str],
        sharia_allowed: set[str],
        current_market_caps: dict[str, float],
    ) -> dict[str, Any]:
        current_index = opening_sessions.index(session)
        prior_sessions = opening_sessions[current_index - 14:current_index]
        current = self.redis.get_json(self.orb_key(f"opening:{session}"), {})
        prior = [self.redis.get_json(self.orb_key(f"opening:{value}"), {}) for value in prior_sessions]
        paper_candidates = []
        user_candidates = []
        missing_opening_history = 0
        for daily in daily_rows:
            symbol = daily["symbol"]
            opening = current.get(symbol)
            previous_volumes = [snapshot.get(symbol, {}).get("volume") for snapshot in prior]
            if opening is None or any(value is None for value in previous_volumes):
                missing_opening_history += 1
                continue
            average_opening_volume = mean(float(value) for value in previous_volumes)
            if average_opening_volume <= 0:
                continue
            rvol = float(opening["volume"]) / average_opening_volume
            candidate = {
                **daily,
                "opening": opening,
                "opening_relative_volume": rvol,
                "average_prior14_opening_volume": average_opening_volume,
                "current_market_cap_diagnostic": current_market_caps.get(symbol),
            }
            if (
                float(opening["open"]) > LIQUID_DAILY_ORB_SPEC["paper"]["price_min_exclusive"]
                and daily["average_share_volume14"] >= LIQUID_DAILY_ORB_SPEC["paper"]["minimum_average_share_volume"]
                and daily["atr14"] > LIQUID_DAILY_ORB_SPEC["paper"]["minimum_atr_exclusive"]
                and rvol >= LIQUID_DAILY_ORB_SPEC["paper"]["minimum_opening_relative_volume"]
                and opening["direction"] in {"LONG", "SHORT"}
            ):
                paper_candidates.append(candidate)
            if (
                LIQUID_DAILY_ORB_SPEC["user_primary"]["price_min_inclusive"]
                <= float(opening["open"])
                <= LIQUID_DAILY_ORB_SPEC["user_primary"]["price_max_inclusive"]
                and daily["average_dollar_volume60"]
                >= LIQUID_DAILY_ORB_SPEC["user_primary"]["minimum_average_dollar_volume_60_sessions"]
                and daily["atr14"] > LIQUID_DAILY_ORB_SPEC["user_primary"]["minimum_atr_exclusive"]
                and rvol >= LIQUID_DAILY_ORB_SPEC["user_primary"]["minimum_opening_relative_volume"]
                and opening["direction"] == "LONG"
                and symbol in sharia_allowed
            ):
                user_candidates.append(candidate)
        paper_candidates.sort(key=lambda row: (-row["opening_relative_volume"], row["symbol"]))
        user_candidates.sort(key=lambda row: (-row["opening_relative_volume"], row["symbol"]))
        paper_selected = paper_candidates[:LIQUID_DAILY_ORB_SPEC["paper"]["daily_rank_count"]]
        user_selected = user_candidates[:LIQUID_DAILY_ORB_SPEC["user_primary"]["primary_daily_rank_count"]]
        selected_symbols = sorted({row["symbol"] for row in paper_selected + user_selected})
        local_day = date.fromisoformat(session)
        calendar = self.redis.get_json(self.orb_key("calendar"), {})
        close_text = str((calendar.get(session) or {}).get("close") or "16:00")
        close_hour, close_minute = [int(value) for value in close_text.split(":")[:2]]
        session_close = dtime(close_hour, close_minute)
        start = datetime.combine(local_day, dtime(9, 35), tzinfo=NY).astimezone(UTC)
        end = datetime.combine(local_day, session_close, tzinfo=NY).astimezone(UTC)
        full_bars: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in selected_symbols}
        for symbol_batch in chunks(selected_symbols, 100):
            fetched = self.alpaca.bars(
                symbol_batch, start, end, feed="sip", adjustment="raw", timeframe="1Min"
            )
            for symbol, bars in fetched.items():
                full_bars[symbol] = bars

        def calculate(selected: list[dict[str, Any]], user_cost: bool) -> list[dict[str, Any]]:
            trades = []
            for candidate in selected:
                opening = candidate["opening"]
                direction = "LONG" if user_cost else opening["direction"]
                entry = float(opening["high"] if direction == "LONG" else opening["low"])
                trade = orb_trade_result(
                    full_bars.get(candidate["symbol"], []),
                    session,
                    direction,
                    entry,
                    float(candidate["atr14"]),
                    cost_pct_round_trip=(
                        LIQUID_DAILY_ORB_SPEC["user_primary"]["decision_cost_pct_round_trip"] if user_cost else None
                    ),
                    commission_per_share_per_side=(
                        None if user_cost else LIQUID_DAILY_ORB_SPEC["paper"]["commission_per_share_per_side_usd"]
                    ),
                    session_close=session_close,
                )
                trades.append({
                    "symbol": candidate["symbol"],
                    "rank": len(trades) + 1,
                    "opening_relative_volume": round(float(candidate["opening_relative_volume"]), 6),
                    "opening": opening,
                    "atr14": round(float(candidate["atr14"]), 6),
                    "average_share_volume14": round(float(candidate["average_share_volume14"]), 2),
                    "average_dollar_volume60": round(float(candidate["average_dollar_volume60"]), 2),
                    "current_market_cap_diagnostic": candidate.get("current_market_cap_diagnostic"),
                    "trade": trade,
                })
            return trades

        paper_trades = calculate(paper_selected, False)
        user_trades = calculate(user_selected, True)
        paper_results = [row["trade"] for row in paper_trades]
        user_results = [row["trade"] for row in user_trades]
        return {
            "session": session,
            "paper_rule_reference": {
                "eligible_count": len(paper_candidates),
                "selected_count": len(paper_selected),
                "trades": paper_trades,
                "equal_slot_daily_return_pct": orb_slot_daily_return(
                    paper_results, LIQUID_DAILY_ORB_SPEC["paper"]["daily_rank_count"]
                ),
            },
            "user_top3_primary": {
                "eligible_count": len(user_candidates),
                "selected_count": len(user_selected),
                "trades": user_trades,
                "daily_return_pct": orb_slot_daily_return(
                    user_results, LIQUID_DAILY_ORB_SPEC["user_primary"]["primary_daily_rank_count"]
                ),
            },
            "user_top1_diagnostic": {
                "selected_count": min(1, len(user_trades)),
                "trades": user_trades[:1],
                "daily_return_pct": orb_slot_daily_return(user_results[:1], 1),
            },
            "coverage": {
                "daily_screen_count": len(daily_rows),
                "missing_exact_opening_history": missing_opening_history,
                "market_cap_snapshot_available_for_user_eligible": sum(
                    row.get("current_market_cap_diagnostic") is not None for row in user_candidates
                ),
                "current_market_cap_above_intended_floor": sum(
                    float(row.get("current_market_cap_diagnostic") or 0.0)
                    >= LIQUID_DAILY_ORB_SPEC["user_primary"]["intended_minimum_market_cap_usd"]
                    for row in user_candidates
                ),
                "market_cap_filter_applied": False,
            },
        }

    @staticmethod
    def _orb_strategy_rows(results: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
        return [
            {"session": row["session"], "daily_return_pct": row[name].get("daily_return_pct")}
            for row in results
        ]

    def _build_orb_report(
        self,
        development_sessions: list[str],
        holdout_sessions: list[str],
        results: list[dict[str, Any]],
        universe_count: int,
        opening_symbol_count: int,
    ) -> dict[str, Any]:
        by_session = {row["session"]: row for row in results}
        development = [by_session[session] for session in development_sessions if session in by_session]
        holdout = [by_session[session] for session in holdout_sessions if session in by_session]
        blocks = [development_sessions[index:index + 15] for index in range(0, 45, 15)]
        block_reports = []
        block_passes = []
        for index, sessions in enumerate(blocks, 1):
            rows = [by_session[session] for session in sessions if session in by_session]
            stats = daily_return_statistics(self._orb_strategy_rows(rows, "user_top3_primary"))
            profit_factor = stats.get("profit_factor")
            passed = bool(
                profit_factor is not None
                and profit_factor > 1.0
                and (stats.get("average_return_pct") or 0.0) > 0.0
                and stats.get("active_days", 0)
                >= LIQUID_DAILY_ORB_SPEC["evaluation"]["minimum_active_days_per_development_block"]
            )
            block_passes.append(passed)
            block_reports.append({"block": index, "passed": passed, **stats})
        judgment = (
            LIQUID_DAILY_ORB_SPEC["evaluation"]["promising_wording"]
            if len(block_passes) == 3 and all(block_passes)
            else LIQUID_DAILY_ORB_SPEC["evaluation"]["failure_wording"]
        )
        paper_daily = [
            {
                "session": row["session"],
                "daily_return_pct": row["paper_rule_reference"].get("equal_slot_daily_return_pct"),
            }
            for row in results
        ]
        market_cap_coverage = sum(
            row["coverage"].get("market_cap_snapshot_available_for_user_eligible", 0) for row in results
        )
        market_cap_above_floor = sum(
            row["coverage"].get("current_market_cap_above_intended_floor", 0) for row in results
        )
        return {
            "schema": 1,
            "generated_at": iso(),
            "version": VERSION,
            "build": BUILD,
            "live_protocol_id": PROTOCOL_ID,
            "live_protocol_sha256": PROTOCOL_SHA256,
            "research_spec": LIQUID_DAILY_ORB_SPEC,
            "coverage": {
                "universe_symbols": universe_count,
                "opening_screen_symbols": opening_symbol_count,
                "completed_sessions": len(results),
                "development_sessions": len(development),
                "legacy_holdout_sessions": len(holdout),
                "current_market_cap_diagnostic_records": market_cap_coverage,
                "current_market_cap_above_intended_floor_records": market_cap_above_floor,
                "market_cap_filter_applied": False,
                "market_cap_note": "Alpaca has no historical point-in-time market cap. Current snapshots are reported only and never select a historical trade.",
            },
            "paper_rule_reference": {
                "role": "short recent-period rule reference, not a replication of the paper's 2016-2023 portfolio",
                "all_sessions_equal_slot_statistics": daily_return_statistics(paper_daily),
                "development_equal_slot_statistics": daily_return_statistics(
                    [row for row in paper_daily if row["session"] in set(development_sessions)]
                ),
                "legacy_holdout_equal_slot_statistics": daily_return_statistics(
                    [row for row in paper_daily if row["session"] in set(holdout_sessions)]
                ),
            },
            "user_top3_primary": {
                "development_blocks": block_reports,
                "development": daily_return_statistics(self._orb_strategy_rows(development, "user_top3_primary")),
                "legacy_holdout_audit_only": daily_return_statistics(
                    self._orb_strategy_rows(holdout, "user_top3_primary")
                ),
                "all_three_development_blocks_passed": len(block_passes) == 3 and all(block_passes),
            },
            "user_top1_diagnostic": {
                "development": daily_return_statistics(self._orb_strategy_rows(development, "user_top1_diagnostic")),
                "legacy_holdout_audit_only": daily_return_statistics(
                    self._orb_strategy_rows(holdout, "user_top1_diagnostic")
                ),
            },
            "capital_reference": {
                "sar": LIQUID_DAILY_ORB_SPEC["user_primary"]["capital_sar_reference"],
                "development_top3_total_simple_pnl_sar": round(
                    LIQUID_DAILY_ORB_SPEC["user_primary"]["capital_sar_reference"]
                    * sum(
                        float(row["user_top3_primary"].get("daily_return_pct") or 0.0) / 100.0
                        for row in development
                    ),
                    2,
                ),
                "note": "Simple non-compounded reference; fractional-share availability and broker constraints are not assumed.",
            },
            "final_judgment": judgment,
            "deployment_approved": False,
            "legacy_holdout_can_approve_live": False,
            "safety": LIQUID_DAILY_ORB_SPEC["safety"],
        }

    def _materialize_orb_download(self, report: dict[str, Any]) -> str:
        manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {})
        _, development_sessions, holdout_sessions = self._orb_manifest_parts(manifest)
        sessions = development_sessions + holdout_sessions
        results = [
            self.redis.get_json(self.orb_key(f"session:{session}"), None)
            for session in sessions
        ]
        results = [row for row in results if row is not None]
        with tempfile.NamedTemporaryFile(prefix="ipr_liquid_daily_orb_", suffix=".json.gz", delete=False) as temporary:
            path = temporary.name
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as output:
            json.dump({"report": report, "sessions": results}, output, ensure_ascii=False, separators=(",", ":"))
        with self.orb_lock:
            old_path = self.orb_path
            self.orb_path = path
        if old_path and old_path != path and os.path.isfile(old_path):
            try:
                os.unlink(old_path)
            except OSError:
                pass
        return path

    def liquid_daily_orb_loop(self) -> None:
        try:
            manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {})
            universe, development_sessions, holdout_sessions = self._orb_manifest_parts(manifest)
            sessions = development_sessions + holdout_sessions
            self._set_orb_progress(
                status="RUNNING",
                phase="DAILY_SCREEN",
                message="Loading causal daily history for the frozen full universe",
                universe_symbols=len(universe),
                total_sessions=len(sessions),
            )
            daily_payloads = self._orb_daily_candidate_batches(universe, sessions)
            opening_symbols = sorted({
                row["symbol"]
                for payload in daily_payloads
                for rows in payload.values()
                for row in rows
            })
            opening_sessions = self._orb_opening_sessions(opening_symbols, sessions)
            assets = self.alpaca.assets()
            sharia_allowed = self._orb_sharia_allowed_symbols(assets)
            current_market_caps = self._orb_current_market_caps(set(opening_symbols))
            completed = 0
            results = []
            for session in sessions:
                if self.orb_stop_event.is_set():
                    self._set_orb_progress(status="PAUSED", phase="SESSION_EVALUATION", message="Paused safely; press start to resume")
                    return
                key = self.orb_key(f"session:{session}")
                result = self.redis.get_json(key, None)
                if result is None:
                    daily_rows = [row for payload in daily_payloads for row in payload.get(session, [])]
                    result = self._orb_session_result(
                        session,
                        daily_rows,
                        opening_sessions,
                        sharia_allowed,
                        current_market_caps,
                    )
                    self.redis.set_json(key, result)
                results.append(result)
                completed += 1
                self._set_orb_progress(
                    status="RUNNING",
                    phase="SESSION_EVALUATION",
                    message=f"Completed liquid daily ORB session {session}",
                    completed_sessions=completed,
                    total_sessions=len(sessions),
                    remaining_sessions=len(sessions) - completed,
                )
            report = self._build_orb_report(
                development_sessions,
                holdout_sessions,
                results,
                len(universe),
                len(opening_symbols),
            )
            self.redis.set_json(self.orb_key("report"), report)
            path = self._materialize_orb_download(report)
            self._set_orb_progress(
                status="COMPLETED",
                phase="COMPLETED",
                message="Liquid Stocks Daily ORB research is complete",
                completed_sessions=len(sessions),
                total_sessions=len(sessions),
                result_ready=True,
                download_ready=True,
                final_judgment=report["final_judgment"],
                compressed_bytes=os.path.getsize(path),
            )
        except InterruptedError:
            self._set_orb_progress(status="PAUSED", message="Paused safely; press start to resume")
        except Exception as exc:
            logging.exception("Liquid daily ORB research failed")
            self._set_orb_progress(status="ERROR", message=f"{type(exc).__name__}: {exc}", result_ready=False)
        finally:
            with self.orb_lock:
                self.orb_thread = None

    def start_liquid_daily_orb(self) -> tuple[bool, str]:
        if self._within_monitoring_hours(now_utc()):
            return False, "Research is blocked during monitoring hours; retry after 17:30 New York time"
        if not self.redis.configured or not self.alpaca.configured:
            return False, "Redis and Alpaca credentials are required"
        with self.orb_lock:
            if self.orb_thread and self.orb_thread.is_alive():
                return True, "already_running"
            if any(
                thread and thread.is_alive()
                for thread in (self.audit_thread, self.export_thread, self.early_thread, self.breakout_thread, self.rsi2_thread, self.sanity_thread)
            ):
                return False, "another historical job is running"
            self.orb_stop_event.clear()
            stored = self.redis.get_json(self.orb_key("status"), None)
            if stored and stored.get("status") == "COMPLETED":
                self.orb_state = stored
                return False, "already_completed"
            self.orb_state = {
                "status": "STARTING",
                "phase": "DAILY_SCREEN",
                "message": "Preparing frozen 60-session liquid daily ORB research",
                "research_id": LIQUID_DAILY_ORB_SPEC["research_id"],
                "alerts_enabled": False,
                "orders_enabled": False,
                "result_ready": False,
                "updated_at": iso(),
            }
            self.orb_thread = threading.Thread(
                target=self.liquid_daily_orb_loop,
                name="independent-priority-liquid-daily-orb",
                daemon=True,
            )
            self.orb_thread.start()
        return True, "started"

    def pause_liquid_daily_orb(self) -> tuple[bool, str]:
        with self.orb_lock:
            if not self.orb_thread or not self.orb_thread.is_alive():
                return False, "not_running"
            self.orb_stop_event.set()
        return True, "pause_requested"

    def _set_breakout_progress(self, **updates: Any) -> None:
        with self.breakout_lock:
            self.breakout_state.update(updates)
            self.breakout_state["updated_at"] = iso()
            snapshot = dict(self.breakout_state)
        if self.redis.configured:
            self.redis.set_json(self.breakout_key("status"), snapshot)

    @staticmethod
    def _daily_breakout_allowed_asset(asset: dict[str, Any]) -> bool:
        """Conservative current security-master screen; ADR wording is allowed."""
        symbol = str(asset.get("symbol") or "").upper()
        if not SYMBOL_RE.fullmatch(symbol) or not asset.get("tradable", False):
            return False
        if symbol in set(DAILY_BREAKOUT_SPEC["universe"]["explicit_symbol_exclusions"]):
            return False
        name = " ".join(str(asset.get("name") or "").lower().replace("-", " ").split())
        product_issuers = (
            "proshares", "direxion", "ishares", "vanguard", "spdr", "invesco",
            "wisdomtree", "vaneck", "global x", "graniteshares", "yieldmax",
            "roundhill", "defiance", "innovator", "first trust", "flexshares",
            "pimco", "t rex", "simplify", "volatility shares", "rex shares",
        )
        product_terms = (
            " etf", "exchange traded fund", " etn", " exchange traded note",
            " index fund", " income fund", " bond fund", " closed end fund",
            " ultrashort", " ultra short", " leveraged", " inverse",
            " 2x ", " 3x ", " daily bull", " daily bear",
        )
        security_terms = (
            " warrant", " rights", " unit", " preferred", " depositary preferred",
            " acquisition corp", " acquisition co", " blank check", " spac",
        )
        prohibited_business = (
            "casino", "gaming", "betting", "wager", "sportsbook", "fantasy sports",
            "alcohol", "brew", "distill", "spirits", "winery", "tobacco",
            "cannabis", "marijuana", "pork", "swine",
            " bank", "bancorp", "financial services", "insurance", "mortgage",
            "consumer credit", "lending", "real estate investment trust", " reit",
        )
        padded = f" {name} "
        return not any(term in padded for term in product_issuers + product_terms + security_terms + prohibited_business)

    def _daily_breakout_batches(
        self,
        symbols: list[str],
        sessions: list[str],
    ) -> list[dict[str, list[dict[str, Any]]]]:
        batches = list(chunks(symbols, 100))
        first_day = date.fromisoformat(sessions[0])
        last_day = date.fromisoformat(sessions[-1])
        start = datetime.combine(first_day - timedelta(days=150), dtime(0, 0), tzinfo=NY).astimezone(UTC)
        end = datetime.combine(last_day + timedelta(days=10), dtime(0, 0), tzinfo=NY).astimezone(UTC)
        payloads: list[dict[str, list[dict[str, Any]]]] = []
        for batch_index, symbol_batch in enumerate(batches):
            if self.breakout_stop_event.is_set():
                raise InterruptedError("pause_requested")
            key = self.breakout_key(f"daily_batch:{batch_index}")
            stored = self.redis.get_json(key, None)
            if stored is not None:
                payloads.append(stored)
                continue
            fetched = self.alpaca.bars(
                symbol_batch, start, end, feed="sip", adjustment="raw", timeframe="1Day"
            )
            payload: dict[str, list[dict[str, Any]]] = {session: [] for session in sessions}
            for symbol in symbol_batch:
                rows = fetched.get(symbol, [])
                for session in sessions:
                    metrics = daily_breakout_signal_metrics(rows, session)
                    if metrics is None:
                        continue
                    if all(metrics[name] for name in ("price_pass", "liquidity_pass", "breakout_pass", "volume_pass")):
                        payload[session].append({"symbol": symbol, **metrics})
            self.redis.set_json(key, payload)
            payloads.append(payload)
            self._set_breakout_progress(
                status="RUNNING",
                phase="DAILY_SIGNALS",
                message="Building causal completed-day breakout and volume signals",
                completed_daily_batches=batch_index + 1,
                total_daily_batches=len(batches),
            )
        return payloads

    @staticmethod
    def _breakout_session_map(
        evaluation_sessions: list[str],
        calendar: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, str]], dict[str, dtime]]:
        calendar_sessions = sorted(str(item.get("date")) for item in calendar if item.get("date"))
        closes: dict[str, dtime] = {}
        for item in calendar:
            session = str(item.get("date") or "")
            close_text = str(item.get("close") or "16:00")
            hour, minute = [int(value) for value in close_text.split(":")[:2]]
            closes[session] = dtime(hour, minute)
        mapping = {}
        for signal_session in evaluation_sessions:
            if signal_session not in calendar_sessions:
                raise RuntimeError(f"Signal session missing from Alpaca calendar: {signal_session}")
            index = calendar_sessions.index(signal_session)
            if index + 2 >= len(calendar_sessions):
                raise RuntimeError(f"Two forward sessions unavailable after {signal_session}")
            mapping[signal_session] = {
                "entry_session": calendar_sessions[index + 1],
                "daily_2_final_session": calendar_sessions[index + 2],
            }
        return mapping, closes

    def _daily_breakout_session_result(
        self,
        signal_session: str,
        candidates: list[dict[str, Any]],
        session_map: dict[str, dict[str, str]],
        closes: dict[str, dtime],
    ) -> dict[str, Any]:
        candidates = sorted(candidates, key=lambda row: (-float(row["volume_ratio20"]), row["symbol"]))
        selected = candidates[:DAILY_BREAKOUT_SPEC["signal"]["daily_rank_count"]]
        entry_session = session_map[signal_session]["entry_session"]
        daily_2_final = session_map[signal_session]["daily_2_final_session"]
        local_start = date.fromisoformat(entry_session)
        local_end = date.fromisoformat(daily_2_final)
        start = datetime.combine(local_start, dtime(9, 30), tzinfo=NY).astimezone(UTC)
        end = datetime.combine(local_end, closes[daily_2_final], tzinfo=NY).astimezone(UTC)
        symbols = [row["symbol"] for row in selected]
        full_bars: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
        for symbol_batch in chunks(symbols, 100):
            fetched = self.alpaca.bars(
                symbol_batch, start, end, feed="sip", adjustment="raw", timeframe="1Min"
            )
            for symbol, bars in fetched.items():
                full_bars[symbol] = bars
        session_closes = {
            session: closes[session]
            for session in sorted(closes)
            if entry_session <= session <= daily_2_final
        }
        trades = []
        daily_1_results = []
        daily_2_results = []
        for rank, candidate in enumerate(selected, 1):
            bars = full_bars.get(candidate["symbol"], [])
            daily_1 = daily_breakout_trade_result(
                bars,
                entry_session,
                entry_session,
                float(candidate["atr14"]),
                {entry_session: closes[entry_session]},
                DAILY_BREAKOUT_SPEC["execution"]["decision_cost_pct_round_trip"],
            )
            daily_2 = daily_breakout_trade_result(
                bars,
                entry_session,
                daily_2_final,
                float(candidate["atr14"]),
                session_closes,
                DAILY_BREAKOUT_SPEC["execution"]["decision_cost_pct_round_trip"],
            )
            daily_1_results.append(daily_1)
            daily_2_results.append(daily_2)
            trades.append({
                "symbol": candidate["symbol"],
                "rank": rank,
                "signal": {
                    key: round(float(candidate[key]), 6)
                    for key in (
                        "signal_open", "signal_high", "signal_low", "signal_close",
                        "signal_volume", "prior_high20", "average_volume20",
                        "volume_ratio20", "average_dollar_volume60", "atr14",
                    )
                },
                "daily_1": daily_1,
                "daily_2": daily_2,
            })
        return {
            "signal_session": signal_session,
            "entry_session": entry_session,
            "daily_2_final_session": daily_2_final,
            "eligible_count": len(candidates),
            "selected_count": len(selected),
            "trades": trades,
            "daily_1": {
                "portfolio_slots": daily_breakout_policy_slots("daily_1"),
                "daily_return_pct": orb_slot_daily_return(
                    daily_1_results, daily_breakout_policy_slots("daily_1")
                ),
            },
            "daily_2": {
                "portfolio_slots": daily_breakout_policy_slots("daily_2"),
                "daily_return_pct": orb_slot_daily_return(
                    daily_2_results, daily_breakout_policy_slots("daily_2")
                ),
            },
        }

    @staticmethod
    def _breakout_policy_rows(results: list[dict[str, Any]], policy: str) -> list[dict[str, Any]]:
        return [
            {"session": row["signal_session"], "daily_return_pct": row[policy].get("daily_return_pct")}
            for row in results
        ]

    @staticmethod
    def _profit_factor_pass(stats: dict[str, Any], threshold: float) -> bool:
        value = stats.get("profit_factor")
        return bool(
            (value is not None and float(value) >= threshold)
            or (value is None and stats.get("positive_days", 0) > 0 and stats.get("negative_days", 0) == 0)
        )

    def _evaluate_breakout_policy(
        self,
        policy: str,
        development_sessions: list[str],
        holdout_sessions: list[str],
        by_session: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        block_reports = []
        block_passes = []
        final_field = "entry_session" if policy == "daily_1" else "daily_2_final_session"
        for index in range(3):
            sessions = development_sessions[index * 15:(index + 1) * 15]
            all_rows = [by_session[session] for session in sessions if session in by_session]
            rows = [row for row in all_rows if str(row.get(final_field) or row["signal_session"]) <= sessions[-1]]
            stats = daily_return_statistics(self._breakout_policy_rows(rows, policy))
            passed = bool(
                self._profit_factor_pass(stats, 1.0 + 1e-12)
                and float(stats.get("average_return_pct") or 0.0) > 0.0
                and int(stats.get("active_days") or 0)
                >= DAILY_BREAKOUT_SPEC["evaluation"]["minimum_active_days_per_block"]
            )
            block_passes.append(passed)
            block_reports.append({
                "block": index + 1,
                "passed": passed,
                "purged_boundary_signals": len(all_rows) - len(rows),
                **stats,
            })
        all_development = [by_session[session] for session in development_sessions if session in by_session]
        development = [
            row for row in all_development
            if str(row.get(final_field) or row["signal_session"]) <= development_sessions[-1]
        ]
        holdout = [by_session[session] for session in holdout_sessions if session in by_session]
        development_stats = daily_return_statistics(self._breakout_policy_rows(development, policy))
        holdout_stats = daily_return_statistics(self._breakout_policy_rows(holdout, policy))
        pooled_pass = bool(
            self._profit_factor_pass(
                development_stats,
                DAILY_BREAKOUT_SPEC["evaluation"]["minimum_pooled_profit_factor"],
            )
            and float(development_stats.get("average_return_pct") or 0.0)
            >= DAILY_BREAKOUT_SPEC["evaluation"]["minimum_pooled_average_net_return_pct"]
            and int(development_stats.get("active_days") or 0)
            >= DAILY_BREAKOUT_SPEC["evaluation"]["minimum_active_days_full_development"]
        )
        promising = len(block_passes) == 3 and all(block_passes) and pooled_pass
        return {
            "policy": policy,
            "development_blocks": block_reports,
            "development": development_stats,
            "purged_development_boundary_signals": len(all_development) - len(development),
            "pooled_thresholds_passed": pooled_pass,
            "all_development_blocks_passed": len(block_passes) == 3 and all(block_passes),
            "legacy_holdout_audit_only": holdout_stats,
            "judgment": f"PROMISING_{policy.upper()}_SHADOW_ONLY" if promising else "NO_STABLE_EDGE",
            "promising_shadow_only": promising,
            "deployment_approved": False,
        }

    def _build_daily_breakout_report(
        self,
        development_sessions: list[str],
        holdout_sessions: list[str],
        results: list[dict[str, Any]],
        source_universe_count: int,
        clean_universe_count: int,
    ) -> dict[str, Any]:
        by_session = {row["signal_session"]: row for row in results}
        daily_1 = self._evaluate_breakout_policy("daily_1", development_sessions, holdout_sessions, by_session)
        daily_2 = self._evaluate_breakout_policy("daily_2", development_sessions, holdout_sessions, by_session)
        promising = [name for name, report in (("DAILY_1", daily_1), ("DAILY_2", daily_2)) if report["promising_shadow_only"]]
        final_judgment = (
            "NO_STABLE_EDGE" if not promising
            else f"PROMISING_{'_AND_'.join(promising)}_SHADOW_ONLY"
        )
        selected = [trade for row in results for trade in row.get("trades", [])]
        return {
            "schema": 1,
            "generated_at": iso(),
            "version": VERSION,
            "build": BUILD,
            "live_protocol_id": PROTOCOL_ID,
            "live_protocol_sha256": PROTOCOL_SHA256,
            "research_spec": DAILY_BREAKOUT_SPEC,
            "coverage": {
                "source_universe_symbols": source_universe_count,
                "clean_current_asset_universe_symbols": clean_universe_count,
                "completed_signal_sessions": len(results),
                "development_sessions": sum(session in by_session for session in development_sessions),
                "legacy_holdout_sessions": sum(session in by_session for session in holdout_sessions),
                "eligible_signals": sum(int(row.get("eligible_count") or 0) for row in results),
                "selected_slots": len(selected),
                "daily_1_valid_entries": sum(bool(trade["daily_1"].get("triggered")) for trade in selected),
                "cancelled_entry_price_slots": sum(
                    trade["daily_1"].get("reason") == "entry_open_outside_price_range" for trade in selected
                ),
                "cancelled_missing_0930_print_slots": sum(
                    trade["daily_1"].get("reason") == "no_executable_0930_opening_print" for trade in selected
                ),
                "current_classification_note": "Current Alpaca asset descriptions are used only to remove products/prohibited names; this is not a historical point-in-time or full Sharia ratio screen.",
            },
            "daily_1_primary": daily_1,
            "daily_2_independent": daily_2,
            "capital_reference": {
                "sar": 2000.0,
                "daily_1_development_simple_pnl_sar": round(
                    2000.0 * float(daily_1["development"].get("total_return_points") or 0.0) / 100.0, 2
                ),
                "daily_2_development_simple_pnl_sar": round(
                    2000.0 * float(daily_2["development"].get("total_return_points") or 0.0) / 100.0, 2
                ),
                "note": "Simple non-compounded reference; no leverage and no assumed fractional-share support.",
            },
            "final_judgment": final_judgment,
            "deployment_approved": False,
            "legacy_holdout_can_approve_live": False,
            "forward_sessions_required_after_promising_result": 20,
            "safety": DAILY_BREAKOUT_SPEC["safety"],
        }

    def _materialize_daily_breakout_download(self, report: dict[str, Any]) -> str:
        manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {})
        _, development_sessions, holdout_sessions = self._orb_manifest_parts(manifest)
        sessions = development_sessions + holdout_sessions
        results = [self.redis.get_json(self.breakout_key(f"session:{session}"), None) for session in sessions]
        results = [row for row in results if row is not None]
        with tempfile.NamedTemporaryFile(prefix="ipr_daily_breakout_volume_", suffix=".json.gz", delete=False) as temporary:
            path = temporary.name
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as output:
            json.dump({"report": report, "sessions": results}, output, ensure_ascii=False, separators=(",", ":"))
        with self.breakout_lock:
            old_path = self.breakout_path
            self.breakout_path = path
        if old_path and old_path != path and os.path.isfile(old_path):
            try:
                os.unlink(old_path)
            except OSError:
                pass
        return path

    def daily_breakout_loop(self) -> None:
        try:
            manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {})
            source_universe, development_sessions, holdout_sessions = self._orb_manifest_parts(manifest)
            sessions = development_sessions + holdout_sessions
            assets = self.alpaca.assets()
            clean_assets = {
                str(asset.get("symbol") or "").upper()
                for asset in assets
                if self._daily_breakout_allowed_asset(asset)
            }
            universe = sorted(set(source_universe) & clean_assets)
            if not universe:
                raise RuntimeError("Clean daily-breakout universe is empty")
            first_day = date.fromisoformat(sessions[0])
            last_day = date.fromisoformat(sessions[-1])
            calendar = self.alpaca.calendar(first_day, last_day + timedelta(days=14))
            session_map, closes = self._breakout_session_map(sessions, calendar)
            self.redis.set_json(self.breakout_key("calendar"), {
                "mapping": session_map,
                "closes": {session: close.strftime("%H:%M") for session, close in closes.items()},
            })
            self._set_breakout_progress(
                status="RUNNING",
                phase="DAILY_SIGNALS",
                message="Loading causal daily bars for the clean operating-company universe",
                source_universe_symbols=len(source_universe),
                clean_universe_symbols=len(universe),
                total_sessions=len(sessions),
            )
            daily_payloads = self._daily_breakout_batches(universe, sessions)
            results = []
            for index, session in enumerate(sessions):
                if self.breakout_stop_event.is_set():
                    self._set_breakout_progress(
                        status="PAUSED", phase="SESSION_EVALUATION",
                        message="Paused safely; press start to resume",
                    )
                    return
                key = self.breakout_key(f"session:{session}")
                result = self.redis.get_json(key, None)
                if result is None:
                    candidates = [row for payload in daily_payloads for row in payload.get(session, [])]
                    result = self._daily_breakout_session_result(
                        session, candidates, session_map, closes
                    )
                    self.redis.set_json(key, result)
                results.append(result)
                self._set_breakout_progress(
                    status="RUNNING",
                    phase="SESSION_EVALUATION",
                    message=f"Completed daily breakout signal session {session}",
                    completed_sessions=index + 1,
                    total_sessions=len(sessions),
                    remaining_sessions=len(sessions) - index - 1,
                )
            report = self._build_daily_breakout_report(
                development_sessions, holdout_sessions, results,
                len(source_universe), len(universe),
            )
            self.redis.set_json(self.breakout_key("report"), report)
            path = self._materialize_daily_breakout_download(report)
            self._set_breakout_progress(
                status="COMPLETED",
                phase="COMPLETED",
                message="Daily breakout with volume research is complete",
                completed_sessions=len(sessions),
                total_sessions=len(sessions),
                result_ready=True,
                download_ready=True,
                final_judgment=report["final_judgment"],
                compressed_bytes=os.path.getsize(path),
            )
        except InterruptedError:
            self._set_breakout_progress(status="PAUSED", message="Paused safely; press start to resume")
        except Exception as exc:
            logging.exception("Daily breakout research failed")
            self._set_breakout_progress(status="ERROR", message=f"{type(exc).__name__}: {exc}", result_ready=False)
        finally:
            with self.breakout_lock:
                self.breakout_thread = None

    def start_daily_breakout(self) -> tuple[bool, str]:
        if self._within_monitoring_hours(now_utc()):
            return False, "Research is blocked during monitoring hours; retry after 17:30 New York time"
        if not self.redis.configured or not self.alpaca.configured:
            return False, "Redis and Alpaca credentials are required"
        with self.breakout_lock:
            if self.breakout_thread and self.breakout_thread.is_alive():
                return True, "already_running"
            if any(
                thread and thread.is_alive()
                for thread in (self.audit_thread, self.export_thread, self.early_thread, self.orb_thread, self.rsi2_thread, self.sanity_thread)
            ):
                return False, "another historical job is running"
            self.breakout_stop_event.clear()
            stored = self.redis.get_json(self.breakout_key("status"), None)
            if stored and stored.get("status") == "COMPLETED":
                self.breakout_state = stored
                return False, "already_completed"
            self.breakout_state = {
                "status": "STARTING",
                "phase": "DAILY_SIGNALS",
                "message": "Preparing frozen Daily-1 and Daily-2 breakout research",
                "research_id": DAILY_BREAKOUT_SPEC["research_id"],
                "alerts_enabled": False,
                "orders_enabled": False,
                "result_ready": False,
                "updated_at": iso(),
            }
            self.breakout_thread = threading.Thread(
                target=self.daily_breakout_loop,
                name="independent-priority-daily-breakout-volume",
                daemon=True,
            )
            self.breakout_thread.start()
        return True, "started"

    def pause_daily_breakout(self) -> tuple[bool, str]:
        with self.breakout_lock:
            if not self.breakout_thread or not self.breakout_thread.is_alive():
                return False, "not_running"
            self.breakout_stop_event.set()
        return True, "pause_requested"

    def _set_rsi2_progress(self, **updates: Any) -> None:
        with self.rsi2_lock:
            self.rsi2_state.update(updates)
            self.rsi2_state["alpaca_pages_completed"] = self.rsi2_request_pages
            self.rsi2_state["daily_bar_points_received"] = self.rsi2_bar_points
            self.rsi2_state["updated_at"] = iso()
            snapshot = dict(self.rsi2_state)
        if self.redis.configured:
            self.redis.set_json(self.rsi2_key("status"), snapshot)

    def _rsi2_page_received(self, points: int) -> None:
        with self.rsi2_lock:
            self.rsi2_request_pages += 1
            self.rsi2_bar_points += int(points)

    @staticmethod
    def _rsi2_session_map(calendar: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        sessions = sorted({str(item.get("date")) for item in calendar if item.get("date")})
        mapping: dict[str, dict[str, str]] = {}
        for index, session in enumerate(sessions[:-2]):
            if RSI2_REVERSAL_SPEC["data"]["development_start"] <= session <= RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"]:
                mapping[session] = {
                    "entry_session": sessions[index + 1],
                    "final_session": sessions[index + 2],
                }
        return mapping

    def _rsi2_data_probe(self, universe: list[str]) -> dict[str, Any]:
        stored = self.redis.get_json(self.rsi2_key("data_probe"), None)
        if stored and stored.get("passed"):
            return stored
        preferred = ["AAPL", "AMD", "F", "GE", "INTC", "META", "NVDA", "PFE", "T", "UBER"]
        sample = [symbol for symbol in preferred if symbol in set(universe)]
        for symbol in universe:
            if len(sample) >= 10:
                break
            if symbol not in sample:
                sample.append(symbol)
        if len(sample) < 5:
            raise RuntimeError("RSI2 data probe requires at least five clean symbols")
        start = datetime.fromisoformat(RSI2_REVERSAL_SPEC["data"]["warmup_start"] + "T00:00:00+00:00")
        end = datetime.fromisoformat(RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"] + "T23:59:59+00:00")
        began = time.monotonic()
        adjusted = self.alpaca.bars(
            sample, start, end, feed="sip", adjustment="split", timeframe="1Day",
            on_page=self._rsi2_page_received,
        )
        raw = self.alpaca.bars(
            sample, start, end, feed="sip", adjustment="raw", timeframe="1Day",
            on_page=self._rsi2_page_received,
        )
        coverage = {}
        long_history = 0
        recent_history = 0
        for symbol in sample:
            adjusted_sessions = {_daily_bar_session(row) for row in adjusted.get(symbol, [])}
            raw_sessions = {_daily_bar_session(row) for row in raw.get(symbol, [])}
            overlap = sorted(adjusted_sessions & raw_sessions)
            first = overlap[0] if overlap else None
            last = overlap[-1] if overlap else None
            if first and first <= "2020-01-15" and len(overlap) >= 1200:
                long_history += 1
            if last and last >= "2026-08-28":
                recent_history += 1
            coverage[symbol] = {
                "adjusted_bars": len(adjusted_sessions),
                "raw_bars": len(raw_sessions),
                "overlap_bars": len(overlap),
                "first_session": first,
                "last_session": last,
            }
        passed = long_history >= 5 and recent_history >= 5
        result = {
            "passed": passed,
            "tested_at": iso(),
            "symbols": sample,
            "long_history_symbols": long_history,
            "recent_history_symbols": recent_history,
            "elapsed_seconds": round(time.monotonic() - began, 3),
            "coverage": coverage,
            "requirements": {
                "minimum_symbols_with_1200_overlapping_bars_and_start_by_2020_01_15": 5,
                "minimum_symbols_current_through_2026_08_28": 5,
                "both_split_adjusted_and_raw_streams_required": True,
            },
        }
        self.redis.set_json(self.rsi2_key("data_probe"), result)
        if not passed:
            raise RuntimeError(f"Alpaca multi-year daily data probe failed: {json_compact(result)}")
        return result

    def _rsi2_fetch_batch(
        self,
        batch_index: int,
        symbols: list[str],
        start: datetime,
        end: datetime,
        session_map: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        adjusted = self.alpaca.bars(
            symbols, start, end, feed="sip", adjustment="split", timeframe="1Day",
            on_page=self._rsi2_page_received,
        )
        # Raw bars are fetched separately so historical $10-$60 membership is
        # based on contemporaneous prices, not today's split-adjusted scale.
        raw = self.alpaca.bars(
            symbols, start, end, feed="sip", adjustment="raw", timeframe="1Day",
            on_page=self._rsi2_page_received,
        )
        combined_counters: dict[str, int] = defaultdict(int)
        records: list[dict[str, Any]] = []
        for symbol in symbols:
            symbol_records, counters = rsi2_candidate_records(
                symbol, adjusted.get(symbol, []), raw.get(symbol, []), session_map
            )
            records.extend(symbol_records)
            for name, value in counters.items():
                combined_counters[name] += int(value)
        return {
            "batch_index": batch_index,
            "symbols": symbols,
            "candidate_records": records,
            "counters": dict(combined_counters),
            "raw_bars_persisted": False,
            "completed_at": iso(),
        }

    @staticmethod
    def _rsi2_block_pass(stats: dict[str, Any], minimum_trades: int) -> bool:
        profit_factor = stats.get("profit_factor")
        profitable_without_losses = bool(
            profit_factor is None
            and int(stats.get("winning_trades") or 0) > 0
            and int(stats.get("losing_trades") or 0) == 0
        )
        return bool(
            int(stats.get("trades") or 0) >= minimum_trades
            and (profitable_without_losses or (profit_factor is not None and float(profit_factor) > 1.0))
            and float(stats.get("average_net_trade_return_pct") or 0.0) > 0.0
        )

    def _evaluate_rsi2_policy(
        self,
        candidates: list[dict[str, Any]],
        threshold: float,
        primary: bool,
    ) -> dict[str, Any]:
        selected = select_rsi2_trades(candidates, threshold)
        minimum_block = int(RSI2_REVERSAL_SPEC["evaluation"]["minimum_trades_per_development_block"])
        blocks = []
        block_passes = []
        for index, (start, end) in enumerate(RSI2_REVERSAL_SPEC["evaluation"]["development_blocks"], 1):
            in_block = [row for row in selected if start <= row["signal_session"] <= end]
            complete = [row for row in in_block if row["exit_session"] <= end]
            stats = rsi2_trade_statistics(complete)
            passed = self._rsi2_block_pass(stats, minimum_block)
            block_passes.append(passed)
            blocks.append({
                "block": index,
                "start": start,
                "end": end,
                "passed": passed,
                "purged_boundary_trades": len(in_block) - len(complete),
                **stats,
            })
        development_end = str(RSI2_REVERSAL_SPEC["data"]["development_end"])
        development = [
            row for row in selected
            if RSI2_REVERSAL_SPEC["data"]["development_start"] <= row["signal_session"] <= development_end
            and row["exit_session"] <= development_end
        ]
        holdout = [
            row for row in selected
            if RSI2_REVERSAL_SPEC["data"]["legacy_holdout_start"] <= row["signal_session"]
            <= RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"]
            and row["exit_session"] <= RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"]
        ]
        development_stats = rsi2_trade_statistics(development)
        pooled_profit_factor = development_stats.get("profit_factor")
        pooled_profitable_without_losses = bool(
            pooled_profit_factor is None
            and int(development_stats.get("winning_trades") or 0) > 0
            and int(development_stats.get("losing_trades") or 0) == 0
        )
        pooled_pass = bool(
            int(development_stats.get("trades") or 0) >= 200
            and (
                pooled_profitable_without_losses
                or (pooled_profit_factor is not None and float(pooled_profit_factor) > 1.0)
            )
            and float(development_stats.get("average_net_trade_return_pct") or 0.0) > 0.0
        )
        promising = all(block_passes) and pooled_pass
        return {
            "name": "RSI2_LT_5_PRIMARY" if primary else "RSI2_LT_10_DIAGNOSTIC",
            "threshold_strictly_below": threshold,
            "primary": primary,
            "selected_trades": len(selected),
            "development_blocks": blocks,
            "all_development_blocks_passed": all(block_passes),
            "development": development_stats,
            "pooled_thresholds_passed": pooled_pass,
            "legacy_holdout_audit_only": rsi2_trade_statistics(holdout),
            "promising_shadow_only": promising,
            "judgment": "PROMISING_SHADOW_ONLY" if promising else "NO_STABLE_EDGE",
            "deployment_approved": False,
            "selected_trade_records": selected,
        }

    def _build_rsi2_report(
        self,
        candidates: list[dict[str, Any]],
        probe: dict[str, Any],
        source_universe_count: int,
        clean_universe_count: int,
        batch_count: int,
    ) -> dict[str, Any]:
        primary = self._evaluate_rsi2_policy(candidates, 5.0, True)
        diagnostic = self._evaluate_rsi2_policy(candidates, 10.0, False)
        primary_records = primary.pop("selected_trade_records")
        diagnostic_records = diagnostic.pop("selected_trade_records")
        report = {
            "schema": 1,
            "generated_at": iso(),
            "version": VERSION,
            "build": BUILD,
            "research_spec": RSI2_REVERSAL_SPEC,
            "data_probe": probe,
            "coverage": {
                "source_universe_symbols": source_universe_count,
                "clean_current_asset_universe_symbols": clean_universe_count,
                "completed_symbol_batches": batch_count,
                "compact_candidates_rsi_below_10": len(candidates),
                "alpaca_pages_completed": self.rsi2_request_pages,
                "daily_bar_points_received_in_this_process": self.rsi2_bar_points,
                "raw_daily_bars_saved_to_redis": False,
                "survivorship_warning": RSI2_REVERSAL_SPEC["universe"]["survivorship_warning"],
            },
            "primary_rsi_below_5": primary,
            "diagnostic_rsi_below_10": diagnostic,
            "diagnostic_can_rescue_primary": False,
            "final_judgment": primary["judgment"],
            "deployment_approved": False,
            "legacy_holdout_can_approve_live": False,
            "forward_requirement_if_promising": {
                "minimum_sessions": 10,
                "minimum_completed_trades": 30,
                "maximum_sessions_if_trade_minimum_not_met": 15,
            },
            "safety": RSI2_REVERSAL_SPEC["safety"],
        }
        return {"report": report, "primary_records": primary_records, "diagnostic_records": diagnostic_records}

    def _materialize_rsi2_download(self, bundle: dict[str, Any] | None = None) -> str:
        if bundle is None:
            report = self.redis.get_json(self.rsi2_key("report"), None)
            primary = [value for _, value in self.redis.scan_hash_json(self.rsi2_key("primary_trades"))]
            diagnostic = [value for _, value in self.redis.scan_hash_json(self.rsi2_key("diagnostic_trades"))]
            bundle = {"report": report, "primary_records": primary, "diagnostic_records": diagnostic}
        with tempfile.NamedTemporaryFile(prefix="ipr_rsi2_short_term_reversal_", suffix=".json.gz", delete=False) as temporary:
            path = temporary.name
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as output:
            json.dump(bundle, output, ensure_ascii=False, separators=(",", ":"))
        with self.rsi2_lock:
            old_path = self.rsi2_path
            self.rsi2_path = path
        if old_path and old_path != path and os.path.isfile(old_path):
            try:
                os.unlink(old_path)
            except OSError:
                pass
        return path

    def rsi2_reversal_loop(self) -> None:
        try:
            manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {})
            source_universe = sorted(set(manifest.get("symbols") or []))
            if not source_universe:
                raise RuntimeError("Frozen source universe is missing")
            assets = self.alpaca.assets()
            clean_assets = {
                str(asset.get("symbol") or "").upper()
                for asset in assets
                if self._daily_breakout_allowed_asset(asset)
            }
            universe = sorted(set(source_universe) & clean_assets)
            if not universe:
                raise RuntimeError("Clean RSI2 universe is empty")
            self._set_rsi2_progress(
                status="RUNNING", phase="DATA_PROBE",
                message="Validating multi-year Alpaca SIP daily depth and raw/split streams",
                source_universe_symbols=len(source_universe), clean_universe_symbols=len(universe),
            )
            probe = self._rsi2_data_probe(universe)
            if self.rsi2_stop_event.is_set():
                raise InterruptedError("pause_requested")

            calendar_start = date.fromisoformat(RSI2_REVERSAL_SPEC["data"]["development_start"])
            calendar_end = date.fromisoformat(RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"]) + timedelta(days=14)
            calendar = self.alpaca.calendar(calendar_start, calendar_end)
            session_map = self._rsi2_session_map(calendar)
            if len(session_map) < 1200:
                raise RuntimeError(f"Insufficient Alpaca calendar coverage: {len(session_map)} signal sessions")
            self.redis.set_json(self.rsi2_key("calendar"), session_map)

            batch_size = max(5, min(50, int(os.getenv("IPR_RSI2_SYMBOLS_PER_BATCH", "12"))))
            workers = max(1, min(64, int(os.getenv("IPR_RSI2_MAX_WORKERS", "24"))))
            batches = list(chunks(universe, batch_size))
            start = datetime.fromisoformat(RSI2_REVERSAL_SPEC["data"]["warmup_start"] + "T00:00:00+00:00")
            end = datetime.fromisoformat(
                (date.fromisoformat(RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"]) + timedelta(days=10)).isoformat()
                + "T23:59:59+00:00"
            )
            completed_indices = []
            pending = []
            for index, symbol_batch in enumerate(batches):
                if self.redis.get_json(self.rsi2_key(f"batch:{index}"), None) is None:
                    pending.append((index, symbol_batch))
                else:
                    completed_indices.append(index)
            self._set_rsi2_progress(
                status="RUNNING", phase="HISTORICAL_DAILY_FETCH",
                message="Fetching raw and split-adjusted daily bars in parallel compact batches",
                data_probe_passed=True, total_symbol_batches=len(batches),
                completed_symbol_batches=len(completed_indices), remaining_symbol_batches=len(pending),
                symbols_per_batch=batch_size, parallel_workers=workers,
                alpaca_page_limit=10000, raw_daily_bars_saved_to_redis=False,
            )

            pending_iterator = iter(pending)
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ipr-rsi2-fetch")
            futures = {}
            try:
                for _ in range(min(workers, len(pending))):
                    index, symbol_batch = next(pending_iterator)
                    future = executor.submit(self._rsi2_fetch_batch, index, symbol_batch, start, end, session_map)
                    futures[future] = index
                while futures:
                    if self.rsi2_stop_event.is_set():
                        raise InterruptedError("pause_requested")
                    done, _ = wait(futures, return_when=FIRST_COMPLETED, timeout=2.0)
                    for future in done:
                        index = futures.pop(future)
                        payload = future.result()
                        self.redis.set_json(self.rsi2_key(f"batch:{index}"), payload)
                        completed_indices.append(index)
                        self._set_rsi2_progress(
                            status="RUNNING", phase="HISTORICAL_DAILY_FETCH",
                            message=f"Completed compact RSI2 symbol batch {len(completed_indices)}/{len(batches)}",
                            completed_symbol_batches=len(completed_indices),
                            remaining_symbol_batches=len(batches) - len(completed_indices),
                        )
                        try:
                            next_index, next_symbols = next(pending_iterator)
                        except StopIteration:
                            continue
                        next_future = executor.submit(
                            self._rsi2_fetch_batch, next_index, next_symbols, start, end, session_map
                        )
                        futures[next_future] = next_index
            finally:
                executor.shutdown(wait=True, cancel_futures=True)

            candidates: list[dict[str, Any]] = []
            aggregate_counters: dict[str, int] = defaultdict(int)
            for index in range(len(batches)):
                payload = self.redis.get_json(self.rsi2_key(f"batch:{index}"), None)
                if payload is None:
                    raise RuntimeError(f"Missing completed RSI2 batch {index}")
                candidates.extend(payload.get("candidate_records") or [])
                for name, value in (payload.get("counters") or {}).items():
                    aggregate_counters[name] += int(value)
            self._set_rsi2_progress(
                status="RUNNING", phase="EVALUATION",
                message="Ranking daily candidates and evaluating frozen Development blocks",
                compact_candidates=len(candidates), candidate_counters=dict(aggregate_counters),
            )
            bundle = self._build_rsi2_report(
                candidates, probe, len(source_universe), len(universe), len(batches)
            )
            report = bundle["report"]
            self.redis.set_json(self.rsi2_key("report"), report)
            for row in bundle["primary_records"]:
                field = f"{row['signal_session']}|{row['rank']}|{row['symbol']}"
                self.redis.hset_json(self.rsi2_key("primary_trades"), field, row)
            for row in bundle["diagnostic_records"]:
                field = f"{row['signal_session']}|{row['rank']}|{row['symbol']}"
                self.redis.hset_json(self.rsi2_key("diagnostic_trades"), field, row)
            path = self._materialize_rsi2_download(bundle)
            self._set_rsi2_progress(
                status="COMPLETED", phase="COMPLETED",
                message="Multi-year causal RSI(2) reversal research is complete",
                result_ready=True, download_ready=True,
                final_judgment=report["final_judgment"],
                compressed_bytes=os.path.getsize(path),
            )
        except InterruptedError:
            self._set_rsi2_progress(
                status="PAUSED", message="Paused safely; completed compact batches are resumable"
            )
        except Exception as exc:
            logging.exception("RSI2 reversal research failed")
            self._set_rsi2_progress(
                status="ERROR", message=f"{type(exc).__name__}: {exc}", result_ready=False
            )
        finally:
            with self.rsi2_lock:
                self.rsi2_thread = None

    def start_rsi2_reversal(self) -> tuple[bool, str]:
        if self._within_monitoring_hours(now_utc()):
            return False, "Research is blocked during monitoring hours; retry after 17:30 New York time"
        if not self.redis.configured or not self.alpaca.configured:
            return False, "Redis and Alpaca credentials are required"
        with self.rsi2_lock:
            if self.rsi2_thread and self.rsi2_thread.is_alive():
                return True, "already_running"
            if any(thread and thread.is_alive() for thread in (
                self.audit_thread, self.export_thread, self.early_thread,
                self.orb_thread, self.breakout_thread, self.sanity_thread,
            )):
                return False, "another historical job is running"
            self.rsi2_stop_event.clear()
            stored = self.redis.get_json(self.rsi2_key("status"), None)
            if stored and stored.get("status") == "COMPLETED":
                self.rsi2_state = stored
                return False, "already_completed"
            self.rsi2_state = {
                "status": "STARTING", "phase": "DATA_PROBE",
                "message": "Preparing multi-year RSI2 data probe",
                "research_id": RSI2_REVERSAL_SPEC["research_id"],
                "alerts_enabled": False, "orders_enabled": False,
                "result_ready": False, "updated_at": iso(),
            }
            self.rsi2_thread = threading.Thread(
                target=self.rsi2_reversal_loop,
                name="independent-priority-rsi2-reversal",
                daemon=True,
            )
            self.rsi2_thread.start()
        return True, "started"

    def pause_rsi2_reversal(self) -> tuple[bool, str]:
        with self.rsi2_lock:
            if not self.rsi2_thread or not self.rsi2_thread.is_alive():
                return False, "not_running"
            self.rsi2_stop_event.set()
        return True, "pause_requested"

    def _set_sanity_progress(self, **updates: Any) -> None:
        with self.sanity_lock:
            self.sanity_state.update(updates)
            self.sanity_state["alpaca_pages_completed"] = self.sanity_request_pages
            self.sanity_state["daily_bar_points_received"] = self.sanity_bar_points
            self.sanity_state["updated_at"] = iso()
            snapshot = dict(self.sanity_state)
        if self.redis.configured:
            self.redis.set_json(self.sanity_key("status"), snapshot)

    def _sanity_page_received(self, points: int) -> None:
        with self.sanity_lock:
            self.sanity_request_pages += 1
            self.sanity_bar_points += int(points)

    def _sanity_fetch_batch(
        self,
        batch_index: int,
        symbols: list[str],
        start: datetime,
        end: datetime,
        session_map: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        adjusted = self.alpaca.bars(
            symbols, start, end, feed="sip", adjustment="split", timeframe="1Day",
            on_page=self._sanity_page_received,
        )
        raw = self.alpaca.bars(
            symbols, start, end, feed="sip", adjustment="raw", timeframe="1Day",
            on_page=self._sanity_page_received,
        )
        compact_candidates = []
        checks = []
        for symbol in symbols:
            rows = adjusted.get(symbol, [])
            checks.append(sanity_indicator_check(symbol, rows))
            records, _ = rsi2_candidate_records(
                symbol, rows, raw.get(symbol, []), session_map,
                maximum_rsi_exclusive=101.0,
            )
            compact_candidates.extend({
                "symbol": row["symbol"],
                "signal_session": row["signal_session"],
                "gross_return_pct": row["gross_return_pct"],
                "rsi2": row["rsi2"],
                "exit_rule": row["exit_rule"],
            } for row in records)
        return {
            "batch_index": batch_index,
            "compact_candidates": compact_candidates,
            "indicator_checks": checks,
            "raw_bars_persisted": False,
        }

    def _save_sanity_checkpoint(
        self,
        reservoirs: dict[str, list[dict[str, Any]]],
        moments: dict[str, dict[str, Any]],
        processed_indices: set[int],
        indicator_integrity: dict[str, Any],
    ) -> None:
        generation = f"g{len(processed_indices):04d}_{int(time.time())}"
        years = sorted({session[:4] for session in set(reservoirs) | set(moments)})
        for year in years:
            payload = {
                "reservoirs": {session: rows for session, rows in reservoirs.items() if session.startswith(year)},
                "moments": {session: row for session, row in moments.items() if session.startswith(year)},
            }
            self.redis.set_json(
                self.sanity_key(f"checkpoint:{generation}:{year}"), pack_checkpoint(payload)
            )
        self.redis.set_json(self.sanity_key("checkpoint"), {
            "generation": generation,
            "years": years,
            "processed_indices": sorted(processed_indices),
            "indicator_integrity": indicator_integrity,
            "saved_at": iso(),
        })

    def _load_sanity_checkpoint(
        self,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], set[int], dict[str, Any]]:
        metadata = self.redis.get_json(self.sanity_key("checkpoint"), None)
        if not metadata:
            return {}, {}, set(), {"checked_points": 0, "max_rsi2_difference": 0.0, "max_sma200_difference": 0.0}
        reservoirs: dict[str, list[dict[str, Any]]] = {}
        moments: dict[str, dict[str, Any]] = {}
        generation = str(metadata["generation"])
        for year in metadata.get("years") or []:
            packed = self.redis.get_json(self.sanity_key(f"checkpoint:{generation}:{year}"), None)
            if packed is None:
                raise RuntimeError(f"Missing sanity checkpoint shard {generation}:{year}")
            payload = unpack_checkpoint(packed)
            reservoirs.update(payload.get("reservoirs") or {})
            moments.update(payload.get("moments") or {})
        return (
            reservoirs,
            moments,
            {int(value) for value in metadata.get("processed_indices") or []},
            dict(metadata.get("indicator_integrity") or {}),
        )

    @staticmethod
    def _merge_indicator_integrity(current: dict[str, Any], checks: list[dict[str, Any]]) -> None:
        for check in checks:
            current["checked_points"] = int(current.get("checked_points") or 0) + int(check.get("checked_points") or 0)
            for name in ("max_rsi2_difference", "max_sma200_difference"):
                value = check.get(name)
                if value is not None:
                    current[name] = max(float(current.get(name) or 0.0), float(value))

    def _build_sanity_report(
        self,
        primary_records: list[dict[str, Any]],
        slot_counts: dict[str, int],
        reservoirs: dict[str, list[dict[str, Any]]],
        moments: dict[str, dict[str, Any]],
        integrity: dict[str, Any],
        source_universe_count: int,
        clean_universe_count: int,
        completed_batches: int,
    ) -> dict[str, Any]:
        periods = {
            "development": (
                RSI2_REVERSAL_SPEC["data"]["development_start"],
                RSI2_REVERSAL_SPEC["data"]["development_end"],
            ),
            "legacy_holdout_audit_only": (
                RSI2_REVERSAL_SPEC["data"]["legacy_holdout_start"],
                RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"],
            ),
        }
        comparisons = {}
        for offset, (name, (start, end)) in enumerate(periods.items()):
            exact = {
                str(cost): sanity_exact_baseline_statistics(moments, slot_counts, start, end, float(cost))
                for cost in RSI2_SANITY_SPEC["simulation"]["cost_sensitivity_pct_round_trip"]
            }
            monte_carlo = sanity_monte_carlo(
                reservoirs, slot_counts, primary_records, start, end,
                iterations=int(RSI2_SANITY_SPEC["simulation"]["iterations"]),
                seed=int(RSI2_SANITY_SPEC["simulation"]["seed"]) + offset,
            )
            comparisons[name] = {
                "start": start,
                "end": end,
                "exact_matched_random_expectation": exact,
                "monte_carlo": monte_carlo,
            }
        identity_difference = max((
            abs(float(row["net_return_pct"]) - (float(row["gross_return_pct"]) - .25))
            for row in primary_records
        ), default=0.0)
        integrity["max_return_identity_difference"] = identity_difference
        tolerance = float(RSI2_SANITY_SPEC["integrity"]["maximum_allowed_indicator_difference"])
        integrity["passed"] = bool(
            int(integrity.get("checked_points") or 0) > 0
            and float(integrity.get("max_rsi2_difference") or 0.0) <= tolerance
            and float(integrity.get("max_sma200_difference") or 0.0) <= tolerance
            and identity_difference <= tolerance
        )
        development_cost = comparisons["development"]["monte_carlo"]["cost_sensitivity"]["0.25"]
        percentile = float(development_cost["strategy_average_percentile_vs_random"])
        strategy_gross = comparisons["development"]["monte_carlo"]["cost_sensitivity"]["0.0"][
            "strategy_observed"
        ]["average_net_trade_return_pct"]
        random_gross = comparisons["development"]["exact_matched_random_expectation"]["0.0"]["average_net_trade_return_pct"]
        if not integrity["passed"]:
            judgment = "ENGINE_REVIEW_REQUIRED"
        elif percentile <= 5.0:
            judgment = "RSI_FILTER_UNDERPERFORMS_MATCHED_RANDOM"
        elif float(strategy_gross) <= 0 and float(random_gross) <= 0:
            judgment = "NO_SHORT_HORIZON_EDGE_IN_MATCHED_UNIVERSE"
        elif percentile < 25.0:
            judgment = "RSI_FILTER_NOT_BETTER_THAN_RANDOM"
        else:
            judgment = "RSI_FILTER_ADDS_SELECTION_VALUE_EXIT_POLICY_STILL_UNPROVEN"
        return {
            "schema": 1,
            "generated_at": iso(),
            "version": VERSION,
            "build": BUILD,
            "audit_spec": RSI2_SANITY_SPEC,
            "coverage": {
                "source_universe_symbols": source_universe_count,
                "clean_current_asset_universe_symbols": clean_universe_count,
                "completed_symbol_batches": completed_batches,
                "matched_signal_sessions": len(slot_counts),
                "primary_strategy_records": len(primary_records),
                "reservoir_sessions": len(reservoirs),
                "exact_moment_sessions": len(moments),
                "alpaca_pages_completed_in_this_process": self.sanity_request_pages,
                "daily_bar_points_received_in_this_process": self.sanity_bar_points,
                "raw_daily_bars_saved_to_redis": False,
            },
            "integrity": integrity,
            "comparisons": comparisons,
            "final_judgment": judgment,
            "diagnostic_only": True,
            "deployment_approved": False,
            "safety": RSI2_SANITY_SPEC["safety"],
        }

    def _materialize_sanity_download(self, report: dict[str, Any] | None = None) -> str:
        report = report or self.redis.get_json(self.sanity_key("report"), None)
        with tempfile.NamedTemporaryFile(prefix="ipr_rsi2_sanity_check_", suffix=".json.gz", delete=False) as temporary:
            path = temporary.name
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as output:
            json.dump({"report": report}, output, ensure_ascii=False, separators=(",", ":"))
        with self.sanity_lock:
            old_path = self.sanity_path
            self.sanity_path = path
        if old_path and old_path != path and os.path.isfile(old_path):
            try:
                os.unlink(old_path)
            except OSError:
                pass
        return path

    def rsi2_sanity_loop(self) -> None:
        try:
            primary_records = [value for _, value in self.redis.scan_hash_json(self.rsi2_key("primary_trades"))]
            if not primary_records:
                raise RuntimeError("Completed RSI2 primary trade records are required")
            slot_counts: dict[str, int] = defaultdict(int)
            for row in primary_records:
                slot_counts[str(row["signal_session"])] += 1
            source_manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {})
            source_universe = sorted(set(source_manifest.get("symbols") or []))
            assets = self.alpaca.assets()
            clean_assets = {
                str(asset.get("symbol") or "").upper()
                for asset in assets if self._daily_breakout_allowed_asset(asset)
            }
            universe = sorted(set(source_universe) & clean_assets)
            if not universe:
                raise RuntimeError("Matched-random clean universe is empty")
            full_session_map = self.redis.get_json(self.rsi2_key("calendar"), None)
            if not full_session_map:
                calendar = self.alpaca.calendar(
                    date.fromisoformat(RSI2_REVERSAL_SPEC["data"]["development_start"]),
                    date.fromisoformat(RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"]) + timedelta(days=14),
                )
                full_session_map = self._rsi2_session_map(calendar)
            session_map = {session: full_session_map[session] for session in slot_counts if session in full_session_map}
            if len(session_map) != len(slot_counts):
                raise RuntimeError("Unable to match every RSI2 signal session to the stored calendar")

            reservoirs, moments, processed, integrity = self._load_sanity_checkpoint()
            batch_size = max(5, min(50, int(os.getenv("IPR_RSI2_SYMBOLS_PER_BATCH", "12"))))
            workers = max(1, min(64, int(os.getenv("IPR_RSI2_MAX_WORKERS", "24"))))
            batches = list(chunks(universe, batch_size))
            pending = [(index, values) for index, values in enumerate(batches) if index not in processed]
            start = datetime.fromisoformat(RSI2_REVERSAL_SPEC["data"]["warmup_start"] + "T00:00:00+00:00")
            end = datetime.fromisoformat(
                (date.fromisoformat(RSI2_REVERSAL_SPEC["data"]["legacy_holdout_end"]) + timedelta(days=10)).isoformat()
                + "T23:59:59+00:00"
            )
            self._set_sanity_progress(
                status="RUNNING", phase="MATCHED_UNIVERSE_FETCH",
                message="Building matched non-RSI daily universe with parallel Alpaca pages",
                total_symbol_batches=len(batches), completed_symbol_batches=len(processed),
                remaining_symbol_batches=len(pending), matched_signal_sessions=len(slot_counts),
                simulations=1000, symbols_per_batch=batch_size, parallel_workers=workers,
            )
            pending_iterator = iter(pending)
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ipr-rsi2-sanity")
            futures = {}
            since_checkpoint = 0
            try:
                for _ in range(min(workers, len(pending))):
                    index, symbols = next(pending_iterator)
                    future = executor.submit(self._sanity_fetch_batch, index, symbols, start, end, session_map)
                    futures[future] = index
                while futures:
                    if self.sanity_stop_event.is_set():
                        raise InterruptedError("pause_requested")
                    done, _ = wait(futures, return_when=FIRST_COMPLETED, timeout=2.0)
                    for future in done:
                        index = futures.pop(future)
                        payload = future.result()
                        for row in payload["compact_candidates"]:
                            sanity_add_candidate(reservoirs, moments, row)
                        self._merge_indicator_integrity(integrity, payload["indicator_checks"])
                        processed.add(index)
                        since_checkpoint += 1
                        if since_checkpoint >= int(RSI2_SANITY_SPEC["storage"]["checkpoint_every_completed_batches"]):
                            self._save_sanity_checkpoint(reservoirs, moments, processed, integrity)
                            since_checkpoint = 0
                        self._set_sanity_progress(
                            status="RUNNING", phase="MATCHED_UNIVERSE_FETCH",
                            message=f"Completed matched-random batch {len(processed)}/{len(batches)}",
                            completed_symbol_batches=len(processed),
                            remaining_symbol_batches=len(batches) - len(processed),
                            reservoir_sessions=len(reservoirs),
                        )
                        try:
                            next_index, next_symbols = next(pending_iterator)
                        except StopIteration:
                            continue
                        next_future = executor.submit(
                            self._sanity_fetch_batch, next_index, next_symbols, start, end, session_map
                        )
                        futures[next_future] = next_index
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
            self._save_sanity_checkpoint(reservoirs, moments, processed, integrity)
            self._set_sanity_progress(
                status="RUNNING", phase="MONTE_CARLO",
                message="Running 1,000 deterministic matched-random portfolios at three cost levels",
            )
            report = self._build_sanity_report(
                primary_records, dict(slot_counts), reservoirs, moments, integrity,
                len(source_universe), len(universe), len(processed),
            )
            self.redis.set_json(self.sanity_key("report"), report)
            path = self._materialize_sanity_download(report)
            self._set_sanity_progress(
                status="COMPLETED", phase="COMPLETED",
                message="RSI2 matched-random sanity check is complete",
                result_ready=True, download_ready=True,
                final_judgment=report["final_judgment"], compressed_bytes=os.path.getsize(path),
            )
        except InterruptedError:
            self._set_sanity_progress(
                status="PAUSED", message="Paused safely; the last atomic checkpoint is resumable"
            )
        except Exception as exc:
            logging.exception("RSI2 sanity check failed")
            self._set_sanity_progress(
                status="ERROR", message=f"{type(exc).__name__}: {exc}", result_ready=False
            )
        finally:
            with self.sanity_lock:
                self.sanity_thread = None

    def start_rsi2_sanity(self) -> tuple[bool, str]:
        if self._within_monitoring_hours(now_utc()):
            return False, "Research is blocked during monitoring hours; retry after 17:30 New York time"
        if not self.redis.configured or not self.alpaca.configured:
            return False, "Redis and Alpaca credentials are required"
        with self.sanity_lock:
            if self.sanity_thread and self.sanity_thread.is_alive():
                return True, "already_running"
            if any(thread and thread.is_alive() for thread in (
                self.audit_thread, self.export_thread, self.early_thread,
                self.orb_thread, self.breakout_thread, self.rsi2_thread,
            )):
                return False, "another historical job is running"
            self.sanity_stop_event.clear()
            stored = self.redis.get_json(self.sanity_key("status"), None)
            if stored and stored.get("status") == "COMPLETED":
                self.sanity_state = stored
                return False, "already_completed"
            self.sanity_state = {
                "status": "STARTING", "phase": "MATCHED_UNIVERSE_FETCH",
                "message": "Preparing frozen matched-random RSI2 sanity check",
                "audit_id": RSI2_SANITY_SPEC["audit_id"],
                "alerts_enabled": False, "orders_enabled": False,
                "result_ready": False, "updated_at": iso(),
            }
            self.sanity_thread = threading.Thread(
                target=self.rsi2_sanity_loop,
                name="independent-priority-rsi2-sanity",
                daemon=True,
            )
            self.sanity_thread.start()
        return True, "started"

    def pause_rsi2_sanity(self) -> tuple[bool, str]:
        with self.sanity_lock:
            if not self.sanity_thread or not self.sanity_thread.is_alive():
                return False, "not_running"
            self.sanity_stop_event.set()
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
            "liquid_daily_orb_research": "/liquid-daily-orb",
            "daily_breakout_volume_research": "/daily-breakout",
            "rsi2_short_term_reversal_research": "/rsi2-reversal",
            "rsi2_matched_random_sanity_check": "/rsi2-sanity",
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


@app.get("/liquid-daily-orb")
def liquid_daily_orb_page():
    return """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>بحث ORB اليومي للأسهم السائلة</title>
    <style>
        body { font-family: system-ui; background: #101114; color: #eee; max-width: 760px; margin: 30px auto; padding: 18px; }
        .box { background: #191b20; border: 1px solid #343741; border-radius: 14px; padding: 20px; margin: 14px 0; }
        input, button { width: 100%; box-sizing: border-box; font-size: 17px; padding: 13px; margin: 7px 0; border-radius: 9px; border: 1px solid #555; }
        button { background: #1d4ed8; color: white; font-weight: 700; }
        a { color: #93c5fd; }
    </style>
</head>
<body>
    <h1>Liquid Stocks Daily ORB Research</h1>
    <div class="box">
        <p>النسخة الأساسية: Long فقط، سعر 10–60 دولار، سيولة 60 جلسة لا تقل عن 20 مليون دولار، Top-3، تكلفة 0.25% وخروج قبل الإغلاق.</p>
        <p>توجد مقارنة تشخيصية مع قواعد ورقة 5-minute ORB. البحث قراءة فقط ولا يرسل تنبيهات أو أوامر.</p>
        <p>التقدم محفوظ ويمكن استكماله. يبدأ بعد 17:30 نيويورك أو خلال الويكند.</p>
        <form method="post" action="/liquid-daily-orb/start">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">ابدأ أو استكمل البحث</button>
        </form>
        <p><a href="/liquid-daily-orb/protocol">البروتوكول المجمد</a> · <a href="/liquid-daily-orb/status">متابعة التقدم</a> · <a href="/liquid-daily-orb/result">النتيجة المختصرة</a></p>
        <form method="post" action="/liquid-daily-orb/pause">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">إيقاف آمن بعد الدفعة الحالية</button>
        </form>
        <form method="post" action="/liquid-daily-orb/download">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">تنزيل النتيجة الكاملة JSON.GZ</button>
        </form>
    </div>
</body>
</html>
"""


@app.post("/liquid-daily-orb/start")
def liquid_daily_orb_start():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    started, message = radar.start_liquid_daily_orb()
    return jsonify({
        "ok": started,
        "status": message,
        "status_url": "/liquid-daily-orb/status",
        "protocol_url": "/liquid-daily-orb/protocol",
        "result_url": "/liquid-daily-orb/result",
        "download_url": "/liquid-daily-orb/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    }), (202 if started else 409)


@app.get("/liquid-daily-orb/protocol")
def liquid_daily_orb_protocol():
    return jsonify({
        "version": VERSION,
        "build": BUILD,
        "research_spec": LIQUID_DAILY_ORB_SPEC,
        "live_protocol_sha256": PROTOCOL_SHA256,
    })


@app.post("/liquid-daily-orb/pause")
def liquid_daily_orb_pause():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    ok, message = radar.pause_liquid_daily_orb()
    return jsonify({"ok": ok, "status": message}), (202 if ok else 409)


@app.get("/liquid-daily-orb/status")
def liquid_daily_orb_status():
    stored = radar.redis.get_json(radar.orb_key("status"), None) if radar.redis.configured else None
    with radar.orb_lock:
        payload = dict(stored or radar.orb_state)
        payload["worker_alive"] = bool(radar.orb_thread and radar.orb_thread.is_alive())
    payload.update({
        "status_url": "/liquid-daily-orb/status",
        "result_url": "/liquid-daily-orb/result",
        "download_url": "/liquid-daily-orb/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    })
    return jsonify(payload)


@app.get("/liquid-daily-orb/result")
def liquid_daily_orb_result():
    report = radar.redis.get_json(radar.orb_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/liquid-daily-orb/status"}), 202
    return jsonify(report)


@app.post("/liquid-daily-orb/download")
def liquid_daily_orb_download():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    report = radar.redis.get_json(radar.orb_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/liquid-daily-orb/status"}), 202
    with radar.orb_lock:
        path = radar.orb_path
    if not path or not os.path.isfile(path):
        path = radar._materialize_orb_download(report)
    filename = f"ipr_liquid_daily_orb_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    return send_file(path, mimetype="application/gzip", as_attachment=True, download_name=filename)


@app.get("/daily-breakout")
def daily_breakout_page():
    return """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>بحث الاختراق اليومي بالحجم</title>
    <style>
        body { font-family: system-ui; background: #101114; color: #eee; max-width: 760px; margin: 30px auto; padding: 18px; }
        .box { background: #191b20; border: 1px solid #343741; border-radius: 14px; padding: 20px; margin: 14px 0; }
        input, button { width: 100%; box-sizing: border-box; font-size: 17px; padding: 13px; margin: 7px 0; border-radius: 9px; border: 1px solid #555; }
        button { background: #1d4ed8; color: white; font-weight: 700; }
        a { color: #93c5fd; }
    </style>
</head>
<body>
    <h1>Daily Breakout with Volume</h1>
    <div class="box">
        <p>إشارة بعد الإغلاق: اختراق أعلى 20 جلسة، حجم 1.5×، سعر 10–60 دولار، سيولة 60 جلسة لا تقل عن 20 مليون دولار، وTop-3.</p>
        <p>Daily-1 يخرج نهاية يوم الدخول، وDaily-2 يخرج نهاية اليوم التالي. لكل سياسة حكم مستقل بعد تكلفة 0.25% ووقف 1 ATR.</p>
        <p>الكون يستبعد الصناديق والمنتجات والقطاعات المحظورة بالأسماء. البحث قراءة فقط ولا يرسل تنبيهات أو أوامر.</p>
        <form method="post" action="/daily-breakout/start">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">ابدأ أو استكمل البحث</button>
        </form>
        <p><a href="/daily-breakout/protocol">البروتوكول المجمد</a> · <a href="/daily-breakout/status">متابعة التقدم</a> · <a href="/daily-breakout/result">النتيجة المختصرة</a></p>
        <form method="post" action="/daily-breakout/pause">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">إيقاف آمن بعد الدفعة الحالية</button>
        </form>
        <form method="post" action="/daily-breakout/download">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">تنزيل النتيجة الكاملة JSON.GZ</button>
        </form>
    </div>
</body>
</html>
"""


@app.get("/daily-breakout/protocol")
def daily_breakout_protocol():
    return jsonify({
        "version": VERSION,
        "build": BUILD,
        "research_spec": DAILY_BREAKOUT_SPEC,
        "live_protocol_sha256": PROTOCOL_SHA256,
    })


@app.post("/daily-breakout/start")
def daily_breakout_start():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    started, message = radar.start_daily_breakout()
    return jsonify({
        "ok": started,
        "status": message,
        "status_url": "/daily-breakout/status",
        "protocol_url": "/daily-breakout/protocol",
        "result_url": "/daily-breakout/result",
        "download_url": "/daily-breakout/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    }), (202 if started else 409)


@app.post("/daily-breakout/pause")
def daily_breakout_pause():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    ok, message = radar.pause_daily_breakout()
    return jsonify({"ok": ok, "status": message}), (202 if ok else 409)


@app.get("/daily-breakout/status")
def daily_breakout_status():
    stored = radar.redis.get_json(radar.breakout_key("status"), None) if radar.redis.configured else None
    with radar.breakout_lock:
        payload = dict(stored or radar.breakout_state)
        payload["worker_alive"] = bool(radar.breakout_thread and radar.breakout_thread.is_alive())
    payload.update({
        "status_url": "/daily-breakout/status",
        "result_url": "/daily-breakout/result",
        "download_url": "/daily-breakout/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    })
    return jsonify(payload)


@app.get("/daily-breakout/result")
def daily_breakout_result():
    report = radar.redis.get_json(radar.breakout_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/daily-breakout/status"}), 202
    return jsonify(report)


@app.post("/daily-breakout/download")
def daily_breakout_download():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    report = radar.redis.get_json(radar.breakout_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/daily-breakout/status"}), 202
    with radar.breakout_lock:
        path = radar.breakout_path
    if not path or not os.path.isfile(path):
        path = radar._materialize_daily_breakout_download(report)
    filename = f"ipr_daily_breakout_volume_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    return send_file(path, mimetype="application/gzip", as_attachment=True, download_name=filename)


@app.get("/rsi2-reversal")
def rsi2_reversal_page():
    return """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>بحث RSI(2) للانعكاس القصير</title>
    <style>
        body { font-family: system-ui; background: #101114; color: #eee; max-width: 760px; margin: 30px auto; padding: 18px; }
        .box { background: #191b20; border: 1px solid #343741; border-radius: 14px; padding: 20px; margin: 14px 0; }
        input, button { width: 100%; box-sizing: border-box; font-size: 17px; padding: 13px; margin: 7px 0; border-radius: 9px; border: 1px solid #555; }
        button { background: #7c3aed; color: white; font-weight: 700; }
        a { color: #c4b5fd; }
    </style>
</head>
<body>
    <h1>RSI(2) Short-Term Reversal</h1>
    <div class="box">
        <p>اختبار تاريخي متعدد السنوات: فوق SMA200، وRSI(2)&lt;5 أساسيًا، ودخول افتتاح الجلسة التالية، وخروج سببي خلال جلستين كحد أقصى.</p>
        <p>السعر 10–60 دولار، والسيولة السابقة 20 مليون دولار يوميًا، وTop-3. تكلفة القرار 0.25% ولا يوجد وقف مخترع.</p>
        <p>يبدأ بفحص عمق Alpaca تلقائيًا، ثم يجلب البيانات الخام والمعدلة بالتوازي. الشموع الخام لا تحفظ في Redis.</p>
        <form method="post" action="/rsi2-reversal/start">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">ابدأ أو استكمل البحث</button>
        </form>
        <p><a href="/rsi2-reversal/protocol">البروتوكول المجمد</a> · <a href="/rsi2-reversal/status">متابعة التقدم</a> · <a href="/rsi2-reversal/result">النتيجة</a> · <a href="/rsi2-reversal/probe">فحص البيانات</a></p>
        <form method="post" action="/rsi2-reversal/pause">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">إيقاف آمن بعد الدفعات الجارية</button>
        </form>
        <form method="post" action="/rsi2-reversal/download">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">تنزيل النتيجة الكاملة JSON.GZ</button>
        </form>
    </div>
</body>
</html>
"""


@app.get("/rsi2-reversal/protocol")
def rsi2_reversal_protocol():
    return jsonify({
        "version": VERSION,
        "build": BUILD,
        "research_spec": RSI2_REVERSAL_SPEC,
        "live_protocol_sha256": PROTOCOL_SHA256,
    })


@app.post("/rsi2-reversal/start")
def rsi2_reversal_start():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    started, message = radar.start_rsi2_reversal()
    return jsonify({
        "ok": started,
        "status": message,
        "status_url": "/rsi2-reversal/status",
        "protocol_url": "/rsi2-reversal/protocol",
        "probe_url": "/rsi2-reversal/probe",
        "result_url": "/rsi2-reversal/result",
        "download_url": "/rsi2-reversal/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    }), (202 if started else 409)


@app.post("/rsi2-reversal/pause")
def rsi2_reversal_pause():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    ok, message = radar.pause_rsi2_reversal()
    return jsonify({"ok": ok, "status": message}), (202 if ok else 409)


@app.get("/rsi2-reversal/status")
def rsi2_reversal_status():
    stored = radar.redis.get_json(radar.rsi2_key("status"), None) if radar.redis.configured else None
    with radar.rsi2_lock:
        payload = dict(stored or radar.rsi2_state)
        payload["worker_alive"] = bool(radar.rsi2_thread and radar.rsi2_thread.is_alive())
        payload["alpaca_pages_completed"] = max(
            int(payload.get("alpaca_pages_completed") or 0), radar.rsi2_request_pages
        )
        payload["daily_bar_points_received"] = max(
            int(payload.get("daily_bar_points_received") or 0), radar.rsi2_bar_points
        )
    payload.update({
        "status_url": "/rsi2-reversal/status",
        "probe_url": "/rsi2-reversal/probe",
        "result_url": "/rsi2-reversal/result",
        "download_url": "/rsi2-reversal/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    })
    return jsonify(payload)


@app.get("/rsi2-reversal/probe")
def rsi2_reversal_probe():
    probe = radar.redis.get_json(radar.rsi2_key("data_probe"), None) if radar.redis.configured else None
    if not probe:
        return jsonify({"probe_ready": False, "status_url": "/rsi2-reversal/status"}), 202
    return jsonify(probe)


@app.get("/rsi2-reversal/result")
def rsi2_reversal_result():
    report = radar.redis.get_json(radar.rsi2_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/rsi2-reversal/status"}), 202
    return jsonify(report)


@app.post("/rsi2-reversal/download")
def rsi2_reversal_download():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    report = radar.redis.get_json(radar.rsi2_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/rsi2-reversal/status"}), 202
    with radar.rsi2_lock:
        path = radar.rsi2_path
    if not path or not os.path.isfile(path):
        path = radar._materialize_rsi2_download()
    filename = f"ipr_rsi2_short_term_reversal_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json.gz"
    return send_file(path, mimetype="application/gzip", as_attachment=True, download_name=filename)


@app.get("/rsi2-sanity")
def rsi2_sanity_page():
    return """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>فحص سلامة RSI(2)</title>
    <style>
        body { font-family: system-ui; background: #101114; color: #eee; max-width: 760px; margin: 30px auto; padding: 18px; }
        .box { background: #191b20; border: 1px solid #343741; border-radius: 14px; padding: 20px; margin: 14px 0; }
        input, button { width: 100%; box-sizing: border-box; font-size: 17px; padding: 13px; margin: 7px 0; border-radius: 9px; border: 1px solid #555; }
        button { background: #0f766e; color: white; font-weight: 700; }
        a { color: #5eead4; }
    </style>
</head>
<body>
    <h1>RSI(2) Matched-Random Sanity Check</h1>
    <div class="box">
        <p>يقارن نتيجة RSI(2) بكون عشوائي مطابق: نفس أيام الإشارة، ونفس عدد المراكز، والسعر والسيولة وSMA200، ونفس الدخول والخروج. الشرط الوحيد المحذوف هو RSI وترتيبه.</p>
        <p>يعرض المتوسط العشوائي الدقيق و1,000 محاكاة ثابتة عند تكاليف 0% و0.10% و0.25%، مع فحص مستقل لحساب RSI وSMA والعوائد.</p>
        <p>هذا فحص تشخيصي فقط؛ لا يرسل تنبيهات أو أوامر ولا يغيّر الاستراتيجية الحية.</p>
        <form method="post" action="/rsi2-sanity/start">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">ابدأ أو استكمل الفحص</button>
        </form>
        <p><a href="/rsi2-sanity/protocol">البروتوكول المجمد</a> · <a href="/rsi2-sanity/status">متابعة التقدم</a> · <a href="/rsi2-sanity/result">النتيجة</a></p>
        <form method="post" action="/rsi2-sanity/pause">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">إيقاف آمن بعد الدفعات الجارية</button>
        </form>
        <form method="post" action="/rsi2-sanity/download">
            <input name="token" type="password" placeholder="Admin token" required>
            <button type="submit">تنزيل التقرير JSON.GZ</button>
        </form>
    </div>
</body>
</html>
"""


@app.get("/rsi2-sanity/protocol")
def rsi2_sanity_protocol():
    return jsonify({
        "version": VERSION,
        "build": BUILD,
        "audit_spec": RSI2_SANITY_SPEC,
        "live_protocol_sha256": PROTOCOL_SHA256,
    })


@app.post("/rsi2-sanity/start")
def rsi2_sanity_start():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    started, message = radar.start_rsi2_sanity()
    return jsonify({
        "ok": started,
        "status": message,
        "status_url": "/rsi2-sanity/status",
        "protocol_url": "/rsi2-sanity/protocol",
        "result_url": "/rsi2-sanity/result",
        "download_url": "/rsi2-sanity/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    }), (202 if started else 409)


@app.post("/rsi2-sanity/pause")
def rsi2_sanity_pause():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    ok, message = radar.pause_rsi2_sanity()
    return jsonify({"ok": ok, "status": message}), (202 if ok else 409)


@app.get("/rsi2-sanity/status")
def rsi2_sanity_status():
    stored = radar.redis.get_json(radar.sanity_key("status"), None) if radar.redis.configured else None
    with radar.sanity_lock:
        payload = dict(stored or radar.sanity_state)
        payload["worker_alive"] = bool(radar.sanity_thread and radar.sanity_thread.is_alive())
        payload["alpaca_pages_completed"] = max(
            int(payload.get("alpaca_pages_completed") or 0), radar.sanity_request_pages
        )
        payload["daily_bar_points_received"] = max(
            int(payload.get("daily_bar_points_received") or 0), radar.sanity_bar_points
        )
    payload.update({
        "status_url": "/rsi2-sanity/status",
        "result_url": "/rsi2-sanity/result",
        "download_url": "/rsi2-sanity/download",
        "alerts_enabled": False,
        "orders_enabled": False,
    })
    return jsonify(payload)


@app.get("/rsi2-sanity/result")
def rsi2_sanity_result():
    report = radar.redis.get_json(radar.sanity_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/rsi2-sanity/status"}), 202
    return jsonify(report)


@app.post("/rsi2-sanity/download")
def rsi2_sanity_download():
    if not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    report = radar.redis.get_json(radar.sanity_key("report"), None) if radar.redis.configured else None
    if not report:
        return jsonify({"result_ready": False, "status_url": "/rsi2-sanity/status"}), 202
    with radar.sanity_lock:
        path = radar.sanity_path
    if not path or not os.path.isfile(path):
        path = radar._materialize_sanity_download(report)
    filename = f"ipr_rsi2_sanity_check_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.json.gz"
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
        "liquid_daily_orb_alive": bool(radar.orb_thread and radar.orb_thread.is_alive()),
        "daily_breakout_alive": bool(radar.breakout_thread and radar.breakout_thread.is_alive()),
        "rsi2_reversal_alive": bool(radar.rsi2_thread and radar.rsi2_thread.is_alive()),
        "rsi2_sanity_alive": bool(radar.sanity_thread and radar.sanity_thread.is_alive()),
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
