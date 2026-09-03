from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ndr_backtest_engine import now_iso, parse_dt
from phase2_model_engine_v2 import Phase2ModelEngine


PROTOCOL_PATH = Path(__file__).with_name("phase4_exit_path_protocol.json")
EMBEDDED_PROTOCOL_JSON = r'''
{
  "protocol_id": "NDR-PHASE4-EXIT-PATH-2026-09-03-A",
  "created_at": "2026-09-03T00:00:00Z",
  "purpose": "Determine whether the strongest causal explosion rankings can be converted into executable profit by a small frozen family of exit policies.",
  "candidate_selection": {
    "model": "L2 logistic explosion-quality head",
    "l2_penalty": 1.0,
    "top_fraction_per_validation_fold": 0.05,
    "folds": "the same three expanding chronological development folds as Phase 2",
    "expected_oof_cases": "approximately 352",
    "holdout_used_for_selection": false
  },
  "execution": {
    "entry": "next one-minute bar open after signal T",
    "horizon_minutes_after_signal": 60,
    "intrabar_ordering": "stop before target",
    "round_trip_cost_pct": 0.25,
    "bar_feed": "Alpaca SIP split-adjusted",
    "policies": [
      {"id": "TP2_STOP3", "target_pct": 2.0, "hard_stop_pct": 3.0},
      {"id": "TP3_STOP3", "target_pct": 3.0, "hard_stop_pct": 3.0},
      {"id": "TP5_BE2_STOP3", "target_pct": 5.0, "hard_stop_pct": 3.0, "breakeven_after_pct": 2.0},
      {"id": "TRAIL15_AFTER2_STOP3", "hard_stop_pct": 3.0, "trail_pct": 1.5, "trail_after_pct": 2.0},
      {"id": "HALF2_REST5_BE_STOP3", "first_target_pct": 2.0, "final_target_pct": 5.0, "first_fraction": 0.5, "hard_stop_pct": 3.0, "breakeven_after_first_target": true}
    ]
  },
  "development_policy": {
    "exit_design_sessions": "first 65 percent of OOF candidate sessions",
    "exit_validation_sessions": "last 35 percent of OOF candidate sessions",
    "minimum_design_trades": 100,
    "minimum_validation_trades": 100,
    "profit_factor_min": 1.15,
    "average_net_return_must_be_positive": true,
    "maximum_drawdown_pct": 20.0,
    "session_cluster_bootstrap_positive_probability_min": 0.95
  },
  "legacy_holdout": "historical audit only, evaluated only if one frozen policy passes development validation",
  "safety": {"alerts_enabled": false, "orders_enabled": false, "live_approved": false}
}
'''


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_phase4_protocol(path: Path = PROTOCOL_PATH) -> tuple[dict[str, Any], str]:
    embedded = json.loads(EMBEDDED_PROTOCOL_JSON)
    if path.exists():
        protocol = json.loads(path.read_text(encoding="utf-8"))
        if _canonical_json(protocol) != _canonical_json(embedded):
            raise RuntimeError("Phase-4 protocol file does not match the embedded frozen protocol")
    else:
        protocol = embedded
    fingerprint = hashlib.sha256(_canonical_json(protocol).encode("utf-8")).hexdigest()
    return protocol, fingerprint


class Phase4ExitPathEngine:
    VERSION = "phase4_exit_path_v1"

    def __init__(self, phase2: Phase2ModelEngine | None = None):
        self.phase2 = phase2 or Phase2ModelEngine()
        self.base = self.phase2.base
        self.redis = self.phase2.redis
        self.alpaca = self.base.alpaca
        self.protocol, self.protocol_sha256 = load_phase4_protocol()
        self.batch_size = max(1, int(os.getenv("PHASE4_SYMBOL_BATCH_SIZE", "100")))
        self.delay = max(0.0, float(os.getenv("PHASE4_REQUEST_DELAY_SECONDS", "0.15")))

    def key(self, suffix: str) -> str:
        return self.base.key(f"{self.VERSION}:{suffix}")

    def protocol_record(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "protocol_sha256": self.protocol_sha256,
            "result_version": self.VERSION,
            "alerts_enabled": False,
            "orders_enabled": False,
            "live_approved": False,
        }

    def lock_protocol(self) -> dict[str, Any]:
        key = self.key("protocol_lock")
        existing = self.redis.get_json(key, None)
        record = self.protocol_record()
        if existing is not None and existing.get("protocol_sha256") != self.protocol_sha256:
            raise RuntimeError("Frozen Phase-4 protocol mismatch; use a new result version")
        if existing is None:
            self.redis.set_json(key, record)
        return existing or record

    def readiness(self) -> dict[str, Any]:
        source = self.phase2.readiness()
        return {
            "ready_for_phase4_development": bool(source["ready_for_phase2_development"]),
            "development_sessions": source["development_sessions"],
            "legacy_holdout_sessions": source["legacy_holdout_sessions"],
            "price_change_cases": source["price_change_cases"],
            "er45_cases": source["er45_cases"],
            "targeted_exit_path_fetch_only": True,
            "protocol_sha256": self.protocol_sha256,
            "alerts_enabled": False,
            "orders_enabled": False,
            "live_approved": False,
        }

    def _quality_oof_candidates(self, development: list[dict[str, Any]]) -> list[dict[str, Any]]:
        l2 = float(self.protocol["candidate_selection"]["l2_penalty"])
        fraction = float(self.protocol["candidate_selection"]["top_fraction_per_validation_fold"])
        selected = []
        for fold_index, (train, valid) in enumerate(self.phase2._chronological_folds(development), 1):
            model = self.phase2._fit(
                self.phase2._matrix(train),
                np.asarray([row["explosion_ge10"] for row in train], dtype=float),
                l2,
            )
            probability = self.phase2._predict(model, self.phase2._matrix(valid))
            cutoff = float(np.quantile(probability, 1.0 - fraction))
            for row, score in zip(valid, probability):
                if score >= cutoff:
                    selected.append({**row, "quality_probability_oof": float(score), "quality_fold": fold_index})
        selected.sort(key=lambda row: (row["session"], row["t"], row["symbol"]))
        return selected

    @staticmethod
    def _bars_window(bars: list[dict[str, Any]], signal_t: str, horizon_minutes: int) -> list[dict[str, Any]]:
        signal_dt = parse_dt(signal_t)
        end = signal_dt + timedelta(minutes=horizon_minutes)
        return [bar for bar in sorted(bars, key=lambda item: item["t"]) if signal_dt < parse_dt(bar["t"]) <= end]

    def _load_bars(
        self,
        candidates: list[dict[str, Any]],
        progress: Callable[[int, int, str | None], None] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        cache_key = self.key("minute_bars")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in candidates:
            grouped.setdefault(row["session"], []).append(row)
        output: dict[str, list[dict[str, Any]]] = {}
        processed = 0
        total = len(candidates)
        horizon = int(self.protocol["execution"]["horizon_minutes_after_signal"])
        for session, session_rows in sorted(grouped.items()):
            for begin in range(0, len(session_rows), self.batch_size):
                batch = session_rows[begin:begin + self.batch_size]
                fields = [row["case_id"] for row in batch]
                existing = self.redis.command("HMGET", cache_key, *fields) or [None] * len(fields)
                todo = [row for row, value in zip(batch, existing) if value is None]
                fetched: dict[str, list[dict[str, Any]]] = {}
                if todo:
                    start = min(parse_dt(row["t"]) for row in todo)
                    end = max(parse_dt(row["t"]) for row in todo) + timedelta(minutes=horizon + 2)
                    symbols = sorted({row["symbol"] for row in todo})
                    fetched = self.alpaca.bars(symbols, start, end, "sip", "split")
                writes = []
                for row, value in zip(batch, existing):
                    if value is not None:
                        bars = json.loads(value)
                    else:
                        bars = self._bars_window(fetched.get(row["symbol"], []), row["t"], horizon)
                        writes.append((row["case_id"], json.dumps(bars, separators=(",", ":"))))
                    output[row["case_id"]] = bars
                    processed += 1
                    if progress:
                        progress(processed, total, session)
                if writes:
                    self.base.hset_bounded(cache_key, writes)
                if todo:
                    time.sleep(self.delay)
        return output

    @staticmethod
    def _exit_row(candidate: dict[str, Any], policy_id: str, exit_price: float, entry: float, status: str) -> dict[str, Any]:
        gross = (exit_price / entry - 1.0) * 100.0
        return {
            "case_id": candidate["case_id"], "session": candidate["session"],
            "symbol": candidate["symbol"], "policy_id": policy_id, "status": status,
            "entry": round(entry, 6), "exit": round(exit_price, 6),
            "gross_return_pct": round(gross, 6),
            "net_return_pct": round(gross - 0.25, 6),
        }

    @classmethod
    def _simulate_policy(cls, candidate: dict[str, Any], bars: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any] | None:
        if not bars:
            return None
        entry = float(bars[0]["o"])
        if entry <= 0:
            return None
        policy_id = str(policy["id"])
        hard_stop = entry * (1.0 - float(policy["hard_stop_pct"]) / 100.0)
        stop = hard_stop
        peak = entry
        partial_realized = 0.0
        remaining = 1.0
        first_taken = False
        trail_active = False
        for bar in bars:
            open_price = float(bar["o"])
            high = float(bar["h"])
            low = float(bar["l"])
            if open_price <= stop:
                exit_price = open_price
                if remaining < 1.0:
                    gross = partial_realized + remaining * ((exit_price / entry - 1.0) * 100.0)
                    row = cls._exit_row(candidate, policy_id, entry * (1.0 + gross / 100.0), entry, "gap_stop_after_partial")
                    return row
                return cls._exit_row(candidate, policy_id, exit_price, entry, "gap_stop")

            if low <= stop:
                if remaining < 1.0:
                    gross = partial_realized + remaining * ((stop / entry - 1.0) * 100.0)
                    row = cls._exit_row(candidate, policy_id, entry * (1.0 + gross / 100.0), entry, "stop_after_partial")
                    return row
                return cls._exit_row(candidate, policy_id, stop, entry, "stop")

            if policy_id in {"TP2_STOP3", "TP3_STOP3"}:
                target = entry * (1.0 + float(policy["target_pct"]) / 100.0)
                if open_price >= target or high >= target:
                    return cls._exit_row(candidate, policy_id, target, entry, "target")

            elif policy_id == "TP5_BE2_STOP3":
                target = entry * (1.0 + float(policy["target_pct"]) / 100.0)
                if open_price >= target or high >= target:
                    return cls._exit_row(candidate, policy_id, target, entry, "target")
                if high >= entry * (1.0 + float(policy["breakeven_after_pct"]) / 100.0):
                    stop = max(stop, entry)  # active from the next bar; conservative intrabar treatment

            elif policy_id == "TRAIL15_AFTER2_STOP3":
                peak = max(peak, high)
                if high >= entry * (1.0 + float(policy["trail_after_pct"]) / 100.0):
                    trail_active = True
                if trail_active:
                    stop = max(stop, peak * (1.0 - float(policy["trail_pct"]) / 100.0))

            elif policy_id == "HALF2_REST5_BE_STOP3":
                first_target = entry * (1.0 + float(policy["first_target_pct"]) / 100.0)
                final_target = entry * (1.0 + float(policy["final_target_pct"]) / 100.0)
                if high >= final_target:
                    if not first_taken:
                        partial_realized = float(policy["first_fraction"]) * float(policy["first_target_pct"])
                        remaining = 1.0 - float(policy["first_fraction"])
                    gross = partial_realized + remaining * float(policy["final_target_pct"])
                    return cls._exit_row(candidate, policy_id, entry * (1.0 + gross / 100.0), entry, "final_target")
                if not first_taken and high >= first_target:
                    first_taken = True
                    fraction = float(policy["first_fraction"])
                    partial_realized = fraction * float(policy["first_target_pct"])
                    remaining = 1.0 - fraction
                    stop = max(stop, entry)  # active from the next bar
        last_close = float(bars[-1]["c"])
        if remaining < 1.0:
            gross = partial_realized + remaining * ((last_close / entry - 1.0) * 100.0)
            return cls._exit_row(candidate, policy_id, entry * (1.0 + gross / 100.0), entry, "time_after_partial")
        return cls._exit_row(candidate, policy_id, last_close, entry, "time")

    def _passes(self, performance: dict[str, Any], minimum_trades: int) -> bool:
        rules = self.protocol["development_policy"]
        pf = performance.get("profit_factor")
        probability = performance.get("session_cluster_bootstrap_positive_probability")
        return bool(
            performance["trades"] >= minimum_trades
            and pf is not None and pf >= float(rules["profit_factor_min"])
            and performance["average_net_return_pct"] is not None and performance["average_net_return_pct"] > 0
            and performance["maximum_drawdown_pct_points"] <= float(rules["maximum_drawdown_pct"])
            and probability is not None and probability >= float(rules["session_cluster_bootstrap_positive_probability_min"])
        )

    def run_development(self, progress: Callable[[int, int, str | None], None] | None = None) -> dict[str, Any]:
        self.lock_protocol()
        rows = self.phase2.build_dataset()
        development = [row for row in rows if row["partition"] == "development"]
        legacy_holdout = [row for row in rows if row["partition"] == "holdout"]
        candidates = self._quality_oof_candidates(development)
        bars_by_case = self._load_bars(candidates, progress)
        usable = [row for row in candidates if bars_by_case.get(row["case_id"])]
        sessions = sorted({row["session"] for row in usable})
        split_index = max(1, min(len(sessions) - 1, int(round(len(sessions) * 0.65))))
        design_sessions = set(sessions[:split_index])
        validation_sessions = set(sessions[split_index:])
        policies = self.protocol["execution"]["policies"]
        design_reports = []
        for policy in policies:
            simulations = [
                self._simulate_policy(row, bars_by_case[row["case_id"]], policy)
                for row in usable if row["session"] in design_sessions
            ]
            simulations = [row for row in simulations if row is not None]
            performance = self.phase2._performance(simulations)
            design_reports.append({
                "policy_id": policy["id"], "performance": performance,
                "passes_design_gate": self._passes(performance, int(self.protocol["development_policy"]["minimum_design_trades"])),
            })
        design_passing = [row for row in design_reports if row["passes_design_gate"]]
        frozen_policy_id = max(
            design_passing,
            key=lambda row: (row["performance"]["profit_factor"], row["performance"]["average_net_return_pct"]),
            default=None,
        )
        frozen_policy_id = frozen_policy_id["policy_id"] if frozen_policy_id else None
        validation_report = None
        development_candidate_found = False
        if frozen_policy_id is not None:
            policy = next(item for item in policies if item["id"] == frozen_policy_id)
            simulations = [
                self._simulate_policy(row, bars_by_case[row["case_id"]], policy)
                for row in usable if row["session"] in validation_sessions
            ]
            simulations = [row for row in simulations if row is not None]
            performance = self.phase2._performance(simulations)
            development_candidate_found = self._passes(
                performance, int(self.protocol["development_policy"]["minimum_validation_trades"])
            )
            validation_report = {
                "policy_id": frozen_policy_id,
                "performance": performance,
                "passes_validation_gate": development_candidate_found,
            }
        report = {
            "schema": 1,
            "generated_at": now_iso(),
            "protocol_sha256": self.protocol_sha256,
            "candidate_selection": {
                "development_rows": len(development),
                "oof_top_5pct_candidates": len(candidates),
                "usable_minute_paths": len(usable),
                "excluded_missing_bars": len(candidates) - len(usable),
                "candidate_explosion_ge10_count": sum(row["explosion_ge10"] for row in usable),
                "candidate_explosion_ge10_rate_pct": round(sum(row["explosion_ge10"] for row in usable) / max(1, len(usable)) * 100, 6),
            },
            "exit_split": {
                "design_sessions": sorted(design_sessions),
                "validation_sessions": sorted(validation_sessions),
                "design_candidate_count": sum(row["session"] in design_sessions for row in usable),
                "validation_candidate_count": sum(row["session"] in validation_sessions for row in usable),
            },
            "design_policy_results": design_reports,
            "frozen_policy_id": frozen_policy_id,
            "validation_result": validation_report,
            "development_candidate_found": development_candidate_found,
            "legacy_holdout_audit": None,
            "next_required_stage": "legacy holdout audit then new shadow sessions" if development_candidate_found else "stop: no frozen exit policy validated",
            "alerts_enabled": False,
            "orders_enabled": False,
            "live_approved": False,
        }
        self.redis.set_json(self.key("development_report"), report)
        return report

    def result(self) -> dict[str, Any] | None:
        return self.redis.get_json(self.key("development_report"), None)

    def status(self) -> dict[str, Any] | None:
        return self.redis.get_json(self.key("status"), None)
