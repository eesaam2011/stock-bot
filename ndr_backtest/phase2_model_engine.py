from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np

from ndr_backtest_engine import BacktestCollector, now_iso, parse_dt


NY = ZoneInfo("America/New_York")
PROTOCOL_PATH = Path(__file__).with_name("phase2_model_protocol.json")
EMBEDDED_PROTOCOL_JSON = r'''
{
  "protocol_id": "NDR-PHASE2-LOGIT-2026-09-03-A",
  "created_at": "2026-09-03T00:00:00Z",
  "purpose": "Build an interpretable explosion-opportunity model from existing causal NDR research cases. Research/shadow use only.",
  "data": {
    "universe": "All stored Approx BREAKOUT_READY signals in REGULAR with joined price-change and ER45 case records",
    "development_partition": "The original 45 development sessions",
    "legacy_holdout_partition": "Historical audit only because its aggregate results were previously inspected",
    "final_validation": "At least 20 genuinely new chronological shadow sessions",
    "decision_cost_pct_round_trip": 0.25,
    "classification_label": "recomputed MFE at least 10 percent during T+1 through T+60",
    "trading_label": "existing conservative full-T1 simulated return through T+60"
  },
  "model": {
    "algorithm": "L2-regularized logistic regression",
    "features": ["price_change_pct_last45m", "er45", "price_change_x_er45", "log_signal_price", "opportunity", "failure_pressure", "minutes_since_regular_open"],
    "l2_penalties": [0.1, 1.0, 10.0],
    "probability_thresholds": [0.5, 0.6, 0.7, 0.8],
    "internal_validation": "three expanding-window chronological folds inside development only",
    "holdout_used_for_selection": false
  },
  "selection_gate": {
    "minimum_internal_validation_trades": 100,
    "profit_factor_min_at_decision_cost": 1.15,
    "average_net_return_must_be_positive": true,
    "maximum_drawdown_pct": 20.0,
    "session_cluster_bootstrap_positive_probability_min": 0.95
  },
  "final_forward_gate": {
    "minimum_new_sessions": 20,
    "minimum_trades": 100,
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


def load_phase2_protocol(path: Path = PROTOCOL_PATH) -> tuple[dict[str, Any], str]:
    # Android file pickers sometimes hide .json files. The exact frozen protocol
    # is embedded so deployment remains reproducible even when the companion
    # JSON file is not uploaded. If present, the file must match this copy.
    embedded = json.loads(EMBEDDED_PROTOCOL_JSON)
    if path.exists():
        protocol = json.loads(path.read_text(encoding="utf-8"))
        if _canonical_json(protocol) != _canonical_json(embedded):
            raise RuntimeError("Phase-2 protocol file does not match the embedded frozen protocol")
    else:
        protocol = embedded
    fingerprint = hashlib.sha256(_canonical_json(protocol).encode("utf-8")).hexdigest()
    return protocol, fingerprint


class Phase2ModelEngine:
    """Interpretable research model built only from existing causal case records.

    It has deliberately no Telegram, broker, alert, or order integration.
    """

    VERSION = "phase2_model_v1"
    FEATURE_NAMES = (
        "price_change_pct_last45m",
        "er45",
        "price_change_x_er45",
        "log_signal_price",
        "opportunity",
        "failure_pressure",
        "minutes_since_regular_open",
    )

    def __init__(self, collector: BacktestCollector | None = None):
        self.base = collector or BacktestCollector()
        self.redis = self.base.redis
        self.protocol, self.protocol_sha256 = load_phase2_protocol()

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
            raise RuntimeError("Frozen Phase-2 protocol mismatch; use a new result version")
        if existing is None:
            self.redis.set_json(key, record)
        return existing or record

    def readiness(self) -> dict[str, Any]:
        manifest = self.redis.get_json(self.base.key("manifest"), {})
        pc_count = int(self.redis.command("HLEN", self.base.key("pcprofit:v2:cases")) or 0)
        er_count = int(self.redis.command("HLEN", self.base.key("pcprofit_er45:v1:cases")) or 0)
        development_sessions = list(manifest.get("development_sessions", []))
        legacy_holdout_sessions = list(manifest.get("holdout_sessions", []))
        ready = bool(development_sessions and pc_count and er_count)
        return {
            "ready_for_phase2_development": ready,
            "development_sessions": len(development_sessions),
            "legacy_holdout_sessions": len(legacy_holdout_sessions),
            "price_change_cases": pc_count,
            "er45_cases": er_count,
            "uses_existing_redis_cases_only": True,
            "legacy_holdout_role": "historical_audit_only",
            "protocol_sha256": self.protocol_sha256,
            "alerts_enabled": False,
            "orders_enabled": False,
            "live_approved": False,
        }

    def _scan_hash_json(self, key: str) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        cursor = "0"
        while True:
            scanned = self.redis.command("HSCAN", key, cursor, "COUNT", 500)
            cursor = str(scanned[0])
            pairs = scanned[1] or []
            for index in range(0, len(pairs), 2):
                output[str(pairs[index])] = json.loads(pairs[index + 1])
            if cursor == "0":
                return output

    @staticmethod
    def _minutes_since_open(t_iso: str) -> float:
        stamp = parse_dt(t_iso).astimezone(NY)
        return float(stamp.hour * 60 + stamp.minute - 570)

    def build_dataset(self, progress: Callable[[int, int, str | None], None] | None = None) -> list[dict[str, Any]]:
        pc_rows = self._scan_hash_json(self.base.key("pcprofit:v2:cases"))
        er_rows = self._scan_hash_json(self.base.key("pcprofit_er45:v1:cases"))
        signal_rows: dict[str, dict[str, Any]] = {}
        scanned = 0
        for row in self.base.iter_results():
            scanned += 1
            signal = row.get("breakout_ready")
            if row.get("mode") != "approx" or not signal or signal.get("phase") != "REGULAR":
                continue
            key = f"{row['session']}|{row['symbol']}|{signal.get('ts')}"
            signal_rows[key] = signal
            if progress and scanned % 10000 == 0:
                progress(scanned, 0, row.get("session"))

        common = sorted(set(pc_rows) & set(er_rows) & set(signal_rows))
        output: list[dict[str, Any]] = []
        for index, key in enumerate(common, 1):
            pc = pc_rows[key]
            er = er_rows[key]
            signal = signal_rows[key]
            required = (
                pc.get("price_change_pct_last45m"), er.get("er45"), pc.get("return_pct"),
                pc.get("recomputed_mfe_pct"), signal.get("price"), signal.get("opportunity"),
                signal.get("failure_pressure"), signal.get("ts"),
            )
            if any(value is None for value in required) or not pc.get("has_plan") or not er.get("has_plan"):
                continue
            if abs(float(pc["return_pct"]) - float(er["return_pct"])) > 1e-8:
                raise RuntimeError(f"Joined return mismatch for {key}")
            price = float(signal["price"])
            if price <= 0:
                continue
            change = float(pc["price_change_pct_last45m"])
            efficiency = float(er["er45"])
            features = {
                "price_change_pct_last45m": change,
                "er45": efficiency,
                "price_change_x_er45": change * efficiency,
                "log_signal_price": math.log(price),
                "opportunity": float(signal["opportunity"]),
                "failure_pressure": float(signal["failure_pressure"]),
                "minutes_since_regular_open": self._minutes_since_open(str(signal["ts"])),
            }
            output.append({
                "case_id": key,
                "session": str(pc["session"]),
                "partition": str(pc["partition"]),
                "symbol": str(pc["symbol"]),
                "t": str(pc["t"]),
                "features": features,
                "explosion_ge10": bool(float(pc["recomputed_mfe_pct"]) >= 10.0),
                "gross_return_pct": float(pc["return_pct"]),
                "net_return_pct": float(pc["return_pct"]) - float(self.protocol["data"]["decision_cost_pct_round_trip"]),
            })
            if progress and index % 5000 == 0:
                progress(index, len(common), str(pc.get("session")))
        output.sort(key=lambda row: (row["session"], row["t"], row["symbol"]))
        return output

    @staticmethod
    def _matrix(rows: list[dict[str, Any]]) -> np.ndarray:
        return np.asarray([[float(row["features"][name]) for name in Phase2ModelEngine.FEATURE_NAMES] for row in rows], dtype=float)

    @staticmethod
    def _fit(X: np.ndarray, y: np.ndarray, l2: float, max_iter: int = 100) -> dict[str, Any]:
        mean = X.mean(axis=0)
        scale = X.std(axis=0)
        scale[scale < 1e-12] = 1.0
        Z = (X - mean) / scale
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
            try:
                step = np.linalg.solve(information, gradient)
            except np.linalg.LinAlgError as exc:
                raise RuntimeError("Logistic fit is singular") from exc
            beta_next = beta + step
            if float(np.max(np.abs(beta_next - beta))) < 1e-8:
                beta = beta_next
                converged = True
                break
            beta = beta_next
        return {"mean": mean, "scale": scale, "beta": beta, "converged": converged, "iterations": iteration + 1}

    @staticmethod
    def _predict(model: dict[str, Any], X: np.ndarray) -> np.ndarray:
        Z = (X - model["mean"]) / model["scale"]
        design = np.column_stack((np.ones(len(Z)), Z))
        logits = np.clip(design @ model["beta"], -35, 35)
        return 1.0 / (1.0 + np.exp(-logits))

    @staticmethod
    def _auc(y: np.ndarray, probability: np.ndarray) -> float | None:
        positive = int(y.sum())
        negative = len(y) - positive
        if not positive or not negative:
            return None
        order = np.argsort(probability, kind="mergesort")
        ranks = np.empty(len(probability), dtype=float)
        index = 0
        while index < len(order):
            end = index + 1
            while end < len(order) and probability[order[end]] == probability[order[index]]:
                end += 1
            ranks[order[index:end]] = (index + 1 + end) / 2.0
            index = end
        rank_sum = float(ranks[y == 1].sum())
        return (rank_sum - positive * (positive + 1) / 2.0) / (positive * negative)

    @staticmethod
    def _performance(rows: list[dict[str, Any]], bootstrap_samples: int = 2000) -> dict[str, Any]:
        returns = [float(row["net_return_pct"]) for row in rows]
        wins = sum(value > 0 for value in returns)
        gross_profit = sum(value for value in returns if value > 0)
        gross_loss = -sum(value for value in returns if value < 0)
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in returns:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        by_session: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            by_session[row["session"]].append(float(row["net_return_pct"]))
        probability_positive = None
        if by_session:
            rng = np.random.default_rng(20260903)
            session_totals = np.asarray([sum(values) for _, values in sorted(by_session.items())], dtype=float)
            totals = rng.choice(session_totals, size=(bootstrap_samples, len(session_totals)), replace=True).sum(axis=1)
            probability_positive = float(np.mean(totals > 0))
        return {
            "trades": len(rows),
            "active_sessions": len(by_session),
            "wins": wins,
            "losses": sum(value < 0 for value in returns),
            "win_rate_pct": round(wins / len(returns) * 100, 4) if returns else None,
            "average_net_return_pct": round(sum(returns) / len(returns), 6) if returns else None,
            "total_net_return_points": round(sum(returns), 6),
            "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
            "maximum_drawdown_pct_points": round(max_drawdown, 6),
            "session_cluster_bootstrap_positive_probability": round(probability_positive, 6) if probability_positive is not None else None,
        }

    @staticmethod
    def _chronological_folds(rows: list[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        sessions = sorted({row["session"] for row in rows})
        if len(sessions) < 12:
            raise RuntimeError("At least 12 development sessions are required")
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
            raise RuntimeError("Unable to construct three chronological folds")
        return folds

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

    def run_development(self, progress: Callable[[int, int, str | None], None] | None = None) -> dict[str, Any]:
        self.lock_protocol()
        rows = self.build_dataset(progress)
        development = [row for row in rows if row["partition"] == "development"]
        legacy_holdout = [row for row in rows if row["partition"] == "holdout"]
        folds = self._chronological_folds(development)
        candidates = []
        for l2 in self.protocol["model"]["l2_penalties"]:
            oof: list[tuple[dict[str, Any], float]] = []
            fold_reports = []
            for train, valid in folds:
                model = self._fit(self._matrix(train), np.asarray([row["explosion_ge10"] for row in train], dtype=float), float(l2))
                probability = self._predict(model, self._matrix(valid))
                oof.extend(zip(valid, probability.tolist()))
                y_valid = np.asarray([row["explosion_ge10"] for row in valid], dtype=int)
                fold_reports.append({
                    "train_first_session": train[0]["session"], "train_last_session": train[-1]["session"],
                    "validation_first_session": valid[0]["session"], "validation_last_session": valid[-1]["session"],
                    "train_rows": len(train), "validation_rows": len(valid),
                    "classification_auc": round(self._auc(y_valid, probability), 6) if self._auc(y_valid, probability) is not None else None,
                })
            y_oof = np.asarray([row["explosion_ge10"] for row, _ in oof], dtype=int)
            p_oof = np.asarray([probability for _, probability in oof], dtype=float)
            for threshold in self.protocol["model"]["probability_thresholds"]:
                selected = [row for row, probability in oof if probability >= float(threshold)]
                performance = self._performance(selected)
                candidates.append({
                    "l2_penalty": float(l2), "probability_threshold": float(threshold),
                    "oof_rows": len(oof),
                    "oof_classification_auc": round(self._auc(y_oof, p_oof), 6) if self._auc(y_oof, p_oof) is not None else None,
                    "folds": fold_reports, "trading_performance": performance,
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
            model = self._fit(
                self._matrix(development),
                np.asarray([row["explosion_ge10"] for row in development], dtype=float),
                selected_spec["l2_penalty"],
            )
            fitted_model = {
                "feature_names": list(self.FEATURE_NAMES),
                "standardization_mean": [round(float(value), 10) for value in model["mean"]],
                "standardization_scale": [round(float(value), 10) for value in model["scale"]],
                "intercept_and_standardized_coefficients": [round(float(value), 10) for value in model["beta"]],
                "l2_penalty": selected_spec["l2_penalty"],
                "probability_threshold": selected_spec["probability_threshold"],
                "converged": model["converged"],
                "iterations": model["iterations"],
            }
            if legacy_holdout:
                probability = self._predict(model, self._matrix(legacy_holdout))
                selected = [row for row, score in zip(legacy_holdout, probability) if score >= selected_spec["probability_threshold"]]
                y_holdout = np.asarray([row["explosion_ge10"] for row in legacy_holdout], dtype=int)
                legacy_audit = {
                    "role": "historical_audit_only_not_model_selection_or_live_approval",
                    "rows": len(legacy_holdout),
                    "classification_auc": round(self._auc(y_holdout, probability), 6) if self._auc(y_holdout, probability) is not None else None,
                    "trading_performance": self._performance(selected),
                    "can_approve_live": False,
                }
        report = {
            "schema": 1,
            "generated_at": now_iso(),
            "protocol_sha256": self.protocol_sha256,
            "source_prefix": self.base.prefix,
            "dataset": {
                "joined_rows": len(rows), "development_rows": len(development),
                "legacy_holdout_rows": len(legacy_holdout),
                "feature_names": list(self.FEATURE_NAMES),
                "cost_pct_round_trip": self.protocol["data"]["decision_cost_pct_round_trip"],
            },
            "candidate_search": candidates,
            "selected_spec": selected_spec,
            "fitted_model": fitted_model,
            "legacy_holdout_audit": legacy_audit,
            "development_candidate_found": selected_spec is not None,
            "next_required_stage": "20 genuinely new chronological shadow sessions" if selected_spec else "stop: no model passed the frozen internal profitability gate",
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
