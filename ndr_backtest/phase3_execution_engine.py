from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ndr_backtest_engine import now_iso
from phase2_model_engine_v2 import Phase2ModelEngine


PROTOCOL_PATH = Path(__file__).with_name("phase3_execution_protocol.json")
EMBEDDED_PROTOCOL_JSON = r'''
{
  "protocol_id": "NDR-PHASE3-DUAL-HEAD-2026-09-03-A",
  "created_at": "2026-09-03T00:00:00Z",
  "purpose": "Test whether a separate execution-profitability head can convert the validated explosion ranking signal into positive net trading expectancy.",
  "data": {
    "source": "The same joined causal cases used by Phase 2 V2",
    "development_partition": "The original 45 development sessions",
    "legacy_holdout_partition": "Historical audit only and never used for selection",
    "decision_cost_pct_round_trip": 0.25
  },
  "model": {
    "algorithm": "Two L2-regularized logistic regressions",
    "quality_target": "recomputed MFE at least 10 percent during T+1 through T+60",
    "execution_target": "net simulated return after 0.25 percent round-trip cost is positive",
    "combined_score": "square root of quality probability multiplied by execution probability",
    "l2_penalty": 1.0,
    "selection_fractions": [0.02, 0.05, 0.10, 0.20],
    "internal_validation": "three expanding-window chronological folds; each fold ranked independently",
    "features": ["price_change_pct_last45m", "er45", "price_change_x_er45", "log_signal_price", "opportunity", "failure_pressure", "minutes_since_regular_open"]
  },
  "selection_gate": {
    "minimum_internal_validation_trades": 100,
    "profit_factor_min_at_decision_cost": 1.15,
    "average_net_return_must_be_positive": true,
    "maximum_drawdown_pct": 20.0,
    "session_cluster_bootstrap_positive_probability_min": 0.95
  },
  "safety": {"alerts_enabled": false, "orders_enabled": false, "live_approved": false, "legacy_holdout_can_approve_live": false}
}
'''


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_phase3_protocol(path: Path = PROTOCOL_PATH) -> tuple[dict[str, Any], str]:
    embedded = json.loads(EMBEDDED_PROTOCOL_JSON)
    if path.exists():
        protocol = json.loads(path.read_text(encoding="utf-8"))
        if _canonical_json(protocol) != _canonical_json(embedded):
            raise RuntimeError("Phase-3 protocol file does not match the embedded frozen protocol")
    else:
        protocol = embedded
    fingerprint = hashlib.sha256(_canonical_json(protocol).encode("utf-8")).hexdigest()
    return protocol, fingerprint


class Phase3ExecutionEngine:
    VERSION = "phase3_dual_head_v1"

    def __init__(self, phase2: Phase2ModelEngine | None = None):
        self.phase2 = phase2 or Phase2ModelEngine()
        self.base = self.phase2.base
        self.redis = self.phase2.redis
        self.protocol, self.protocol_sha256 = load_phase3_protocol()

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
            raise RuntimeError("Frozen Phase-3 protocol mismatch; use a new result version")
        if existing is None:
            self.redis.set_json(key, record)
        return existing or record

    def readiness(self) -> dict[str, Any]:
        source = self.phase2.readiness()
        return {
            "ready_for_phase3_development": bool(source["ready_for_phase2_development"]),
            "development_sessions": source["development_sessions"],
            "legacy_holdout_sessions": source["legacy_holdout_sessions"],
            "price_change_cases": source["price_change_cases"],
            "er45_cases": source["er45_cases"],
            "uses_existing_redis_cases_only": True,
            "protocol_sha256": self.protocol_sha256,
            "alerts_enabled": False,
            "orders_enabled": False,
            "live_approved": False,
        }

    @staticmethod
    def _combined(quality_probability: np.ndarray, execution_probability: np.ndarray) -> np.ndarray:
        return np.sqrt(np.clip(quality_probability * execution_probability, 0.0, 1.0))

    @staticmethod
    def _diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
        mfes = [float(row["recomputed_mfe_pct"]) for row in rows if row.get("recomputed_mfe_pct") is not None]
        maes = [float(row["recomputed_mae_pct"]) for row in rows if row.get("recomputed_mae_pct") is not None]
        explosions = sum(bool(row["explosion_ge10"]) for row in rows)
        return {
            "selected_rows": len(rows),
            "explosion_ge10_count": explosions,
            "explosion_ge10_rate_pct": round(explosions / max(1, len(rows)) * 100, 6),
            "average_mfe_pct": round(sum(mfes) / len(mfes), 6) if mfes else None,
            "average_mae_pct": round(sum(maes) / len(maes), 6) if maes else None,
        }

    def _gate(self, performance: dict[str, Any]) -> bool:
        gate = self.protocol["selection_gate"]
        pf = performance.get("profit_factor")
        probability = performance.get("session_cluster_bootstrap_positive_probability")
        return bool(
            performance["trades"] >= int(gate["minimum_internal_validation_trades"])
            and pf is not None and pf >= float(gate["profit_factor_min_at_decision_cost"])
            and performance["average_net_return_pct"] is not None and performance["average_net_return_pct"] > 0
            and performance["maximum_drawdown_pct_points"] <= float(gate["maximum_drawdown_pct"])
            and probability is not None and probability >= float(gate["session_cluster_bootstrap_positive_probability_min"])
        )

    @staticmethod
    def _serialize_model(model: dict[str, Any]) -> dict[str, Any]:
        return {
            "standardization_mean": [round(float(value), 10) for value in model["mean"]],
            "standardization_scale": [round(float(value), 10) for value in model["scale"]],
            "intercept_and_standardized_coefficients": [round(float(value), 10) for value in model["beta"]],
            "converged": bool(model["converged"]),
            "iterations": int(model["iterations"]),
        }

    def run_development(self, progress: Callable[[int, int, str | None], None] | None = None) -> dict[str, Any]:
        self.lock_protocol()
        rows = self.phase2.build_dataset(progress)
        development = [row for row in rows if row["partition"] == "development"]
        legacy_holdout = [row for row in rows if row["partition"] == "holdout"]
        folds = self.phase2._chronological_folds(development)
        l2 = float(self.protocol["model"]["l2_penalty"])
        oof: list[tuple[dict[str, Any], float, int]] = []
        fold_reports = []
        for fold_index, (train, valid) in enumerate(folds):
            X_train = self.phase2._matrix(train)
            quality_model = self.phase2._fit(X_train, np.asarray([row["explosion_ge10"] for row in train], dtype=float), l2)
            execution_model = self.phase2._fit(X_train, np.asarray([row["net_return_pct"] > 0 for row in train], dtype=float), l2)
            X_valid = self.phase2._matrix(valid)
            quality_probability = self.phase2._predict(quality_model, X_valid)
            execution_probability = self.phase2._predict(execution_model, X_valid)
            combined = self._combined(quality_probability, execution_probability)
            oof.extend((row, score, fold_index) for row, score in zip(valid, combined.tolist()))
            fold_reports.append({
                "fold": fold_index + 1,
                "train_first_session": train[0]["session"], "train_last_session": train[-1]["session"],
                "validation_first_session": valid[0]["session"], "validation_last_session": valid[-1]["session"],
                "train_rows": len(train), "validation_rows": len(valid),
                "quality_auc": round(self.phase2._auc(np.asarray([row["explosion_ge10"] for row in valid], dtype=int), quality_probability), 6),
                "execution_auc": round(self.phase2._auc(np.asarray([row["net_return_pct"] > 0 for row in valid], dtype=int), execution_probability), 6),
            })
        candidates = []
        for fraction in self.protocol["model"]["selection_fractions"]:
            selected = []
            cutoffs = []
            for fold_index in range(len(folds)):
                fold_rows = [(row, score) for row, score, index in oof if index == fold_index]
                cutoff = float(np.quantile([score for _, score in fold_rows], 1.0 - float(fraction)))
                chosen = [row for row, score in fold_rows if score >= cutoff]
                selected.extend(chosen)
                cutoffs.append({"fold": fold_index + 1, "combined_score_cutoff": round(cutoff, 10), "selected_rows": len(chosen)})
            performance = self.phase2._performance(selected)
            candidates.append({
                "selection_fraction": float(fraction),
                "oof_rows": len(oof),
                "fold_cutoffs": cutoffs,
                "selected_diagnostics": self._diagnostics(selected),
                "trading_performance": performance,
                "passes_internal_gate": self._gate(performance),
            })
        passing = [candidate for candidate in candidates if candidate["passes_internal_gate"]]
        selected_spec = max(
            passing,
            key=lambda candidate: (
                candidate["trading_performance"]["profit_factor"],
                candidate["trading_performance"]["average_net_return_pct"],
                candidate["trading_performance"]["trades"],
            ),
            default=None,
        )
        fitted_model = None
        legacy_audit = None
        if selected_spec is not None:
            X_development = self.phase2._matrix(development)
            quality_model = self.phase2._fit(X_development, np.asarray([row["explosion_ge10"] for row in development], dtype=float), l2)
            execution_model = self.phase2._fit(X_development, np.asarray([row["net_return_pct"] > 0 for row in development], dtype=float), l2)
            development_score = self._combined(
                self.phase2._predict(quality_model, X_development),
                self.phase2._predict(execution_model, X_development),
            )
            cutoff = float(np.quantile(development_score, 1.0 - selected_spec["selection_fraction"]))
            fitted_model = {
                "feature_names": list(self.phase2.FEATURE_NAMES),
                "l2_penalty": l2,
                "selection_fraction": selected_spec["selection_fraction"],
                "frozen_combined_score_cutoff": round(cutoff, 10),
                "quality_head": self._serialize_model(quality_model),
                "execution_head": self._serialize_model(execution_model),
            }
            if legacy_holdout:
                X_holdout = self.phase2._matrix(legacy_holdout)
                score = self._combined(
                    self.phase2._predict(quality_model, X_holdout),
                    self.phase2._predict(execution_model, X_holdout),
                )
                selected = [row for row, value in zip(legacy_holdout, score) if value >= cutoff]
                legacy_audit = {
                    "role": "historical_audit_only_not_model_selection_or_live_approval",
                    "selected_diagnostics": self._diagnostics(selected),
                    "trading_performance": self.phase2._performance(selected),
                    "can_approve_live": False,
                }
        report = {
            "schema": 1,
            "generated_at": now_iso(),
            "protocol_sha256": self.protocol_sha256,
            "dataset": {"joined_rows": len(rows), "development_rows": len(development), "legacy_holdout_rows": len(legacy_holdout)},
            "folds": fold_reports,
            "candidate_search": candidates,
            "selected_spec": selected_spec,
            "fitted_model": fitted_model,
            "legacy_holdout_audit": legacy_audit,
            "development_candidate_found": selected_spec is not None,
            "next_required_stage": "20 genuinely new chronological shadow sessions" if selected_spec else "targeted one-minute exit-path research on top-ranked cases",
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
