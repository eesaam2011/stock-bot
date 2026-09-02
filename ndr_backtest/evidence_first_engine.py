from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from collections import Counter
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from ndr_backtest_engine import BacktestCollector, now_iso, parse_dt


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
PROTOCOL_PATH = Path(__file__).with_name("evidence_first_protocol.json")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_protocol(path: Path = PROTOCOL_PATH) -> tuple[dict[str, Any], str]:
    protocol = json.loads(path.read_text(encoding="utf-8"))
    fingerprint = hashlib.sha256(_canonical_json(protocol).encode("utf-8")).hexdigest()
    return protocol, fingerprint


def _bar_time(bar: dict[str, Any]) -> datetime:
    return parse_dt(str(bar["t"]))


def _close_location(bar: dict[str, Any]) -> float:
    high, low, close = (float(bar[x]) for x in ("h", "l", "c"))
    return (close - low) / (high - low) if high > low else 0.5


def _causal_vwap(bars: Iterable[dict[str, Any]]) -> float | None:
    numerator = 0.0
    volume = 0.0
    for bar in bars:
        bar_volume = float(bar.get("v", 0) or 0)
        numerator += float(bar.get("vw") or bar["c"]) * bar_volume
        volume += bar_volume
    return numerator / volume if volume > 0 else None


def _volume_ratio(history: list[dict[str, Any]], bar: dict[str, Any]) -> float:
    baseline = [float(x.get("v", 0) or 0) for x in history[-10:] if float(x.get("v", 0) or 0) > 0]
    return float(bar.get("v", 0) or 0) / max(1.0, median(baseline)) if baseline else 0.0


class EvidenceFirstEngine:
    """Development-only, pre-registered ORB/retest research engine.

    This class intentionally has no Telegram, broker, or order integration.
    """

    RESULT_VERSION = "evidence_first_v1"
    PATHS = ("ORB_5M", "BREAKOUT_RETEST_RECLAIM")

    def __init__(self, collector: BacktestCollector | None = None):
        self.base = collector or BacktestCollector()
        self.redis = self.base.redis
        self.alpaca = self.base.alpaca
        self.protocol, self.protocol_sha256 = load_protocol()
        self.batch_size = max(1, int(os.getenv("EFB_SYMBOL_BATCH_SIZE", "100")))
        self.delay = max(0.0, float(os.getenv("EFB_REQUEST_DELAY_SECONDS", "0.15")))
        self.allow_exploratory_universe = os.getenv(
            "EFB_ALLOW_STORED_CANDIDATE_UNIVERSE", "false"
        ).lower() in {"1", "true", "yes"}

    def key(self, suffix: str) -> str:
        return self.base.key(f"{self.RESULT_VERSION}:{suffix}")

    def protocol_record(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "protocol_sha256": self.protocol_sha256,
            "result_version": self.RESULT_VERSION,
            "execution_scope": "development_only",
            "alerts_enabled": False,
            "orders_enabled": False,
        }

    def lock_protocol(self) -> dict[str, Any]:
        key = self.key("protocol_lock")
        existing = self.redis.get_json(key, None)
        record = self.protocol_record()
        if existing is not None and existing.get("protocol_sha256") != self.protocol_sha256:
            raise RuntimeError("Frozen protocol mismatch: use a new result version for changed rules")
        if existing is None:
            self.redis.set_json(key, record)
        return existing or record

    @staticmethod
    def _regular_bars(session: str, bars: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        for bar in sorted(bars, key=lambda x: x["t"]):
            stamp = _bar_time(bar).astimezone(NY)
            minute = stamp.hour * 60 + stamp.minute
            if stamp.date().isoformat() == session and 570 <= minute < 960:
                output.append(bar)
        return output

    def _opening_range(self, session: str, bars: list[dict[str, Any]]) -> dict[str, Any] | None:
        opening = []
        expected = {570, 571, 572, 573, 574}
        for bar in bars:
            stamp = _bar_time(bar).astimezone(NY)
            minute = stamp.hour * 60 + stamp.minute
            if minute in expected:
                opening.append(bar)
        actual = {_bar_time(x).astimezone(NY).hour * 60 + _bar_time(x).astimezone(NY).minute for x in opening}
        if actual != expected:
            return None
        return {
            "high": max(float(x["h"]) for x in opening),
            "low": min(float(x["l"]) for x in opening),
            "bars": opening,
            "end_ts": max(x["t"] for x in opening),
        }

    def _qualifying_breakout(
        self, bars: list[dict[str, Any]], opening_range: dict[str, Any]
    ) -> tuple[int, dict[str, Any]] | None:
        shared = self.protocol["shared"]
        level = float(opening_range["high"])
        latest_hour, latest_minute = map(int, shared["latest_entry_ny"].split(":"))
        latest = latest_hour * 60 + latest_minute
        for index, bar in enumerate(bars):
            stamp = _bar_time(bar).astimezone(NY)
            minute = stamp.hour * 60 + stamp.minute
            if minute < 575 or minute > latest:
                continue
            price = float(bar["c"])
            if not (shared["price_min"] <= price <= shared["price_max"]):
                continue
            history = bars[:index]
            vwap = _causal_vwap(bars[: index + 1])
            volume_ratio = _volume_ratio(history, bar)
            if price < level * (1 + shared["breakout_buffer_pct"] / 100):
                continue
            if _close_location(bar) < shared["breakout_close_location_min"]:
                continue
            if volume_ratio < shared["breakout_volume_ratio_min"]:
                continue
            if shared["require_close_at_or_above_causal_vwap"] and (vwap is None or price < vwap):
                continue
            return index, {
                "signal_ts": bar["t"],
                "signal_price": price,
                "breakout_index": index,
                "breakout_low": float(bar["l"]),
                "opening_range_high": level,
                "opening_range_low": float(opening_range["low"]),
                "causal_vwap": round(vwap, 6) if vwap is not None else None,
                "volume_ratio": round(volume_ratio, 4),
                "close_location": round(_close_location(bar), 4),
            }
        return None

    def _retest_reclaim(
        self, bars: list[dict[str, Any]], breakout: dict[str, Any]
    ) -> dict[str, Any] | None:
        rules = self.protocol["paths"]["BREAKOUT_RETEST_RECLAIM"]
        level = float(breakout["opening_range_high"])
        breakout_index = int(breakout["breakout_index"])
        breakout_dt = _bar_time(bars[breakout_index])
        retest: tuple[int, dict[str, Any]] | None = None
        for index in range(breakout_index + 1, len(bars)):
            bar = bars[index]
            elapsed = (_bar_time(bar) - breakout_dt).total_seconds() / 60
            if elapsed > rules["retest_deadline_minutes"]:
                break
            touch = float(bar["l"]) <= level * (1 + rules["retest_touch_above_level_pct"] / 100)
            held = float(bar["c"]) >= level * (1 - rules["retest_max_close_below_level_pct"] / 100)
            if touch and held:
                retest = (index, bar)
                break
        if retest is None:
            return None
        retest_index, retest_bar = retest
        retest_dt = _bar_time(retest_bar)
        for index in range(retest_index + 1, len(bars)):
            bar = bars[index]
            elapsed = (_bar_time(bar) - retest_dt).total_seconds() / 60
            if elapsed > rules["reclaim_deadline_minutes"]:
                break
            ratio = _volume_ratio(bars[:index], bar)
            if float(bar["c"]) < level * (1 + rules["reclaim_buffer_pct"] / 100):
                continue
            if _close_location(bar) < rules["reclaim_close_location_min"]:
                continue
            if ratio < rules["reclaim_volume_ratio_min"]:
                continue
            return {
                **breakout,
                "signal_ts": bar["t"],
                "signal_price": float(bar["c"]),
                "retest_ts": retest_bar["t"],
                "retest_low": float(retest_bar["l"]),
                "reclaim_index": index,
                "reclaim_volume_ratio": round(ratio, 4),
                "reclaim_close_location": round(_close_location(bar), 4),
            }
        return None

    def detect(self, session: str, raw_bars: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
        bars = self._regular_bars(session, raw_bars)
        opening_range = self._opening_range(session, bars)
        if opening_range is None:
            return {path: None for path in self.PATHS}
        found = self._qualifying_breakout(bars, opening_range)
        if found is None:
            return {path: None for path in self.PATHS}
        _, breakout = found
        return {
            "ORB_5M": breakout,
            "BREAKOUT_RETEST_RECLAIM": self._retest_reclaim(bars, breakout),
        }

    def _structural_stop(self, path: str, signal: dict[str, Any]) -> float:
        shared = self.protocol["shared"]
        level_stop = float(signal["opening_range_high"]) * (1 - shared["level_stop_buffer_pct"] / 100)
        structure_low = (
            float(signal["retest_low"])
            if path == "BREAKOUT_RETEST_RECLAIM"
            else float(signal["breakout_low"])
        )
        bar_stop = structure_low * (1 - shared["bar_stop_buffer_pct"] / 100)
        return min(level_stop, bar_stop)

    def simulate(
        self, session: str, symbol: str, path: str, raw_bars: Iterable[dict[str, Any]], signal: dict[str, Any]
    ) -> dict[str, Any]:
        bars = self._regular_bars(session, raw_bars)
        future = [bar for bar in bars if bar["t"] > signal["signal_ts"]]
        base = {"session": session, "symbol": symbol, "path": path, **signal}
        if not future:
            return {**base, "tradable": False, "status": "no_next_bar"}
        entry_bar = future[0]
        entry = float(entry_bar["o"])
        stop = self._structural_stop(path, signal)
        if entry <= stop:
            return {**base, "tradable": False, "status": "entry_gap_below_stop", "entry": entry, "stop": stop}
        stop_pct = (entry - stop) / entry * 100
        shared = self.protocol["shared"]
        if not (shared["minimum_stop_pct"] <= stop_pct <= shared["maximum_stop_pct"]):
            return {**base, "tradable": False, "status": "stop_outside_frozen_bounds", "entry": entry, "stop": stop, "stop_pct": stop_pct}
        target = entry + (entry - stop) * shared["target_r_multiple"]
        deadline = _bar_time(entry_bar) + timedelta(minutes=self.protocol["data_policy"]["time_horizon_minutes"])
        eligible = [bar for bar in future if _bar_time(bar) <= deadline]
        exit_price = None
        exit_ts = None
        status = "time_exit"
        for bar in eligible:
            if float(bar["l"]) <= stop:
                exit_price, exit_ts, status = stop, bar["t"], "stop_exit"
                break
            if float(bar["h"]) >= target:
                exit_price, exit_ts, status = target, bar["t"], "target_2r_exit"
                break
        if exit_price is None:
            if not eligible:
                return {**base, "tradable": False, "status": "no_bars_in_horizon"}
            exit_price, exit_ts = float(eligible[-1]["c"]), eligible[-1]["t"]
        return_pct = (exit_price / entry - 1) * 100
        return {
            **base,
            "tradable": True,
            "status": status,
            "entry_ts": entry_bar["t"],
            "entry": round(entry, 6),
            "stop": round(stop, 6),
            "stop_pct": round(stop_pct, 4),
            "target": round(target, 6),
            "exit_ts": exit_ts,
            "exit": round(exit_price, 6),
            "gross_return_pct": round(return_pct, 4),
        }

    def evaluate_symbol(self, session: str, symbol: str, bars: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        bars = list(bars)
        signals = self.detect(session, bars)
        rows = []
        for path in self.PATHS:
            signal = signals[path]
            if signal is not None:
                rows.append(self.simulate(session, symbol, path, bars, signal))
        return rows

    @staticmethod
    def _max_drawdown(returns: list[float]) -> float:
        equity = peak = 1.0
        max_drawdown = 0.0
        for value in returns:
            equity *= 1 + value / 100
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        return max_drawdown

    @classmethod
    def _summary(cls, rows: list[dict[str, Any]], cost_pct: float) -> dict[str, Any]:
        trades = sorted((row for row in rows if row.get("tradable")), key=lambda x: (x["session"], x["entry_ts"], x["symbol"]))
        returns = [float(row["gross_return_pct"]) - cost_pct for row in trades]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        gross_profit = sum(wins)
        gross_loss = -sum(losses)
        return {
            "trades": len(trades),
            "active_sessions": len({row["session"] for row in trades}),
            "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else None,
            "average_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (math.inf if gross_profit > 0 else None),
            "maximum_drawdown_pct": round(cls._max_drawdown(returns), 4) if returns else None,
            "statuses": dict(Counter(row["status"] for row in trades)),
        }

    @staticmethod
    def _session_bootstrap_positive_probability(
        rows: list[dict[str, Any]], cost_pct: float, samples: int = 5000, seed: int = 20260902
    ) -> float | None:
        by_session: dict[str, list[float]] = {}
        for row in rows:
            if row.get("tradable"):
                by_session.setdefault(row["session"], []).append(float(row["gross_return_pct"]) - cost_pct)
        sessions = sorted(by_session)
        if len(sessions) < 2:
            return None
        rng = random.Random(seed)
        positive = 0
        for _ in range(samples):
            returns = []
            for _ in sessions:
                returns.extend(by_session[rng.choice(sessions)])
            positive += (sum(returns) / len(returns)) > 0
        return round(positive / samples, 4)

    def analyze(self, rows: list[dict[str, Any]], universe_causal: bool) -> dict[str, Any]:
        costs = self.protocol["data_policy"]["round_trip_costs_pct"]
        decision_cost = self.protocol["data_policy"]["decision_cost_pct"]
        stress_cost = max(costs)
        gate = self.protocol["development_gate"]
        output: dict[str, Any] = {}
        for path in self.PATHS:
            selected = [row for row in rows if row.get("path") == path]
            summaries = {str(cost): self._summary(selected, float(cost)) for cost in costs}
            decision = summaries[str(decision_cost)]
            stress = summaries[str(stress_cost)]
            bootstrap = self._session_bootstrap_positive_probability(selected, decision_cost)
            metric_pass = bool(
                decision["trades"] >= gate["minimum_trades_per_path"]
                and decision["active_sessions"] >= gate["minimum_active_sessions"]
                and decision["profit_factor"] is not None
                and decision["profit_factor"] >= gate["profit_factor_min_at_decision_cost"]
                and decision["average_return_pct"] is not None
                and decision["average_return_pct"] > 0
                and stress["profit_factor"] is not None
                and stress["profit_factor"] >= gate["profit_factor_min_at_stress_cost"]
                and bootstrap is not None
                and bootstrap >= gate["session_cluster_bootstrap_positive_probability_min"]
                and decision["maximum_drawdown_pct"] is not None
                and decision["maximum_drawdown_pct"] <= gate["maximum_drawdown_pct"]
            )
            output[path] = {
                "signals": len(selected),
                "non_tradable": len([row for row in selected if not row.get("tradable")]),
                "cost_summaries": summaries,
                "session_cluster_bootstrap_positive_probability": bootstrap,
                "development_metric_gate_passed": metric_pass,
                "eligible_for_forward_holdout": bool(metric_pass and universe_causal),
                "live_approved": False,
            }
        return output

    def _development_sessions(self) -> list[str]:
        manifest = self.redis.get_json(self.base.key("manifest"), {})
        sessions = list(manifest.get("development_sessions") or [])
        if not sessions:
            holdout = set(manifest.get("holdout_sessions") or [])
            sessions = [x for x in manifest.get("sessions", []) if x not in holdout]
        if not sessions:
            raise RuntimeError("No development sessions found in the immutable manifest")
        return sessions

    def _full_universe(self) -> tuple[list[str] | None, str, bool]:
        manifest = self.redis.get_json(self.base.key("manifest"), {})
        for field in ("universe_symbols", "symbols"):
            value = manifest.get(field)
            if isinstance(value, list) and value:
                return sorted(set(map(str, value))), f"manifest.{field}", True
        value = self.redis.get_json(self.base.key("universe_symbols"), None)
        if isinstance(value, list) and value:
            return sorted(set(map(str, value))), "redis.frozen_full_universe", True
        return None, "missing", False

    def readiness(self) -> dict[str, Any]:
        sessions = self._development_sessions()
        symbols, source, causal = self._full_universe()
        return {
            "ready_for_decisive_development_run": bool(symbols and causal),
            "development_sessions": len(sessions),
            "legacy_holdout_locked": True,
            "full_universe_found": symbols is not None,
            "full_universe_count": len(symbols) if symbols is not None else 0,
            "universe_source": source,
            "exploratory_fallback_enabled": self.allow_exploratory_universe,
            "warning": None if symbols else (
                "A frozen full universe was not found. The stored candidate universe may be run only as exploratory "
                "and can never qualify a path for forward holdout or live use."
            ),
            "protocol_sha256": self.protocol_sha256,
            "alerts_enabled": False,
            "orders_enabled": False,
        }

    def _exploratory_symbols(self, session: str) -> list[str]:
        values = self.redis.command("SMEMBERS", self.base.key(f"detail_session:{session}")) or []
        return sorted(set(map(str, values)))

    def run_development(self, progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
        self.lock_protocol()
        sessions = self._development_sessions()
        full_universe, universe_source, universe_causal = self._full_universe()
        if full_universe is None and not self.allow_exploratory_universe:
            raise RuntimeError(
                "Frozen full universe unavailable. Set EFB_ALLOW_STORED_CANDIDATE_UNIVERSE=true only for an explicitly exploratory run."
            )
        cases_key = self.key("development_cases")
        rows: list[dict[str, Any]] = []
        processed = 0
        total = len(sessions) * (len(full_universe) if full_universe is not None else 0)
        for session in sessions:
            symbols = full_universe if full_universe is not None else self._exploratory_symbols(session)
            if full_universe is None:
                universe_source = "stored_coarse_candidate_universe_outcome_selection_unknown"
                universe_causal = False
                total += len(symbols)
            start, end = self.base.session_window(session)
            regular_start = datetime.combine(datetime.fromisoformat(session).date(), dtime(9, 30), NY).astimezone(UTC)
            regular_end = datetime.combine(datetime.fromisoformat(session).date(), dtime(16, 0), NY).astimezone(UTC)
            for offset in range(0, len(symbols), self.batch_size):
                batch = symbols[offset : offset + self.batch_size]
                fields = [f"{session}|{symbol}" for symbol in batch]
                existing = self.redis.command("HMGET", cases_key, *fields) or [None] * len(fields)
                todo = [symbol for symbol, value in zip(batch, existing) if value is None]
                downloaded = self.alpaca.bars(todo, regular_start, regular_end, "sip") if todo else {}
                writes = []
                for symbol, field, value in zip(batch, fields, existing):
                    if value is not None:
                        payload = json.loads(value)
                    else:
                        signal_rows = self.evaluate_symbol(session, symbol, downloaded.get(symbol, []))
                        payload = {"session": session, "symbol": symbol, "rows": signal_rows}
                        writes.append((field, _canonical_json(payload)))
                    rows.extend(payload.get("rows", []))
                    processed += 1
                    if progress:
                        progress(processed, total, session)
                if writes:
                    self.base.hset_bounded(cases_key, writes)
                if todo:
                    time.sleep(self.delay)
        report = {
            "schema": 1,
            "generated_at": now_iso(),
            "protocol_sha256": self.protocol_sha256,
            "protocol_id": self.protocol["protocol_id"],
            "partition": "development",
            "legacy_holdout_opened": False,
            "universe_source": universe_source,
            "universe_causal_for_adoption": universe_causal,
            "symbols_processed": processed,
            "analysis": self.analyze(rows, universe_causal),
            "next_step": "No live use. A passing path proceeds to 20 new unseen forward sessions.",
        }
        self.redis.set_json(self.key("development_report"), report)
        return report

    def development_result(self) -> dict[str, Any] | None:
        return self.redis.get_json(self.key("development_report"), None)

    def status(self) -> dict[str, Any] | None:
        return self.redis.get_json(self.key("status"), None)
