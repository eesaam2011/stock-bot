from __future__ import annotations

import hashlib
import fnmatch
import gzip
import json
import logging
import math
import os
import re
import sqlite3
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

VERSION = "1.7.25-R1"
BUILD = "INDEPENDENT-PRIORITY-RADAR-2026-09-13-EARLY-CAUSAL-ENTRY-RESEARCH-EXECUTION-THRESHOLD-AMENDMENT-FREEZE"
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
PHASE0_PROBE_SPEC = {
    "probe_id": "IPR-HISTORICAL-EXPLOSION-PHASE0-PROBE-2026-09-05-B",
    "purpose": "Fail-closed dual-detector capability probe before any Phase 0A historical explosion census.",
    "trading_cycle": "previous official regular close -> target session official regular close",
    "primary_threshold_pct": 20.0,
    "full_cycle_detector": "forward streaming running-min detector on chronologically merged one-minute closes across the full trading cycle",
    "session_detector": "the same forward streaming running-min detector, but restricted to the frozen expected session only",
    "candidate_sources": ["Alpaca SIP 1Min raw", "Alpaca BOATS 1Min raw for overnight"],
    "acceptance": {
        "all_frozen_reference_cases_must_have_expected_session_coverage": True,
        "all_frozen_reference_cases_must_detect_ge20_in_full_cycle": True,
        "all_frozen_reference_cases_must_detect_ge20_inside_expected_session": True,
        "full_cycle_t1_session_is_diagnostic_only": True,
        "synthetic_streaming_detector_tests_must_pass": True,
        "phase0a_is_fail_closed": True,
    },
    "frozen_reference_cases": [
        {"symbol":"INHD","target_session":"2026-08-28","expected_session":"AH","expected_ge20_in_cycle":True,"expected_ge20_in_session":True,"provenance":"raw one-minute reference established before this dual-detector probe"},
        {"symbol":"ELPW","target_session":"2026-06-09","expected_session":"Overnight","expected_ge20_in_cycle":True,"expected_ge20_in_session":True,"provenance":"selected from legacy NDR catalog, then independently reviewed on raw Alpaca one-minute bars before this dual-detector probe"},
        {"symbol":"OFAL","target_session":"2026-08-12","expected_session":"Premarket","expected_ge20_in_cycle":True,"expected_ge20_in_session":True,"provenance":"selected from legacy NDR catalog, then independently reviewed on raw Alpaca one-minute bars before this dual-detector probe"},
    ],
    "safety": {"phase0a_runs": False, "feature_discovery_runs": False, "alerts_enabled": False, "orders_enabled": False},
}
PHASE0_PROBE_SHA256 = hashlib.sha256(
    json.dumps(PHASE0_PROBE_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


PHASE0A_SPEC = {
    "census_id": "IPR-HISTORICAL-EXPLOSION-PHASE0A-2026-09-05-A",
    "purpose": "Cheap high-recall historical explosion candidate census only; Phase 0B performs one-minute verification.",
    "source_manifest": "next_day_radar_backtest_v3:manifest",
    "scope": "frozen 60-session full universe from the legacy NDR manifest",
    "trading_cycle": "previous official regular close -> target session official regular close",
    "coarse_timeframe": "30Min",
    "sources": ["Alpaca SIP raw", "Alpaca BOATS raw for Overnight"],
    "candidate_rule": "chronological running minimum of coarse bar lows; candidate when a later-or-same coarse bar high reaches >=20%; same-bar ordering is intentionally unresolved for high recall and must be verified in Phase 0B",
    "primary_threshold_pct": 20.0,
    "retained_ladders_pct": [5.0, 10.0, 15.0, 20.0, 30.0, 50.0],
    "max_events_per_symbol_cycle": 1,
    "corporate_action_policy": "flag coarse split/corporate-action suspects; never promote a flagged case to verified explosion; Phase 0B must perform strict exclusion",
    "resume": "session checkpoint persisted in Redis; completed sessions are never rescanned unless reset explicitly",
    "safety": {"feature_discovery_runs": False, "phase0b_runs": False, "alerts_enabled": False, "orders_enabled": False},
}
PHASE0A_SHA256 = hashlib.sha256(json.dumps(PHASE0A_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

HISTORICAL_UNIVERSE_RECONSTRUCTION_SPEC = {
    "reconstruction_id": "IPR-HISTORICAL-UNIVERSE-RECONSTRUCTION-2026-09-05-A",
    "period": ["2019-01-01", "2026-08-31"],
    "sources": ["Alpaca all US-equity assets", "legacy NDR manifest", "legacy NDR explosion catalog", "frozen historical probe references"],
    "presence_index": "Alpaca SIP raw 1Month bars; presence is indexed by symbol-year and is not an economic-entity identity",
    "ticker_recycling_policy": "Never assume ticker==same company across time. Census identity remains symbol x trading_cycle. Long gaps are flagged as recycling risk; no cross-era entity merge is performed.",
    "dedup_policy": "Exact ticker text is deduplicated only for request efficiency while source provenance is retained. This is not entity deduplication.",
    "recycling_gap_days": 730,
    "batch_size": 200,
    "safety": {"historical_census_runs": False, "phase0b_runs": False, "feature_discovery_runs": False, "orders_enabled": False},
}
HISTORICAL_UNIVERSE_RECONSTRUCTION_SHA256 = hashlib.sha256(json.dumps(HISTORICAL_UNIVERSE_RECONSTRUCTION_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


HISTORICAL_CENSUS_SPEC = {
    "census_id": "IPR-HISTORICAL-EXPLOSION-CENSUS-2019-2026-A",
    "period": ["2019-01-01", "2026-08-31"],
    "universe_source": "historical_universe_reconstruction:v1:records",
    "universe_reconstruction_sha256": HISTORICAL_UNIVERSE_RECONSTRUCTION_SHA256,
    "identity": "symbol x trading_cycle; ticker text is never treated as a permanent company identity",
    "year_presence_gate": "scan a symbol in a target year only when reconstruction recorded SIP presence in that year",
    "trading_cycle": "previous official regular close -> target official regular close using Alpaca market calendar",
    "coarse_timeframe": "1Hour",
    "sources": ["Alpaca SIP raw", "Alpaca BOATS raw when the overnight venue existed"],
    "boats_market_structure": "BOATS went live in June 2021; pre-launch cycles have no BOATS overnight segment by market structure, not missing-data imputation",
    "boats_launch_date": "2021-06-01",
    "candidate_rule": "optimistic chronological coarse low/high envelope for high recall; same-bar order remains ambiguous and must be verified at 1-minute in Phase 0B",
    "primary_threshold_pct": 20.0,
    "retained_ladders_pct": [5.0, 10.0, 15.0, 20.0, 30.0, 50.0],
    "request_batch_size": 500,
    "max_events_per_symbol_cycle": 1,
    "corporate_action_policy": "coarse discontinuities are flagged and excluded from Phase 0B eligibility; strict corporate-action exclusion remains mandatory in Phase 0B",
    "ticker_recycling_policy": "recycling-risk symbols remain separate symbol x cycle observations; no cross-era entity merge",
    "resume": "completed trading sessions and per-session candidates/quality are persisted in Redis",
    "safety": {"phase0b_runs": False, "feature_discovery_runs": False, "alerts_enabled": False, "orders_enabled": False},
}
HISTORICAL_CENSUS_SHA256 = hashlib.sha256(json.dumps(HISTORICAL_CENSUS_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

PHASE0B_WINDOW_PROBE_SPEC = {
    "probe_id": "IPR-PHASE0B-WINDOWING-VALIDATION-PROBE-2026-09-07-A",
    "purpose": "Validate whether a coarse-informed buffered 1-minute window can exactly reproduce full-trading-cycle Phase 0B verification before any large Phase 0B run.",
    "sample_size": 64,
    "sample_design": "Deterministic stratified sample: up to 4 same-bar ambiguous + 4 temporally ordered clean candidates per year, 2019-2026.",
    "ground_truth": "Full Trading Cycle raw 1-minute SIP plus BOATS where market structure permits; chronological running-min using bar low then later bar high.",
    "optimization": "1-minute bars restricted to coarse event span plus 60 minutes on each side; never changes the event definition.",
    "buffer_minutes_each_side": 60,
    "threshold_pct": 20.0,
    "acceptance": "Exact classification agreement for every comparable sampled case; any mismatch blocks window optimization. Same-minute low/high threshold hit is still_ambiguous, never verified.",
    "safety": {"phase0b_full_run": False, "feature_discovery_runs": False, "orders_enabled": False},
}
PHASE0B_WINDOW_PROBE_SHA256 = hashlib.sha256(json.dumps(PHASE0B_WINDOW_PROBE_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

PHASE0B_FULL_SPEC = {
    "verification_id": "IPR-PHASE0B-FULL-CYCLE-VERIFICATION-2019-2026-A",
    "input": "205,028 recommended-clean candidates frozen by v1.7.6",
    "ground_truth": "Full Trading Cycle raw 1-minute SIP plus BOATS where venue existed; no coarse-informed window optimization",
    "threshold_pct": 20.0,
    "retained_ladders_pct": [5.0, 10.0, 15.0, 20.0, 30.0, 50.0],
    "minute_ordering": "running minimum is updated chronologically; if a new minute low and threshold-reaching high occur in that same minute, classify still_ambiguous, never verified",
    "window_optimization": False,
    "resume": "session-boundary checkpoints in Redis; completed sessions are never refetched on resume",
    "batch_size": 200,
    "corporate_action_policy": "only candidates already passing frozen v1.7.6 split screen and common-like/recycling exclusions enter Phase 0B; raw 1-minute verification never overrides an exclusion",
    "safety": {"feature_discovery_runs": False, "alerts_enabled": False, "orders_enabled": False, "stop_and_review_after_completion": True},
}
PHASE0B_FULL_SHA256 = hashlib.sha256(json.dumps(PHASE0B_FULL_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


PHASE0B_DATASET_AUDIT_SPEC = {
    "audit_id": "IPR-PHASE0B-DATASET-AUDIT-2026-09-07-B-HARDENED",
    "purpose": "Read-only integrity and distribution audit of the completed Phase 0B ground-truth dataset before Feature Discovery.",
    "source": "Persisted Redis Phase 0B per-session results only; no Alpaca requests and no minute-bar refetch.",
    "expected_processed": 205028,
    "expected_verified": 169628,
    "expected_sessions": 1926,
    "breakdowns": ["year", "classification", "verified_gain_ladders", "verified_first_20_phase", "unique_symbols", "repeat_verified_events", "bar_coverage", "extreme_verified_events"],
    "integrity": "All partitions must reconcile exactly to persisted Phase 0B totals; fail closed on missing session results or count mismatch.",
    "safety": {"alpaca_requests": False, "feature_discovery_runs": False, "alerts_enabled": False, "orders_enabled": False, "stop_and_review_after_completion": True},
}
PHASE0B_DATASET_AUDIT_SHA256 = hashlib.sha256(json.dumps(PHASE0B_DATASET_AUDIT_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

FEATURE_DISCOVERY_PROTOCOL_SPEC = {
    "protocol_id": "IPR-FEATURE-DISCOVERY-2026-09-07-A",
    "status": "FROZEN_PROTOCOL_ONLY",
    "purpose": "Discover causal pre-explosion features from the audited Phase 0B ground-truth dataset without running feature discovery yet.",
    "source_gate": {
        "required_phase0b_processed": 205028,
        "required_phase0b_verified": 169628,
        "required_unique_verified_symbols": 3724,
        "required_dataset_audit_integrity_passed": True,
        "source_phase0b_sha256": "8864474fe6c513d9eca37791b90df1804674ea9e59ef20017490fb0f7eaa205f",
        "source_audit_id": "IPR-PHASE0B-DATASET-AUDIT-2026-09-07-B-HARDENED",
    },
    "positive_events": {
        "primary": "Phase 0B verified >=20% events only",
        "strength_ladders": [20, 30, 50],
        "still_ambiguous_policy": "excluded from positives and retained only for sensitivity diagnostics",
        "failed_policy": "never relabeled positive",
    },
    "causality": {
        "feature_cutoff": "strictly before the event's first verified +20% threshold timestamp; no post-threshold information",
        "anchors": "universal completed 5-minute anchors before threshold, not trigger-conditioned anchors",
        "anchor_offsets_minutes": [5, 15, 30, 60, 120, 240],
        "no_future_leakage": True,
    },
    "controls": {
        "hard_negatives": "matched on contextual variables only: year/regime, trading phase, price band, liquidity/coverage availability, and comparable symbol-session opportunity; never match on candidate predictive features",
        "random_controls": "separate random eligible symbol-cycle controls from the same broad time/regime pool",
        "match_id": "one immutable match_id binds each positive event to its hard-negative/control set",
        "ratio": "1 positive : up to 3 hard negatives : 1 random control, fail closed when contextual match quality is inadequate",
    },
    "symbol_aware": {
        "unit_of_observation": "event",
        "inference": "symbol-clustered; uncertainty and significance are computed with symbol as the cluster",
        "discovery_weight": "equal-symbol weighting: each symbol contributes total weight 1 across its eligible positive events; event weight = 1 / eligible positive-event count for that symbol",
        "no_arbitrary_event_cap": True,
        "reason": "retain temporal diversity without allowing symbols with hundreds of events to dominate feature discovery",
        "validation_grouping": "all observations for a symbol stay in one validation group; no symbol leakage across grouped folds",
    },
    "feature_families": [
        "price/return path and acceleration",
        "volume and dollar-volume participation",
        "range/volatility expansion and compression",
        "VWAP/location and close-position structure",
        "persistence/recovery/pullback asymmetry",
        "gap and session-transition context",
        "liquidity/spread where historically available",
    ],
    "statistics": {
        "discovery_goal": "effect size and stability first; p-values are secondary",
        "multiple_testing": "Benjamini-Hochberg FDR within each frozen feature family",
        "report": ["weighted positive vs hard-negative effect size", "random-control contrast", "symbol-clustered uncertainty", "year stability", "phase stability", "20/30/50 ladder monotonicity"],
        "min_n_rule": "minimum support thresholds must be frozen from dataset counts before feature results are inspected",
    },
    "splits": {
        "discovery": "2019-2024 only",
        "validation": "2025 only; untouched during feature selection",
        "final_holdout": "2026 through 2026-08-31; locked and not inspected until a feature set and scoring rule are frozen",
        "symbol_grouping_applies_within_discovery_resampling": True,
        "time_order_is_primary_oos_test": True,
    },
    "promotion_gate": {
        "required": [
            "directionally stable effect in discovery years",
            "survives symbol-aware inference",
            "passes frozen FDR/support rules",
            "replicates direction and material effect in 2025 validation",
            "feature set and scoring rule frozen before opening 2026 holdout"
        ],
        "no_strategy_or_entry_claim": "Feature Discovery identifies predictive structure only; it does not establish a tradable strategy or profitability.",
    },
    "safety": {
        "feature_discovery_runs": False,
        "alpaca_requests": False,
        "orders_enabled": False,
        "alerts_enabled": False,
        "protocol_review_required_before_code": True,
    },
}
FEATURE_DISCOVERY_PROTOCOL_SHA256 = hashlib.sha256(json.dumps(FEATURE_DISCOVERY_PROTOCOL_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


FEATURE_DISCOVERY_EXEC_SPEC = {
    "run_id": "IPR-FEATURE-DISCOVERY-2019-2024-2026-09-07-A",
    "protocol_sha256": FEATURE_DISCOVERY_PROTOCOL_SHA256,
    "scope": "Discovery years 2019-2024 only; 2025 validation and 2026 holdout are not read by this run.",
    "source": "Persisted Phase 0B verified/failed events plus raw 5-minute SIP/BOATS bars fetched only for selected Discovery observations.",
    "controls": {
        "eligible_pool": "Phase 0B failed clean candidates in 2019-2024 only; still_ambiguous is never a control.",
        "hard_negative_matching": ["same target session when available", "same trading phase", "same frozen log2 price band", "comparable 5-minute coverage"],
        "hard_negative_ratio": "up to 3 per positive; controls may be reused when a contextual cell is sparse, with reuse counted and reported",
        "random_control": "one separate deterministic random failed clean candidate from the same target-session contextual failed pool (same session is a stricter subset of the frozen broad time/regime requirement); never selected using predictive feature values",
        "pseudo_cutoff": "failed controls use the frozen Phase 0A coarse first-20 timestamp as opportunity-time cutoff; no post-cutoff feature data",
    },
    "anchors_minutes": [5,15,30,60,120,240],
    "bar_timeframe": "5Min",
    "feature_families": FEATURE_DISCOVERY_PROTOCOL_SPEC["feature_families"],
    "min_n_formula": {
        "events": "max(500, ceil(0.5% of all eligible Discovery positive events))",
        "symbols": "max(100, ceil(3% of all eligible Discovery positive symbols))",
        "year_support": "at least 4 distinct Discovery years",
        "freeze_timing": "computed and persisted from Phase 0B labels before any feature-effect calculation",
    },
    "statistics": "equal-symbol weighting; deterministic symbol-cluster multiplier bootstrap uncertainty (Rademacher cluster weights, 2000 replicates); Benjamini-Hochberg FDR within frozen feature family; effect stability by year",
    "safety": {"orders_enabled":False,"alerts_enabled":False,"validation_2025_read":False,"holdout_2026_read":False,"stop_and_review_after_discovery":True},
}
FEATURE_DISCOVERY_EXEC_SHA256 = hashlib.sha256(json.dumps(FEATURE_DISCOVERY_EXEC_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


FEATURE_DISCOVERY_AUDIT_SPEC = {
    "audit_id": "IPR-FEATURE-DISCOVERY-AUDIT-2026-09-07-A",
    "purpose": "Read-only post-Discovery hardening on persisted 2019-2024 observations before any 2025 validation.",
    "source_run_id": FEATURE_DISCOVERY_EXEC_SPEC["run_id"],
    "source_protocol_sha256": FEATURE_DISCOVERY_PROTOCOL_SHA256,
    "source_execution_sha256": FEATURE_DISCOVERY_EXEC_SHA256,
    "scope": "Persisted Discovery observations from 2019-2024 only; no Alpaca fetch and no 2025/2026 read.",
    "diagnostics": {
        "year_stability": "For each Feature x Anchor, recompute equal-symbol standardized positive-vs-hard effect separately in each Discovery year 2019-2024; report sign agreement with the all-years Discovery effect and yearly magnitudes.",
        "phase_stability": "For each Feature x Anchor, recompute equal-symbol standardized positive-vs-hard effect within each observed trading phase; report sign agreement where both classes have >=100 events and >=30 symbols.",
        "ladder_monotonicity": "Within positive events only, compare equal-symbol feature means for >=20, >=30, >=50 strength ladders. Report whether the absolute movement from hard-negative mean is non-decreasing in the all-years Discovery direction; this is diagnostic, not a new selection rule.",
    },
    "integrity": {
        "expected_sessions": 1510,
        "expected_observations": 197125,
        "expected_class_counts": {"positive":92538,"hard_negative":71332,"random_control":33255},
        "expected_min_n": {"eligible_discovery_positive_events":107397,"eligible_discovery_positive_symbols":3260,"min_positive_events":537,"min_positive_symbols":100,"min_years":4,"frozen_at":"2026-09-07T17:31:10.246281Z"},
        "source_report_must_be_completed_stop_review": True,
        "validation_2025_must_remain_closed": True,
        "holdout_2026_must_remain_closed": True,
    },
    "statistics": {
        "weighting": "equal-symbol means within each class/subgroup",
        "effect": "standardized difference positive-vs-hard using pooled SD of symbol means",
        "phase_support_floor": {"events_per_class":100,"symbols_per_class":30},
        "note": "No new p-value/FDR threshold is introduced post hoc; this audit measures stability/monotonicity required by the frozen protocol."
    },
    "safety": {"alpaca_requests":False,"validation_2025_read":False,"holdout_2026_read":False,"orders_enabled":False,"alerts_enabled":False,"stop_and_review_after_audit":True},
}
FEATURE_DISCOVERY_AUDIT_SHA256 = hashlib.sha256(json.dumps(FEATURE_DISCOVERY_AUDIT_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


FEATURE_SCORING_FREEZE_SPEC = {
    "freeze_id": "IPR-FEATURE-SCORING-FREEZE-2026-09-08-B",
    "purpose": "Freeze feature eligibility, early-detection/confirmation/severity roles, score construction and Development-only operating thresholds before opening 2025.",
    "source_audit_id": FEATURE_DISCOVERY_AUDIT_SPEC["audit_id"],
    "scope": "2019-2024 persisted Discovery observations only; no Alpaca fetch and no 2025/2026 read.",
    "pre_selection_eligibility_rule": {
        "principle": "A trading-model feature must directly represent observable price, volume, liquidity, VWAP/location, volatility/range, or persistence/recovery behavior. Data-availability/coverage/history-length mechanics are structural proxies and are ineligible regardless of statistical performance.",
        "structural_proxy_exact_names": ["bars_available"],
        "structural_proxy_name_tokens": ["coverage", "data_available", "history_length", "bars_available"],
        "applies_regardless_of_effect": True,
    },
    "anchor_selection_policy": {
        "frozen_before_validation": True,
        "principle": "Anchor choice is role-defined and deterministic, never chosen by observed effect magnitude, p/q value, ladder strength, or positive recall. For each feature, use the role's fixed priority from earliest information to latest and select the first anchor that passes the already-defined eligibility gates.",
        "early_core_priority_minutes": [240, 120, 60],
        "confirmation_priority_minutes": [30, 15, 5],
        "severity_quality_priority_minutes": [240, 120, 60, 30, 15, 5],
        "tie_break": "The fixed priority list is the complete tie-break; no result-dependent secondary tie-break is permitted.",
        "development_status": "Frozen now while only 2019-2024 Discovery/Audit results are available. This is Development model selection, not a claim that the anchor policy was frozen before Discovery/Audit results existed.",
        "validation_lock": "Once 2025 is opened, anchor priorities and selected Feature x Anchor pairs cannot be changed from 2025 results.",
    },
    "roles": {
        "early_core": "Source-promoted, six-year sign-stable, all-supported-phase sign-stable, eligible features at anchors 60/120/240m. Apply fixed priority 240->120->60; keep first qualifying anchor per feature.",
        "confirmation": "Same stability/eligibility rule at anchors 5/15/30m. Apply fixed priority 30->15->5; keep first qualifying anchor per feature. Confirmation cannot redefine Early Core after Validation opens.",
        "severity_quality": "Source-promoted + six-year stable + supported-phase stable + monotonic +20/+30/+50, eligible features only. Apply fixed priority 240->120->60->30->15->5. This is quality/severity evidence, not a mandatory +20 detector gate.",
    },
    "score": {
        "normalization": "For each selected Feature x Anchor, center at the Discovery hard-negative equal-symbol mean and scale by the pooled symbol-mean SD reconstructed as abs((positive_mean-hard_negative_mean)/standardized_effect).",
        "component": "clip(direction * (x-hard_negative_mean)/pooled_sd, -3, +3)",
        "weight": "abs(Discovery standardized effect), normalized to sum to 1 within role",
        "event_score": "weighted mean of available selected components; renormalize over available weights; require >=50% of role weight observed",
        "development_threshold": "95th percentile of per-symbol mean hard-negative event scores in 2019-2024; frozen before 2025. This targets approximately 5% Development hard-negative symbol FPR without optimizing on positive recall.",
    },
    "integrity": {
        "expected_sessions": 1510,
        "expected_observations": 197125,
        "expected_class_counts": {"positive":92538,"hard_negative":71332,"random_control":33255},
        "expected_source_promoted": 76,
        "expected_year_stable": 73,
        "expected_phase_stable": 60,
        "expected_ladder_monotonic": 16,
    },
    "safety": {"alpaca_requests":False,"validation_2025_read":False,"holdout_2026_read":False,"orders_enabled":False,"alerts_enabled":False,"stop_and_review_after_freeze":True},
}
FEATURE_SCORING_FREEZE_SHA256 = hashlib.sha256(json.dumps(FEATURE_SCORING_FREEZE_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# Frozen only after the 2019-2024 model freeze and before any 2025 read.
# These rules define how 2025 will be interpreted; they do not open or scan 2025.
VALIDATION_SUCCESS_CRITERIA_SPEC = {
    "criteria_id": "IPR-VALIDATION-SUCCESS-CRITERIA-2026-09-08-A",
    "development_status": "Post-Discovery/post-model-freeze, pre-Validation protocol amendment. Frozen before any 2025 read and never claimed as pre-Discovery preregistration.",
    "purpose": "Pre-commit PASS/WEAK_PASS/FAIL interpretation for the untouched 2025 Validation year before opening it.",
    "required_frozen_model_sha256": "c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3",
    "required_freeze_id": "IPR-FEATURE-SCORING-FREEZE-2026-09-08-B",
    "scope": "Freeze criteria from the already-frozen 2019-2024 model report only. No 2025/2026 observations may be read.",
    "metric_unit": {
        "positive": "event-level pass rate among scoreable verified >=20% positive events",
        "hard_negative": "symbol-level pass rate using mean event score per symbol, matching Development threshold calibration",
        "scoreability": "report scoreable numerator/denominator separately; do not silently drop missing-score cases from coverage diagnostics",
    },
    "role_rules": {
        "early_core": {"critical": True, "pass_min_development_recall_retention": 0.60, "weak_min_development_recall_retention": 0.40, "max_hard_negative_symbol_pass_rate": 0.10},
        "confirmation": {"critical": True, "pass_min_development_recall_retention": 0.60, "weak_min_development_recall_retention": 0.40, "max_hard_negative_symbol_pass_rate": 0.10},
        "severity_quality": {"critical": False, "pass_min_development_recall_retention": 0.60, "weak_min_development_recall_retention": 0.40, "max_hard_negative_symbol_pass_rate": 0.10, "note": "Diagnostic/quality role; cannot rescue or veto Core Validation by itself."},
    },
    "classification": {
        "role_pass": "positive pass rate >= 60% of its frozen Development positive pass rate AND hard-negative symbol pass rate <=10%.",
        "role_weak_pass": "positive pass rate >=40% but <60% of frozen Development positive pass rate AND hard-negative symbol pass rate <=10%.",
        "role_fail": "positive pass rate <40% of frozen Development positive pass rate OR hard-negative symbol pass rate >10%.",
        "overall_pass": "Both critical roles (early_core and confirmation) are PASS.",
        "overall_weak_pass": "Neither critical role is FAIL and at least one critical role is WEAK_PASS.",
        "overall_fail": "Either critical role is FAIL.",
    },
    "guardrails": {
        "no_threshold_change": True, "no_feature_change": True, "no_anchor_change": True, "no_direction_change": True, "no_weight_change": True,
        "no_2025_driven_reinterpretation": True, "no_2026_read": True,
        "after_2025": "STOP_REVIEW regardless of PASS/WEAK_PASS/FAIL. 2026 remains locked and requires a separate explicit decision."
    },
    "safety": {"alpaca_requests": False, "validation_2025_read": False, "holdout_2026_read": False, "orders_enabled": False, "alerts_enabled": False},
}
VALIDATION_SUCCESS_CRITERIA_SHA256 = hashlib.sha256(json.dumps(VALIDATION_SUCCESS_CRITERIA_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

VALIDATION_2025_SPEC={"validation_id":"IPR-FROZEN-MODEL-VALIDATION-2025-2026-09-08-A","scope":"2025 only; 2026 forbidden","required_frozen_model_sha256":"c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3","required_criteria_sha256":"8369764d07694d2dc69c8de5c06f331bef0ff2e998da0b11936ade30c19ed67a","required_criteria_artifact_sha256":"b64031aa249d24660ca3fdc2a510bf145b6285be00324bf906b88a0d422ea664","locked_year":2025,"forbidden_year":2026,"model_mutation_allowed":False,"threshold_recalibration_allowed":False,"criteria_reinterpretation_allowed":False,"stop_and_review_after_2025":True}
VALIDATION_2025_SHA256=hashlib.sha256(json.dumps(VALIDATION_2025_SPEC,sort_keys=True,separators=(",", ":")).encode("utf-8")).hexdigest()


# Frozen after completed 2025 Validation and before any 2026 read.
# This is explicitly a post-Validation / pre-Holdout protocol amendment.
HOLDOUT_SUCCESS_CRITERIA_SPEC = {
    "criteria_id": "IPR-HOLDOUT-PROTOCOL-LOCK-2026-09-08-B",
    "development_status": "Post-2025-Validation, pre-2026-Holdout protocol lock. The official 2026 PASS/WEAK_PASS/FAIL rules are inherited unchanged from the criteria frozen before 2025; no new outcome cutoff is introduced from 2025 results.",
    "purpose": "Lock the untouched 2026 Holdout to the original pre-2025 performance criteria, while reporting scoreability as diagnostic-only coverage information.",
    "required_frozen_model_sha256": "c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3",
    "required_validation_result_sha256": "96f7645e8db3145f9305bfe995df0ce54cc999ea4ef4a550d1a3cc63c4f0232f",
    "required_validation_overall_classification": "PASS",
    "required_pre2025_criteria_sha256": "8369764d07694d2dc69c8de5c06f331bef0ff2e998da0b11936ade30c19ed67a",
    "scope": "Protocol lock only. No 2026 observations may be read. 2025 is provenance/diagnostic context only and cannot recalibrate the model or official decision thresholds.",
    "critical_roles": ["early_core", "confirmation"],
    "performance_rules": {
        "pass_min_development_recall_retention": 0.60,
        "weak_min_development_recall_retention": 0.40,
        "max_hard_negative_symbol_pass_rate": 0.10,
        "provenance": "Inherited unchanged from IPR-VALIDATION-SUCCESS-CRITERIA-2026-09-08-A, frozen before 2025 was opened.",
        "note": "No relaxation, tightening, or 2025-based recalibration is permitted for the official 2026 classification."
    },
    "scoreability_policy": {
        "classification_role": "diagnostic_only",
        "can_change_official_pass_weak_fail": False,
        "required_reporting": ["positive_events_raw_selected", "positive_events_scoreable", "positive_scoreability_rate"],
        "comparisons": ["2025", "Development where available"],
        "no_numeric_cutoff": True,
        "reason": "The pre-2025 protocol required explicit scoreability reporting but did not precommit a scoreability failure threshold; inventing one after seeing 2025 would add post-validation flexibility."
    },
    "classification": {
        "role_pass": "positive pass rate >=60% of frozen Development positive pass rate AND hard-negative symbol pass rate <=10%.",
        "role_weak_pass": "positive pass rate >=40% but <60% of frozen Development positive pass rate AND hard-negative symbol pass rate <=10%.",
        "role_fail": "positive pass rate <40% of frozen Development positive pass rate OR hard-negative symbol pass rate >10%.",
        "overall_pass": "Both critical roles (early_core and confirmation) are PASS.",
        "overall_weak_pass": "Neither critical role is FAIL and at least one critical role is WEAK_PASS.",
        "overall_fail": "Either critical role is FAIL.",
        "severity_quality": "Diagnostic/quality role; cannot rescue or veto the official Core Holdout classification by itself."
    },
    "guardrails": {
        "no_model_change": True, "no_threshold_change": True, "no_feature_change": True, "no_anchor_change": True, "no_direction_change": True, "no_weight_change": True,
        "no_2025_recalibration": True, "no_2026_driven_reinterpretation": True, "no_2026_read_during_lock": True,
        "after_2026": "STOP_REVIEW regardless of PASS/WEAK_PASS/FAIL; no live-bot or profitability claim follows automatically."
    },
    "safety": {"alpaca_requests": False, "holdout_2026_read": False, "orders_enabled": False, "alerts_enabled": False}
}
HOLDOUT_SUCCESS_CRITERIA_SHA256 = hashlib.sha256(json.dumps(HOLDOUT_SUCCESS_CRITERIA_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


FINAL_HOLDOUT_2026_SPEC = {
    "holdout_id": "IPR-FINAL-FROZEN-HOLDOUT-2026-2026-09-08-A",
    "purpose": "One-time final untouched 2026 holdout evaluation using the frozen 2019-2024 model and the official criteria inherited unchanged from the pre-2025 criteria freeze.",
    "scope": "2026-01-01 through 2026-08-31 persisted Phase0B sessions only; no post-2026-08-31 session is eligible.",
    "locked_year": 2026,
    "last_allowed_session": "2026-08-31",
    "required_frozen_model_sha256": "c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3",
    "required_pre2025_criteria_sha256": "8369764d07694d2dc69c8de5c06f331bef0ff2e998da0b11936ade30c19ed67a",
    "required_validation_result_sha256": "96f7645e8db3145f9305bfe995df0ce54cc999ea4ef4a550d1a3cc63c4f0232f",
    "required_holdout_criteria_id": "IPR-HOLDOUT-PROTOCOL-LOCK-2026-09-08-B",
    "required_holdout_criteria_sha256": "ec9a1292d4d3c13ab4688cd7238239f2566ae45fcdea30205d05ec9942355160",
    "required_holdout_criteria_artifact_sha256": "7289c944c839d77b79a934b64157590f55c397cf3733ce11b4eed5c99a0fde3a",
    "official_classification": {
        "pass_min_development_recall_retention": 0.60,
        "weak_min_development_recall_retention": 0.40,
        "max_hard_negative_symbol_pass_rate": 0.10,
        "critical_roles": ["early_core", "confirmation"],
        "severity_quality": "diagnostic_only",
    },
    "scoreability": {
        "classification_role": "diagnostic_only",
        "no_numeric_cutoff": True,
        "report_positive_events_raw_selected": True,
        "report_positive_events_scoreable": True,
        "report_positive_scoreability_rate": True,
        "compare_to_2025": True,
    },
    "guardrails": {
        "one_time_holdout": True,
        "rerun_after_completion_prohibited": True,
        "no_model_mutation": True,
        "no_threshold_recalibration": True,
        "no_feature_change": True,
        "no_anchor_change": True,
        "no_direction_change": True,
        "no_weight_change": True,
        "no_criteria_reinterpretation": True,
        "stop_and_review_after_holdout": True,
        "no_live_or_profitability_claim": True,
    },
}
FINAL_HOLDOUT_2026_SHA256 = hashlib.sha256(json.dumps(FINAL_HOLDOUT_2026_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


CAUSAL_TRADING_TRANSLATION_SPEC = {
    "translation_id": "IPR-CAUSAL-TRADING-TRANSLATION-2026-09-08-A",
    "purpose": "Freeze the diagnostic causal trading-translation experiment before inspecting any post-signal MFE/MAE or entry/exit profitability results.",
    "required_final_holdout_result_sha256": "cdea1fcb18090f0384e0d68ea4558f86577646535f27e35264160bfa6dcbbaea",
    "required_frozen_model_sha256": "c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3",
    "source_model_status": "Final 2026 Holdout PASS; model remains immutable.",
    "scope": "Diagnostic replay only. No model/feature/anchor/weight/threshold tuning. No live alerts, orders, stop optimization, target optimization, or profitability claim.",
    "signal_definition": {
        "primary_role": "early_core",
        "rule": "First causal completed 5-minute evaluation timestamp at which the frozen Early Core score is scoreable (>=50% frozen role weight observed) and reaches/exceeds its frozen threshold 0.5205528990060366.",
        "clock": "America/New_York trading-cycle timeline using only information completed at or before each evaluation timestamp.",
        "evaluation_grid": "Completed 5-minute boundaries only; never use future/incomplete bars.",
        "confirmation": "Record first later causal completed 5-minute timestamp at which frozen Confirmation score reaches/exceeds 0.872042442164783; diagnostic only and must not redefine the primary Early Core signal.",
        "event_limit": "At most one first Early Core signal per verified explosion event.",
    },
    "entry_reference": {
        "diagnostic_price": "Close of the completed 5-minute bar that first satisfies Early Core; no assumption of fill at an earlier intrabar price.",
        "not_a_trade_fill_claim": True,
    },
    "pre_signal_diagnostics": [
        "event baseline/running-min reference price used by Phase0B where available",
        "percent move already realized by first Early Core signal",
        "minutes from first Early Core signal to Phase0B +20% confirmation timestamp",
        "trading phase at first signal",
    ],
    "forward_outcomes": {
        "horizons_minutes": [5, 15, 30, 60, 120],
        "also_to_end_of_trading_cycle": True,
        "metrics": ["MFE_pct_from_signal_close", "MAE_pct_from_signal_close", "close_return_pct", "time_to_MFE_minutes"],
        "price_source": "historical one-minute bars after the completed signal bar only",
        "no_stop_or_target_optimization": True,
    },
    "reporting": {
        "stratify_by_year": [2019,2020,2021,2022,2023,2024,2025,2026],
        "stratify_by_signal_phase": True,
        "report_scoreability_and_signal_coverage": True,
        "report_medians_and_distribution_quantiles": [0.10,0.25,0.50,0.75,0.90],
        "report_fraction_signal_before_plus20_confirmation": True,
        "report_fraction_with_MFE_ge": [0.05,0.10,0.20],
        "hard_negative_same_signal_rule": True,
        "hard_negative_forward_outcomes_same_horizons": True,
    },
    "interpretation_guardrails": {
        "diagnostic_stage_only": True,
        "no_entry_stop_exit_rules_selected_from_this_freeze": True,
        "no_expectancy_or_profit_factor_claim_in_this_stage": True,
        "no_model_mutation": True,
        "no_threshold_recalibration": True,
        "no_feature_change": True,
        "no_anchor_change": True,
        "no_direction_change": True,
        "no_weight_change": True,
        "2026_is_no_longer_an_untouched_model_holdout": True,
        "translation_protocol_frozen_before_post_signal_path_inspection": True,
        "stop_and_review_after_diagnostic_replay": True,
    },
    "safety": {"alpaca_requests_during_freeze": False, "orders_enabled": False, "alerts_enabled": False},
}
CAUSAL_TRADING_TRANSLATION_SHA256 = hashlib.sha256(json.dumps(CAUSAL_TRADING_TRANSLATION_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


EARLY_CAUSAL_ENTRY_RESEARCH_SPEC = {
    "research_id": "IPR-EARLY-CAUSAL-ENTRY-RESEARCH-2026-09-13-A",
    "purpose": "Freeze the next Development/Research protocol before any new early-entry feature search, candidate-rule selection, or fresh post-2026-08-31 OOS inspection.",
    "required_causal_replay_result_sha256": "980026ad16d290d61d2a1f244949e19b50091b0533ba116825ce25d4e5e97faa",
    "required_causal_replay_spec_sha256": "30362daf7d213ef50293fe6d23686202e90a389b8daf822a4149d1efc1dc2ada",
    "required_frozen_model_sha256": "c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3",
    "baseline": {
        "name": "Frozen Early Core",
        "threshold": 0.5205528990060366,
        "positive_signal_coverage": 0.9435706369231495,
        "hard_negative_signal_coverage_event_level_diagnostic": 0.7563769901800594,
        "fraction_positive_signal_before_plus20_confirmation": 0.3885077722797021,
        "positive_percent_move_already_realized_mean": 15.390776086711702,
        "positive_percent_move_already_realized_median": 12.676056338028175,
        "note": "Replay signal is the first causal completed-5m Frozen Early Core threshold crossing, not the Census/Phase0B candidate timestamp."
    },
    "development_data_policy": {
        "development_research_window": "2019-01-02 through 2026-08-31; already research-open and may be used for Development diagnostics only.",
        "fresh_oos_boundary": "strictly after 2026-08-31",
        "fresh_oos_must_remain_unread_during_development": True,
        "no_2019_2026_result_may_be_called_fresh_oos": True,
        "entry_stop_exit_protocol_requires_fresh_independent_oos_after_freeze": True
    },
    "causal_search": {
        "lookback_checkpoints_minutes_before_frozen_early_core": [240,120,60,30,15],
        "features_at_checkpoint": "Only information completed and observable at that historical timestamp; no future/incomplete bars and no Phase0B confirmation fields as predictors.",
        "labels_only_for_evaluation": "Verified positive vs contextual hard-negative labels may be used as outcomes, never as live-available inputs.",
        "candidate_live_rule_requirement": "Any proposed rule must be expressible on a universal causal completed-5m timeline without knowing future Early Core or +20 confirmation time.",
        "selection_order": [240,120,60,30,15],
        "selection_rule": "Choose the earliest checkpoint (largest minutes-before-Early-Core) that independently meets every frozen success gate; never choose by largest observed effect, best p-value, or best retrospective profit.",
        "no_entry_stop_exit_optimization": True
    },
    "frozen_early_success_criterion": {
        "unit_for_false_positive_control": "symbol-level contextual hard-negative pass rate using equal-symbol aggregation; event-level FPR is diagnostic only",
        "max_hard_negative_symbol_pass_rate": 0.10,
        "minimum_positive_recall": 0.20,
        "minimum_relative_recall_vs_frozen_early_core_before_plus20_fraction": 0.50,
        "derived_minimum_positive_recall_from_baseline": 0.19425388613985105,
        "effective_minimum_positive_recall": 0.20,
        "minimum_years_same_direction": 6,
        "recent_year_requirement": "2025 and 2026-through-2026-08-31 must each preserve the same direction; these remain Development diagnostics, not fresh OOS.",
        "timing_requirement": "Candidate must be evaluable at the stated checkpoint and therefore precede the Frozen Early Core crossing by construction.",
        "classification": {
            "PASS": "HN symbol pass <=10%, positive recall >=20%, same-direction stability in >=6 calendar years including 2025 and 2026-to-Aug31, and universal causal live-expressibility passes.",
            "FAIL": "Any mandatory PASS condition fails.",
            "NO_RESULT": "Insufficient scoreable support or causal live-expressibility cannot be established."
        },
        "rationale": "Pre-frozen before new early-search results. The 20% recall floor is slightly stricter than 50% of the observed 38.8508% baseline-before-+20 fraction and prevents declaring a tiny early subset a success."
    },
    "support_and_inference": {
        "minimum_positive_events": 500,
        "minimum_positive_symbols": 100,
        "minimum_calendar_years": 6,
        "equal_symbol_weighting": True,
        "symbol_clustered_inference": True,
        "multiple_testing_control": "BH FDR q<=0.05 within the predeclared early-search family when feature-level hypothesis testing is used",
        "effect_size_must_be_reported": True
    },
    "reporting_required": [
        "scoreability by checkpoint/class/year",
        "positive recall and hard-negative symbol pass rate by checkpoint",
        "year stability including 2025 and 2026-through-Aug31",
        "lead minutes versus Frozen Early Core",
        "fraction candidate signal before Phase0B +20 confirmation",
        "percent move already realized at candidate signal",
        "comparison with unchanged Frozen Early Core baseline",
        "all attempted checkpoints including failures"
    ],
    "guardrails": {
        "frozen_early_core_remains_immutable_baseline": True,
        "no_model_mutation_in_protocol_freeze": True,
        "no_threshold_recalibration_in_protocol_freeze": True,
        "no_new_post_2026_08_31_data_read_in_protocol_freeze": True,
        "no_profitability_claim": True,
        "no_entry_stop_exit_claim": True,
        "no_live_alert_claim": True,
        "stop_and_review_after_research": True
    },
    "safety": {"alpaca_requests_during_freeze": False, "orders_enabled": False, "alerts_enabled": False}
}
EARLY_CAUSAL_ENTRY_RESEARCH_SHA256 = hashlib.sha256(json.dumps(EARLY_CAUSAL_ENTRY_RESEARCH_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


EARLY_CAUSAL_ENTRY_EXEC_SPEC = {
    "execution_id": "IPR-EARLY-CAUSAL-ENTRY-EXECUTION-2026-09-13-A",
    "required_research_spec_sha256": "d68e30de9497742c4d887bcbf4c2646b022f35746e337568983d94ed57f45592",
    "required_protocol_artifact_sha256": "2145af689499490a958d527a53755c07e1a3512529398c0c593813eda5d05c30",
    "required_causal_replay_result_sha256": "980026ad16d290d61d2a1f244949e19b50091b0533ba116825ce25d4e5e97faa",
    "required_frozen_model_sha256": "c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3",
    "development_end": "2026-08-31",
    "checkpoints_minutes_before_frozen_early_core": [240,120,60,30,15],
    "candidate_family": {
        "name": "Frozen Early Core precursor-score family",
        "description": "Evaluate the unchanged frozen Early Core score on completed 5-minute information at each pre-Early-Core checkpoint; Development may select a lower predeclared score threshold, but feature definitions/directions/weights remain immutable.",
        "threshold_multipliers_of_frozen_early_core": [0.25,0.50,0.75,1.00],
        "threshold_selection_within_checkpoint": "Among predeclared thresholds satisfying every mandatory gate, select the highest threshold (most conservative); no profit/MFE/MAE is used for selection.",
        "checkpoint_selection": "Evaluate checkpoints strictly 240,120,60,30,15 and stop selection at the earliest checkpoint with a qualifying threshold.",
        "fresh_oos_required_after_candidate_freeze": True
    },
    "cohort": "Persisted Causal Diagnostic Replay positive and contextual hard-negative signals only; each checkpoint is defined relative to that event's frozen Early Core signal. This is Development research, not fresh OOS.",
    "gates": {
        "max_hard_negative_equal_symbol_pass_rate": 0.10,
        "min_positive_event_recall": 0.20,
        "min_positive_events": 500,
        "min_positive_symbols": 100,
        "min_same_direction_years": 6,
        "must_include_years_same_direction": [2025,2026],
        "same_direction_definition": "positive event pass rate > hard-negative event pass rate in the year",
        "live_expressible": True
    },
    "guardrails": {
        "no_post_2026_08_31_data": True,
        "no_entry_stop_exit_optimization": True,
        "no_profitability_selection": True,
        "no_model_feature_direction_weight_mutation": True,
        "resume_by_completed_session": True,
        "persist_session_observations": True,
        "stop_and_review_after_execution": True
    }
}
EARLY_CAUSAL_ENTRY_EXEC_SHA256 = hashlib.sha256(json.dumps(EARLY_CAUSAL_ENTRY_EXEC_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SPEC = {
    "amendment_id": "IPR-EARLY-CAUSAL-ENTRY-THRESHOLD-FAMILY-AMENDMENT-2026-09-13-A",
    "purpose": "Pre-execution documentation freeze for the threshold family that was coded before any Early Causal Entry execution result was observed.",
    "required_research_spec_sha256": "d68e30de9497742c4d887bcbf4c2646b022f35746e337568983d94ed57f45592",
    "required_protocol_artifact_sha256": "2145af689499490a958d527a53755c07e1a3512529398c0c593813eda5d05c30",
    "required_execution_spec_sha256": EARLY_CAUSAL_ENTRY_EXEC_SHA256,
    "frozen_threshold_family": {
        "threshold_multipliers_of_frozen_early_core": [0.25,0.50,0.75,1.00],
        "within_checkpoint_selection": "Among predeclared thresholds satisfying every mandatory gate, select the highest threshold (most conservative).",
        "checkpoint_selection": "Evaluate checkpoints strictly 240,120,60,30,15 and select the earliest checkpoint with a qualifying threshold.",
        "selection_must_not_use": ["profit", "MFE", "MAE", "best_p_value", "largest_observed_effect"]
    },
    "timing_attestation": {
        "frozen_before_execution_start": True,
        "frozen_before_any_execution_result_observed": True,
        "research_execution_must_remain_not_started_until_amendment_frozen": True
    },
    "guardrails": {
        "no_new_post_2026_08_31_data_read": True,
        "no_alpaca_requests_during_amendment_freeze": True,
        "no_model_mutation": True,
        "no_threshold_result_driven_recalibration": True
    }
}
EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SHA256 = hashlib.sha256(json.dumps(EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


CAUSAL_DIAGNOSTIC_REPLAY_SPEC = {
    "replay_id": "IPR-CAUSAL-DIAGNOSTIC-REPLAY-2026-09-08-A",
    "required_translation_spec_sha256": "d6db4145b30c8b712856f9ab4ad78028e61ff937cdd4c3f114f44c1c72c5a041",
    "required_translation_protocol_artifact_sha256": "510fb4f951fde48946d14b910133617210ae9ea21876b523abd3c3a6ff53fbbf",
    "required_frozen_model_sha256": "c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3",
    "scope": "2019-2026 persisted Phase0B sessions through 2026-08-31; diagnostic causal replay under the already-frozen v1.7.22 translation protocol.",
    "horizons_minutes": [5,15,30,60,120],
    "forward_metrics": ["MFE_pct_from_signal_close","MAE_pct_from_signal_close","close_return_pct","time_to_MFE_minutes"],
    "guardrails": {"no_model_mutation": True, "no_threshold_recalibration": True, "no_feature_change": True, "no_anchor_change": True, "no_direction_change": True, "no_weight_change": True, "no_stop_or_target_optimization": True, "no_expectancy_or_profit_factor_claim": True, "stop_and_review_after_replay": True},
}
CAUSAL_DIAGNOSTIC_REPLAY_SHA256 = hashlib.sha256(json.dumps(CAUSAL_DIAGNOSTIC_REPLAY_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()



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
                if exc.code in (429, 500, 502, 503, 504) and attempt < 5:
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

    def assets_by_status(self, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"asset_class": "us_equity"}
        if status:
            params["status"] = status
        return list(self.get(f"{self.trading_base}/v2/assets", params) or [])

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
            for symbol, rows in (page.get("bars") or {}).items():
                output.setdefault(symbol, []).extend(rows or [])
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
        self.phase0_probe_lock = threading.RLock()
        self.phase0_probe_thread: threading.Thread | None = None
        self.phase0_probe_state: dict[str, Any] = {
            "status": "IDLE", "message": "Phase 0 capability probe has not started",
            "probe_id": PHASE0_PROBE_SPEC["probe_id"], "phase0a_allowed": False,
            "updated_at": iso(),
        }
        self.phase0_probe_report: dict[str, Any] | None = None
        self.phase0_reference_lock = threading.RLock()
        self.phase0_reference_thread: threading.Thread | None = None
        self.phase0_reference_state: dict[str, Any] = {
            "status": "IDLE", "message": "Reference-candidate discovery has not started",
            "selection_uses_session_detector": False, "phase0a_allowed": False, "updated_at": iso(),
        }
        self.phase0_reference_report: dict[str, Any] | None = None
        self.universe_probe_lock = threading.RLock()
        self.universe_probe_thread: threading.Thread | None = None
        self.universe_probe_state: dict[str, Any] = {"status":"IDLE","message":"Historical-universe capability probe has not started","historical_census_allowed":False,"updated_at":iso()}
        self.universe_probe_report: dict[str, Any] | None = None
        self.universe_reconstruction_lock = threading.RLock()
        self.universe_reconstruction_thread: threading.Thread | None = None
        self.universe_reconstruction_stop_event = threading.Event()
        self.universe_reconstruction_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Historical-universe reconstruction has not started","historical_census_allowed":False,"updated_at":iso()}
        self.historical_census_lock = threading.RLock()
        self.historical_census_thread: threading.Thread | None = None
        self.historical_census_stop_event = threading.Event()
        self.historical_census_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"2019-2026 historical census has not started","phase0b_allowed":False,"updated_at":iso()}
        self.historical_census_audit_lock = threading.RLock()
        self.historical_census_audit_thread: threading.Thread | None = None
        self.historical_census_audit_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Historical Census audit has not started","phase0b_allowed":False,"updated_at":iso()}
        self.instrument_cleanup_lock = threading.RLock()
        self.instrument_cleanup_thread: threading.Thread | None = None
        self.instrument_cleanup_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Instrument-type cleanup audit has not started","phase0b_allowed":False,"updated_at":iso()}
        self.pre0b_audit_lock = threading.RLock()
        self.pre0b_audit_thread: threading.Thread | None = None
        self.pre0b_audit_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Pre-Phase0B resolution/normalization audit has not started","phase0b_allowed":False,"updated_at":iso()}
        self.phase0b_window_probe_lock = threading.RLock()
        self.phase0b_window_probe_thread: threading.Thread | None = None
        self.phase0b_window_probe_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Phase 0B windowing validation probe has not started","phase0b_allowed":False,"updated_at":iso()}
        self.phase0b_full_lock = threading.RLock()
        self.phase0b_full_thread: threading.Thread | None = None
        self.phase0b_full_stop_event = threading.Event()
        self.phase0b_full_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Phase 0B full-cycle verification has not started","verification_id":PHASE0B_FULL_SPEC["verification_id"],"feature_discovery_allowed":False,"updated_at":iso()}
        self.phase0b_dataset_audit_lock = threading.RLock()
        self.phase0b_dataset_audit_thread: threading.Thread | None = None
        self.phase0b_dataset_audit_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Phase 0B dataset audit has not started","audit_id":PHASE0B_DATASET_AUDIT_SPEC["audit_id"],"feature_discovery_allowed":False,"updated_at":iso()}
        self.feature_discovery_lock = threading.RLock()
        self.feature_discovery_thread: threading.Thread | None = None
        self.feature_discovery_stop_event = threading.Event()
        self.feature_discovery_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Feature Discovery has not started","run_id":FEATURE_DISCOVERY_EXEC_SPEC["run_id"],"validation_2025_opened":False,"holdout_2026_opened":False,"updated_at":iso()}
        self.feature_discovery_audit_lock = threading.RLock()
        self.feature_discovery_audit_thread: threading.Thread | None = None
        self.feature_discovery_audit_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Discovery Audit has not started","audit_id":FEATURE_DISCOVERY_AUDIT_SPEC["audit_id"],"validation_2025_opened":False,"holdout_2026_opened":False,"updated_at":iso()}
        self.feature_scoring_freeze_lock = threading.RLock()
        self.feature_scoring_freeze_thread: threading.Thread | None = None
        self.feature_scoring_freeze_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Feature/Scoring Freeze has not started","freeze_id":FEATURE_SCORING_FREEZE_SPEC["freeze_id"],"validation_2025_opened":False,"holdout_2026_opened":False,"updated_at":iso()}
        self.validation_criteria_state: dict[str, Any] = {"status":"IDLE","phase":"NOT_STARTED","message":"Validation Success Criteria Freeze has not started","criteria_id":VALIDATION_SUCCESS_CRITERIA_SPEC["criteria_id"],"validation_2025_opened":False,"holdout_2026_opened":False,"updated_at":iso()}
        self.validation_2025_lock=threading.RLock(); self.validation_2025_thread=None; self.validation_2025_stop_event=threading.Event()
        self.validation_2025_state={"status":"IDLE","phase":"NOT_STARTED","message":"2025 Validation not started","validation_id":VALIDATION_2025_SPEC["validation_id"],"validation_2025_opened":False,"holdout_2026_opened":False,"updated_at":iso()}
        self.holdout_criteria_state={"status":"IDLE","phase":"NOT_STARTED","message":"2026 Holdout Success Criteria Freeze has not started","criteria_id":HOLDOUT_SUCCESS_CRITERIA_SPEC["criteria_id"],"holdout_2026_opened":False,"updated_at":iso()}
        self.final_holdout_2026_lock=threading.RLock(); self.final_holdout_2026_thread=None; self.final_holdout_2026_stop_event=threading.Event()
        self.final_holdout_2026_state={"status":"IDLE","phase":"NOT_STARTED","message":"Final 2026 Holdout not started","holdout_id":FINAL_HOLDOUT_2026_SPEC["holdout_id"],"holdout_2026_opened":False,"holdout_2026_read":False,"updated_at":iso()}
        self.causal_diagnostic_replay_lock=threading.RLock(); self.causal_diagnostic_replay_thread=None; self.causal_diagnostic_replay_stop_event=threading.Event()
        self.causal_diagnostic_replay_state={"status":"IDLE","phase":"NOT_STARTED","message":"Causal Diagnostic Replay not started","replay_id":CAUSAL_DIAGNOSTIC_REPLAY_SPEC["replay_id"],"post_signal_paths_read":False,"updated_at":iso()}
        self.early_causal_entry_exec_lock=threading.RLock(); self.early_causal_entry_exec_thread=None; self.early_causal_entry_exec_stop_event=threading.Event()
        self.early_causal_entry_exec_state={"status":"IDLE","phase":"NOT_STARTED","message":"Early Causal Entry Research execution not started","execution_id":EARLY_CAUSAL_ENTRY_EXEC_SPEC["execution_id"],"new_post_2026_08_31_data_read":False,"updated_at":iso()}
        self.phase0a_lock = threading.RLock()
        self.phase0a_thread: threading.Thread | None = None
        self.phase0a_stop_event = threading.Event()
        self.phase0a_state: dict[str, Any] = {
            "status": "IDLE", "phase": "NOT_STARTED", "message": "Phase 0A census has not started",
            "census_id": PHASE0A_SPEC["census_id"], "phase0b_allowed": False, "updated_at": iso(),
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

    def phase0_probe_key(self, suffix: str) -> str:
        return self.key(f"phase0_probe:v1:{suffix}")

    def audit_key(self, suffix: str) -> str:
        return self.key(f"historical_confirmation:v1:{suffix}")

    def early_key(self, suffix: str) -> str:
        return self.key(f"early_causal_entry:v1:{suffix}")

    def orb_key(self, suffix: str) -> str:
        return self.key(f"liquid_daily_orb:v1:{suffix}")

    def breakout_key(self, suffix: str) -> str:
        return self.key(f"daily_breakout_volume:v1:{suffix}")

    def _set_phase0_probe_state(self, **updates: Any) -> None:
        with self.phase0_probe_lock:
            self.phase0_probe_state.update(updates)
            self.phase0_probe_state["updated_at"] = iso()
            snapshot = dict(self.phase0_probe_state)
        if self.redis.configured:
            try:
                self.redis.set_json(self.phase0_probe_key("status"), snapshot)
            except Exception:
                logging.exception("Unable to persist Phase 0 probe state")

    @staticmethod
    def _streaming_ge20(rows: list[dict[str, Any]], threshold_pct: float = 20.0) -> dict[str, Any]:
        threshold = 1.0 + threshold_pct / 100.0
        running_min = None
        running_min_ts = None
        for row in sorted(rows, key=lambda x: str(x.get("t") or "")):
            try:
                price = float(row.get("c"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            ts = str(row.get("t") or "")
            if running_min is None or price < running_min:
                running_min, running_min_ts = price, ts
            if running_min is not None and price >= running_min * threshold:
                return {
                    "detected": True, "threshold_pct": threshold_pct,
                    "t1": running_min_ts, "t1_price": running_min,
                    "t2": ts, "t2_price": price,
                    "gain_pct": (price / running_min - 1.0) * 100.0,
                }
        return {"detected": False, "threshold_pct": threshold_pct}

    @staticmethod
    def _probe_session(ts_text: str, target_session: date) -> str:
        dt = datetime.fromisoformat(ts_text.replace("Z", "+00:00")).astimezone(NY)
        minutes = dt.hour * 60 + dt.minute
        if dt.date() < target_session and 16 * 60 <= minutes < 20 * 60:
            return "AH"
        if (dt.date() < target_session and minutes >= 20 * 60) or (dt.date() == target_session and minutes < 4 * 60):
            return "Overnight"
        if dt.date() == target_session and 4 * 60 <= minutes < 9 * 60 + 30:
            return "Premarket"
        if dt.date() == target_session and 9 * 60 + 30 <= minutes <= 16 * 60:
            return "Regular"
        return "Other"

    def _probe_cycle_bounds(self, target_session: date) -> tuple[datetime, datetime]:
        cal = self.alpaca.calendar(target_session - timedelta(days=10), target_session)
        sessions = sorted(date.fromisoformat(str(x["date"])) for x in cal if x.get("date"))
        if target_session not in sessions:
            raise RuntimeError(f"Target session missing from Alpaca calendar: {target_session}")
        idx = sessions.index(target_session)
        if idx == 0:
            raise RuntimeError(f"Previous session unavailable for: {target_session}")
        prev = sessions[idx - 1]
        # Official close is normally 16:00 ET. Calendar close is used when supplied (early close).
        by_date = {date.fromisoformat(str(x["date"])): x for x in cal if x.get("date")}
        def close_dt(d: date) -> datetime:
            text = str(by_date[d].get("close") or "16:00")
            hh, mm = [int(v) for v in text.split(":")[:2]]
            return datetime.combine(d, dtime(hh, mm), NY).astimezone(UTC)
        return close_dt(prev), close_dt(target_session)

    def _run_phase0_reference_case(self, case: dict[str, Any]) -> dict[str, Any]:
        symbol = str(case["symbol"]).upper()
        target = date.fromisoformat(str(case["target_session"]))
        start, end = self._probe_cycle_bounds(target)
        sip = self.alpaca.bars([symbol], start, end, feed="sip", adjustment="raw", timeframe="1Min").get(symbol, [])
        boats = self.alpaca.bars([symbol], start, end, feed="boats", adjustment="raw", timeframe="1Min").get(symbol, [])
        merged: dict[str, dict[str, Any]] = {}
        source_by_ts: dict[str, str] = {}
        for source, source_rows in (("sip", sip), ("boats", boats)):
            for row in source_rows:
                ts = str(row.get("t") or "")
                if not ts: continue
                session = self._probe_session(ts, target)
                if source == "boats" and session != "Overnight": continue
                if ts not in merged or source == "sip":
                    merged[ts] = row; source_by_ts[ts] = source
        rows = [merged[k] for k in sorted(merged)]
        expected_session = str(case.get("expected_session") or "")
        session_rows = [r for r in rows if self._probe_session(str(r.get("t") or ""), target) == expected_session]
        session_counts: dict[str, int] = defaultdict(int); source_counts: dict[str, int] = defaultdict(int)
        for row in rows:
            ts = str(row.get("t") or "")
            session_counts[self._probe_session(ts, target)] += 1; source_counts[source_by_ts.get(ts, "unknown")] += 1
        threshold = float(PHASE0_PROBE_SPEC["primary_threshold_pct"])
        full_cycle_detector = self._streaming_ge20(rows, threshold)
        session_detector = self._streaming_ge20(session_rows, threshold)
        coverage_ok = bool(session_counts.get(expected_session, 0)) if expected_session else True
        full_cycle_ok = full_cycle_detector["detected"] == bool(case.get("expected_ge20_in_cycle", True))
        session_path_ok = session_detector["detected"] == bool(case.get("expected_ge20_in_session", True))
        full_cycle_t1_session = None
        if full_cycle_detector.get("detected") and full_cycle_detector.get("t1"):
            full_cycle_t1_session = self._probe_session(str(full_cycle_detector["t1"]), target)
        passed = coverage_ok and full_cycle_ok and session_path_ok
        return {**case, "cycle_start":iso(start), "cycle_end":iso(end), "sip_bars":len(sip), "boats_bars":len(boats), "merged_bars":len(rows),
                "expected_session_bars":len(session_rows), "session_counts":dict(session_counts), "source_counts":dict(source_counts),
                "coverage_ok":coverage_ok, "full_cycle_expectation_ok":full_cycle_ok, "session_path_expectation_ok":session_path_ok,
                "full_cycle_t1_session":full_cycle_t1_session, "full_cycle_t1_session_is_diagnostic_only":True,
                "full_cycle_detector":full_cycle_detector, "session_detector":session_detector, "passed":passed}

    @staticmethod
    def _synthetic_phase0_probe_tests() -> dict[str, Any]:
        def rows(prices: list[float]) -> list[dict[str, Any]]:
            base = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)
            return [{"t": iso(base + timedelta(minutes=i)), "c": p} for i, p in enumerate(prices)]
        cases = [
            ("detect_after_new_running_min", [10, 9, 9.5, 10.8], True),
            ("detect_first_causal_crossing", [10, 12, 9], True),
            ("below_threshold", [10, 9, 10.79], False),
        ]
        results = []
        for name, prices, expected in cases:
            actual = IndependentPriorityRadar._streaming_ge20(rows(prices))["detected"]
            results.append({"name": name, "expected": expected, "actual": actual, "passed": actual == expected})
        return {"passed": all(x["passed"] for x in results), "cases": results}

    @staticmethod
    def _phase0_gate_decision(synthetic: dict[str, Any], reference_results: list[dict[str, Any]]) -> dict[str, Any]:
        expected_sessions = {str(c.get("expected_session") or "") for c in PHASE0_PROBE_SPEC["frozen_reference_cases"]}
        covered_sessions = {str(r.get("expected_session") or "") for r in reference_results if r.get("coverage_ok")}
        refs_complete = len(reference_results) == len(PHASE0_PROBE_SPEC["frozen_reference_cases"])
        refs_pass = refs_complete and bool(reference_results) and all(bool(r.get("passed")) for r in reference_results)
        sessions_pass = bool(expected_sessions) and expected_sessions.issubset(covered_sessions)
        probe_passed = bool(synthetic.get("passed") and refs_pass and sessions_pass)
        return {
            "probe_passed": probe_passed,
            "phase0a_allowed": probe_passed,
            "fail_closed": True,
            "reference_cases_complete": refs_complete,
            "reference_cases_passed": refs_pass,
            "expected_extended_sessions": sorted(expected_sessions),
            "covered_extended_sessions": sorted(covered_sessions),
            "extended_session_coverage_passed": sessions_pass,
        }

    def phase0_probe_loop(self) -> None:
        try:
            self._set_phase0_probe_state(status="RUNNING", message="Testing Alpaca extended-hours capability", phase0a_allowed=False)
            synthetic = self._synthetic_phase0_probe_tests()
            reference_results = [self._run_phase0_reference_case(case) for case in PHASE0_PROBE_SPEC["frozen_reference_cases"]]
            gate = self._phase0_gate_decision(synthetic, reference_results)
            probe_passed = bool(gate["probe_passed"])
            report = {
                "version": VERSION, "build": BUILD, "probe_id": PHASE0_PROBE_SPEC["probe_id"],
                "probe_sha256": PHASE0_PROBE_SHA256, **gate,
                "synthetic_detector_validation": synthetic,
                "reference_cases": reference_results,
                "source_decision": "SIP+BOATS one-minute raw is eligible for Phase 0A" if probe_passed else "REJECTED: Phase 0A is blocked",
                "completed_at": iso(),
            }
            self.phase0_probe_report = report
            if self.redis.configured:
                self.redis.set_json(self.phase0_probe_key("report"), report)
            self._set_phase0_probe_state(
                status="PASSED" if probe_passed else "FAILED",
                message="Capability probe passed; Phase 0A may be designed" if probe_passed else "Capability probe failed; Phase 0A is blocked",
                probe_passed=probe_passed, phase0a_allowed=probe_passed,
            )
        except Exception as exc:
            logging.exception("Phase 0 capability probe failed")
            self._set_phase0_probe_state(status="ERROR", message="Capability probe errored; Phase 0A is blocked", probe_passed=False, phase0a_allowed=False, last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.phase0_probe_lock:
                self.phase0_probe_thread = None

    def start_phase0_probe(self) -> tuple[bool, str]:
        if not self.alpaca.configured:
            return False, "Alpaca credentials are required"
        with self.phase0_probe_lock:
            if self.phase0_probe_thread and self.phase0_probe_thread.is_alive():
                return False, "already_running"
            self.phase0_probe_state = {
                "status": "STARTING", "message": "Starting fail-closed Phase 0 capability probe",
                "probe_id": PHASE0_PROBE_SPEC["probe_id"], "probe_sha256": PHASE0_PROBE_SHA256,
                "probe_passed": False, "phase0a_allowed": False, "updated_at": iso(),
            }
            self.phase0_probe_thread = threading.Thread(target=self.phase0_probe_loop, name="independent-priority-phase0-probe", daemon=True)
            self.phase0_probe_thread.start()
        return True, "started"

    def universe_probe_key(self, suffix: str) -> str:
        return self.key(f"historical_universe_probe:v1:{suffix}")

    def _set_universe_probe_state(self, **updates: Any) -> None:
        with self.universe_probe_lock:
            self.universe_probe_state.update(updates)
            self.universe_probe_state["updated_at"] = iso()
            snapshot = dict(self.universe_probe_state)
        if self.redis.configured:
            self.redis.set_json(self.universe_probe_key("status"), snapshot)

    @staticmethod
    def _asset_symbol_set(rows: list[dict[str, Any]]) -> set[str]:
        return {str(x.get("symbol") or "").upper() for x in rows if SYMBOL_RE.fullmatch(str(x.get("symbol") or "").upper())}

    def historical_universe_probe_loop(self) -> None:
        try:
            self._set_universe_probe_state(status="RUNNING", message="Testing Alpaca all/inactive asset coverage and old SIP history", historical_census_allowed=False)
            all_assets = self.alpaca.assets_by_status(None)
            active_assets = self.alpaca.assets_by_status("active")
            inactive_assets = self.alpaca.assets_by_status("inactive")
            all_syms, active_syms, inactive_syms = map(self._asset_symbol_set, (all_assets, active_assets, inactive_assets))
            manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {}) if self.redis.configured else {}
            manifest_syms = {str(x).upper() for x in (manifest.get("symbols") or []) if SYMBOL_RE.fullmatch(str(x).upper())}
            catalog = self.redis.get_json(f"{self.source_prefix}:explosions:catalog", {}) if self.redis.configured else {}
            catalog_syms = {str(x.get("symbol") or "").upper() for x in (catalog.get("cases") or []) if SYMBOL_RE.fullmatch(str(x.get("symbol") or "").upper())}
            historical_cases = [
                {"symbol":"CELG","start":"2019-10-01","end":"2019-10-08","purpose":"2019 delisted/acquired ticker"},
                {"symbol":"TWTR","start":"2022-10-20","end":"2022-10-28","purpose":"2022 delisted/acquired ticker"},
                {"symbol":"SIVB","start":"2023-03-01","end":"2023-03-11","purpose":"2023 inactive bank ticker"},
                {"symbol":"ATVI","start":"2023-10-06","end":"2023-10-14","purpose":"2023 acquired ticker"},
                {"symbol":"BBBY","start":"2023-04-03","end":"2023-04-11","purpose":"2023 delisted ticker"},
            ]
            case_results=[]
            for case in historical_cases:
                start=datetime.fromisoformat(case["start"]+"T00:00:00+00:00")
                end=datetime.fromisoformat(case["end"]+"T23:59:59+00:00")
                rows=self.alpaca.bars([case["symbol"]], start, end, feed="sip", adjustment="raw", timeframe="1Day").get(case["symbol"],[])
                sym=case["symbol"]
                case_results.append({**case,"sip_historical_bars":len(rows),"sip_history_found":bool(rows),"in_all_assets":sym in all_syms,"in_active_assets":sym in active_syms,"in_inactive_assets":sym in inactive_syms})
            # Explicitly test historical symbol mapping around FB -> META.
            map_start=datetime(2022,6,6,tzinfo=UTC); map_end=datetime(2022,6,11,tzinfo=UTC)
            mapped=self.alpaca.bars(["META"],map_start,map_end,feed="sip",adjustment="raw",timeframe="1Day").get("META",[])
            old_history_hits=sum(bool(x["sip_history_found"]) for x in case_results)
            inactive_with_history=sum(bool(x["sip_history_found"] and x["in_inactive_assets"]) for x in case_results)
            manifest_in_all=len(manifest_syms & all_syms)
            catalog_in_all=len(catalog_syms & all_syms)
            diagnostics={
                "all_assets_count":len(all_syms),"active_assets_count":len(active_syms),"inactive_assets_count":len(inactive_syms),
                "all_equals_active_union_inactive": all_syms == (active_syms | inactive_syms),
                "manifest_symbol_count":len(manifest_syms),"manifest_in_all_assets":manifest_in_all,"manifest_missing_from_all_assets":len(manifest_syms-all_syms),
                "catalog_symbol_count":len(catalog_syms),"catalog_in_all_assets":catalog_in_all,"catalog_missing_from_all_assets":len(catalog_syms-all_syms),
                "old_history_cases_found":old_history_hits,"old_history_cases_total":len(case_results),"inactive_cases_with_sip_history":inactive_with_history,
                "meta_mapping_bars_2022_06_06_to_10":len(mapped),"meta_mapping_has_pre_rename_days":any(str(r.get("t") or "")[:10] < "2022-06-09" for r in mapped),
            }
            # This probe can establish usefulness, but Alpaca /assets is not documented as a point-in-time 2019-2026 security master.
            useful = len(inactive_syms)>0 and old_history_hits>=3 and diagnostics["meta_mapping_has_pre_rename_days"]
            if useful and len(manifest_syms-all_syms)==0:
                decision="PARTIAL"
                reason="Alpaca all/inactive assets plus SIP history are useful, but /v2/assets is not documented as a complete point-in-time 2019-2026 security master; do not run the full historical census from this list alone."
            elif useful:
                decision="PARTIAL"
                reason="Historical SIP/inactive coverage works, but current all-assets misses symbols already present in the frozen manifest; merge/reconstruct the universe before a multi-year census."
            else:
                decision="FAIL"
                reason="Alpaca all/inactive assets did not demonstrate enough historical-universe capability for the 2019-2026 census."
            report={"version":VERSION,"build":BUILD,"probe_id":"IPR-HISTORICAL-UNIVERSE-CAPABILITY-2026-09-05-A","status":"COMPLETED","decision":decision,"decision_reason":reason,"historical_census_allowed":False,"fail_closed":True,"diagnostics":diagnostics,"historical_reference_cases":case_results,"meta_symbol_mapping_test":{"symbol":"META","bars":len(mapped),"has_pre_rename_days":diagnostics["meta_mapping_has_pre_rename_days"]},"next_step":"Design a documented historical-universe reconstruction only after reviewing this report.","completed_at":iso()}
            self.universe_probe_report=report
            if self.redis.configured:self.redis.set_json(self.universe_probe_key("report"),report)
            self._set_universe_probe_state(status="COMPLETED",message=f"Historical-universe probe completed: {decision}",decision=decision,historical_census_allowed=False)
        except Exception as exc:
            logging.exception("Historical universe capability probe failed")
            self._set_universe_probe_state(status="ERROR",message="Historical-universe probe failed closed",historical_census_allowed=False,last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.universe_probe_lock:self.universe_probe_thread=None

    def start_historical_universe_probe(self) -> tuple[bool,str]:
        if not (self.alpaca.configured and self.redis.configured): return False,"Alpaca and Redis are required"
        with self.universe_probe_lock:
            if self.universe_probe_thread and self.universe_probe_thread.is_alive(): return False,"already_running"
            self.universe_probe_state={"status":"STARTING","message":"Starting historical-universe capability probe","historical_census_allowed":False,"updated_at":iso()}
            self.universe_probe_thread=threading.Thread(target=self.historical_universe_probe_loop,name="historical-universe-probe",daemon=True)
            self.universe_probe_thread.start()
        return True,"started"

    def universe_reconstruction_key(self, suffix: str) -> str:
        return self.key(f"historical_universe_reconstruction:v1:{suffix}")

    def _set_universe_reconstruction_state(self, **updates: Any) -> None:
        with self.universe_reconstruction_lock:
            self.universe_reconstruction_state.update(updates)
            self.universe_reconstruction_state["updated_at"] = iso()
            snapshot = dict(self.universe_reconstruction_state)
        if self.redis.configured:
            self.redis.set_json(self.universe_reconstruction_key("status"), snapshot)

    @staticmethod
    def _reconstruction_sources(all_syms: set[str], manifest_syms: set[str], catalog_syms: set[str]) -> dict[str, list[str]]:
        refs = {"CELG", "TWTR", "SIVB", "ATVI", "BBBY", "META"}
        union = sorted(all_syms | manifest_syms | catalog_syms | refs)
        out = {}
        for sym in union:
            prov = []
            if sym in all_syms: prov.append("alpaca_all_assets")
            if sym in manifest_syms: prov.append("legacy_manifest")
            if sym in catalog_syms: prov.append("legacy_ndr_catalog")
            if sym in refs: prov.append("frozen_probe_reference")
            out[sym] = prov
        return out

    @staticmethod
    def _presence_summary(rows: list[dict[str, Any]], recycling_gap_days: int = 730) -> dict[str, Any]:
        dates = []
        for row in rows:
            raw = str(row.get("t") or "")[:10]
            try: dates.append(date.fromisoformat(raw))
            except ValueError: continue
        dates = sorted(set(dates))
        years = sorted({d.year for d in dates})
        max_gap = max(((b-a).days for a,b in zip(dates, dates[1:])), default=0)
        return {"months_with_sip":len(dates),"years_with_sip":years,"first_sip_month":dates[0].isoformat() if dates else None,"last_sip_month":dates[-1].isoformat() if dates else None,"max_observed_month_gap_days":max_gap,"ticker_recycling_risk":bool(max_gap >= recycling_gap_days)}

    def historical_universe_reconstruction_loop(self) -> None:
        try:
            probe = self.redis.get_json(self.universe_probe_key("report"), None)
            if not probe or probe.get("decision") != "PARTIAL":
                raise RuntimeError("Historical-universe capability probe PARTIAL report is required")
            self._set_universe_reconstruction_state(status="RUNNING",phase="SOURCE_UNION",message="Building source union with provenance; no entity merge",historical_census_allowed=False)
            all_assets = self.alpaca.assets_by_status(None)
            all_syms = self._asset_symbol_set(all_assets)
            manifest = self.redis.get_json(f"{self.source_prefix}:manifest", {})
            manifest_syms = {str(x).upper() for x in (manifest.get("symbols") or []) if SYMBOL_RE.fullmatch(str(x).upper())}
            catalog = self.redis.get_json(f"{self.source_prefix}:explosions:catalog", {})
            catalog_syms = {str(x.get("symbol") or "").upper() for x in (catalog.get("cases") or []) if SYMBOL_RE.fullmatch(str(x.get("symbol") or "").upper())}
            provenance = self._reconstruction_sources(all_syms, manifest_syms, catalog_syms)
            symbols = sorted(provenance)
            self.redis.set_json(self.universe_reconstruction_key("source_provenance"), provenance)
            completed = set(self.redis.get_json(self.universe_reconstruction_key("completed_batches"), []))
            batch_size = int(HISTORICAL_UNIVERSE_RECONSTRUCTION_SPEC["batch_size"])
            total_batches = math.ceil(len(symbols)/batch_size)
            start = datetime(2019,1,1,tzinfo=UTC); end = datetime(2026,9,1,tzinfo=UTC)
            self._set_universe_reconstruction_state(status="RUNNING",phase="SIP_PRESENCE_INDEX",message="Indexing monthly SIP presence",symbol_count=len(symbols),total_batches=total_batches,completed_batches=len(completed),historical_census_allowed=False)
            for bi in range(total_batches):
                if self.universe_reconstruction_stop_event.is_set():
                    self._set_universe_reconstruction_state(status="PAUSED",phase="SIP_PRESENCE_INDEX",message="Paused safely between batches",completed_batches=len(completed),total_batches=total_batches,historical_census_allowed=False); return
                if bi in completed: continue
                batch = symbols[bi*batch_size:(bi+1)*batch_size]
                bars = self.alpaca.bars(batch,start,end,feed="sip",adjustment="raw",timeframe="1Month")
                summaries = {sym:self._presence_summary(bars.get(sym,[]), int(HISTORICAL_UNIVERSE_RECONSTRUCTION_SPEC["recycling_gap_days"])) for sym in batch}
                self.redis.set_json(self.universe_reconstruction_key(f"batch:{bi}"), summaries)
                completed.add(bi); self.redis.set_json(self.universe_reconstruction_key("completed_batches"), sorted(completed))
                self._set_universe_reconstruction_state(status="RUNNING",phase="SIP_PRESENCE_INDEX",message=f"Completed presence batch {bi+1}/{total_batches}",symbol_count=len(symbols),completed_batches=len(completed),total_batches=total_batches,historical_census_allowed=False)
            year_counts={str(y):0 for y in range(2019,2027)}; observed=0; recycling=0; records={}
            for bi in range(total_batches):
                chunk=self.redis.get_json(self.universe_reconstruction_key(f"batch:{bi}"), {})
                for sym, info in chunk.items():
                    rec={"symbol":sym,"sources":provenance.get(sym,[]),**info}; records[sym]=rec
                    if info.get("months_with_sip",0)>0: observed += 1
                    if info.get("ticker_recycling_risk"): recycling += 1
                    for y in info.get("years_with_sip",[]):
                        if str(y) in year_counts: year_counts[str(y)] += 1
            report={"version":VERSION,"build":BUILD,"reconstruction_id":HISTORICAL_UNIVERSE_RECONSTRUCTION_SPEC["reconstruction_id"],"reconstruction_sha256":HISTORICAL_UNIVERSE_RECONSTRUCTION_SHA256,"status":"COMPLETED","period":HISTORICAL_UNIVERSE_RECONSTRUCTION_SPEC["period"],"source_counts":{"alpaca_all_assets":len(all_syms),"legacy_manifest":len(manifest_syms),"legacy_ndr_catalog":len(catalog_syms),"union_symbols":len(symbols)},"symbols_with_sip_presence_2019_2026":observed,"symbols_without_sip_presence_2019_2026":len(symbols)-observed,"year_presence_counts":year_counts,"ticker_recycling_risk_count":recycling,"ticker_recycling_policy":HISTORICAL_UNIVERSE_RECONSTRUCTION_SPEC["ticker_recycling_policy"],"dedup_policy":HISTORICAL_UNIVERSE_RECONSTRUCTION_SPEC["dedup_policy"],"historical_census_allowed":False,"stop_and_review_required":True,"records_key":self.universe_reconstruction_key("records"),"completed_at":iso()}
            self.redis.set_json(self.universe_reconstruction_key("records"), records)
            self.redis.set_json(self.universe_reconstruction_key("report"), report)
            self._set_universe_reconstruction_state(status="COMPLETED",phase="STOP_REVIEW",message="Historical universe reconstructed; STOP and review before census",historical_census_allowed=False,stop_and_review_required=True,symbol_count=len(symbols),symbols_with_sip_presence=observed,ticker_recycling_risk_count=recycling)
        except Exception as exc:
            logging.exception("Historical universe reconstruction failed")
            self._set_universe_reconstruction_state(status="ERROR",phase="BLOCKED",message="Historical-universe reconstruction failed closed",historical_census_allowed=False,last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.universe_reconstruction_lock: self.universe_reconstruction_thread=None

    def start_historical_universe_reconstruction(self) -> tuple[bool,str]:
        if not (self.alpaca.configured and self.redis.configured): return False,"Alpaca and Redis are required"
        with self.universe_reconstruction_lock:
            if self.universe_reconstruction_thread and self.universe_reconstruction_thread.is_alive(): return False,"already_running"
            self.universe_reconstruction_stop_event.clear()
            self.universe_reconstruction_state={"status":"STARTING","phase":"GATE","message":"Starting historical-universe reconstruction","historical_census_allowed":False,"updated_at":iso()}
            self.universe_reconstruction_thread=threading.Thread(target=self.historical_universe_reconstruction_loop,name="historical-universe-reconstruction",daemon=True); self.universe_reconstruction_thread.start()
        return True,"started"

    def historical_census_key(self, suffix: str) -> str:
        return self.key(f"historical_census:v1:{suffix}")

    def _set_historical_census_state(self, **updates: Any) -> None:
        with self.historical_census_lock:
            self.historical_census_state.update(updates)
            self.historical_census_state["updated_at"] = iso()
            snapshot = dict(self.historical_census_state)
        if self.redis.configured:
            self.redis.set_json(self.historical_census_key("status"), snapshot)

    def _historical_census_gate(self) -> tuple[bool, str]:
        if not (self.redis.configured and self.alpaca.configured):
            return False, "Alpaca and Redis are required"
        report = self.redis.get_json(self.universe_reconstruction_key("report"), None)
        if not report or report.get("status") != "COMPLETED":
            return False, "Completed historical-universe reconstruction is required"
        if str(report.get("reconstruction_sha256")) != HISTORICAL_UNIVERSE_RECONSTRUCTION_SHA256:
            return False, "Historical-universe reconstruction SHA does not match frozen v1.7.1"
        records = self.redis.get_json(self.universe_reconstruction_key("records"), None)
        if not isinstance(records, dict) or len(records) < 10000:
            return False, "Historical-universe records are missing or incomplete"
        return True, "allowed"

    @staticmethod
    def _historical_year_symbols(records: dict[str, Any], year: int) -> list[str]:
        return sorted(sym for sym, rec in records.items()
                      if SYMBOL_RE.fullmatch(str(sym).upper()) and year in (rec.get("years_with_sip") or []))

    @staticmethod
    def _coarse_ladder(rows: list[dict[str, Any]], ladders: list[float]) -> dict[str, Any]:
        ordered = sorted(rows, key=lambda x: str(x.get("t") or ""))
        running_min = None; running_min_ts = None
        hits = {str(int(x)): False for x in ladders}
        first = {str(int(x)): None for x in ladders}
        max_gain = 0.0; max_high = None; max_high_ts = None; same_bar = False
        for row in ordered:
            try: lo, hi = float(row.get("l")), float(row.get("h"))
            except (TypeError, ValueError): continue
            if not (math.isfinite(lo) and math.isfinite(hi) and lo > 0 and hi > 0): continue
            ts = str(row.get("t") or "")
            if running_min is None or lo < running_min:
                running_min, running_min_ts = lo, ts
            gain = (hi / running_min - 1.0) * 100.0
            if gain > max_gain:
                max_gain, max_high, max_high_ts = gain, hi, ts
            for level in ladders:
                key = str(int(level))
                if not hits[key] and gain + 1e-12 >= level:
                    hits[key] = True
                    first[key] = ts
                    if level == 20.0 and running_min_ts == ts: same_bar = True
        return {"max_coarse_gain_pct":max_gain,"running_min_low":running_min,"running_min_ts":running_min_ts,
                "max_high":max_high,"max_high_ts":max_high_ts,"ladder_hits":hits,"ladder_first_ts":first,
                "same_bar_order_ambiguous_ge20":same_bar}

    def _historical_fetch_cycle(self, symbols: list[str], target: date) -> dict[str, list[dict[str, Any]]]:
        start, end = self._probe_cycle_bounds(target); out = {s: [] for s in symbols}
        batch_size = int(HISTORICAL_CENSUS_SPEC["request_batch_size"])
        boats_launch = date.fromisoformat(HISTORICAL_CENSUS_SPEC["boats_launch_date"])
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i+batch_size]
            sip = self.alpaca.bars(batch,start,end,feed="sip",adjustment="raw",timeframe=HISTORICAL_CENSUS_SPEC["coarse_timeframe"])
            boats = ({s: [] for s in batch} if target < boats_launch else
                     self.alpaca.bars(batch,start,end,feed="boats",adjustment="raw",timeframe=HISTORICAL_CENSUS_SPEC["coarse_timeframe"]))
            for symbol in batch:
                merged = {}
                for source, source_rows in (("sip",sip.get(symbol,[])),("boats",boats.get(symbol,[]))):
                    for row in source_rows:
                        ts = str(row.get("t") or "")
                        if not ts: continue
                        sess = self._probe_session(ts,target)
                        if source == "boats" and sess != "Overnight": continue
                        if ts not in merged or source == "sip": merged[ts] = {**row,"source":source,"session":sess}
                out[symbol] = [merged[k] for k in sorted(merged)]
        return out

    def historical_census_loop(self) -> None:
        try:
            allowed, reason = self._historical_census_gate()
            if not allowed: raise RuntimeError(reason)
            records = self.redis.get_json(self.universe_reconstruction_key("records"), {})
            cal = self.alpaca.calendar(date(2018,12,15), date(2026,8,31))
            sessions = sorted(date.fromisoformat(str(x["date"])) for x in cal if x.get("date") and date(2019,1,1) <= date.fromisoformat(str(x["date"])) <= date(2026,8,31))
            if len(sessions) < 1800: raise RuntimeError(f"Historical calendar unexpectedly short: {len(sessions)}")
            completed = set(self.redis.get_json(self.historical_census_key("completed_sessions"),[]) or [])
            total_candidates = int(self.redis.get_json(self.historical_census_key("candidate_count"),0) or 0)
            self._set_historical_census_state(status="RUNNING",phase="CENSUS",message="Historical 2019-2026 high-recall census",session_count=len(sessions),completed_sessions=len(completed),remaining_sessions=len(sessions)-len(completed),candidate_count=total_candidates,phase0b_allowed=False)
            for target in sessions:
                key = target.isoformat()
                if key in completed: continue
                if self.historical_census_stop_event.is_set():
                    self._set_historical_census_state(status="PAUSED",phase="CENSUS",message="Paused safely at trading-session boundary",completed_sessions=len(completed),remaining_sessions=len(sessions)-len(completed),candidate_count=total_candidates,phase0b_allowed=False); return
                symbols = self._historical_year_symbols(records,target.year)
                bars_by_symbol = self._historical_fetch_cycle(symbols,target)
                candidates=[]; quality={"symbols_expected":len(symbols),"symbols_with_bars":0,"symbols_without_bars":0,"split_suspects":0,"same_bar_ambiguous":0,"ticker_recycling_risk":0,"boats_market_structure_active":target>=date.fromisoformat(HISTORICAL_CENSUS_SPEC["boats_launch_date"])}
                for symbol in symbols:
                    rows=bars_by_symbol.get(symbol,[])
                    if not rows: quality["symbols_without_bars"]+=1; continue
                    quality["symbols_with_bars"]+=1
                    ladder=self._coarse_ladder(rows,list(HISTORICAL_CENSUS_SPEC["retained_ladders_pct"]))
                    if not ladder["ladder_hits"].get("20"): continue
                    split=self._phase0a_split_suspect(rows)
                    recycle=bool((records.get(symbol) or {}).get("ticker_recycling_risk"))
                    if split.get("suspect"): quality["split_suspects"]+=1
                    if recycle: quality["ticker_recycling_risk"]+=1
                    if ladder.get("same_bar_order_ambiguous_ge20"): quality["same_bar_ambiguous"]+=1
                    candidates.append({"symbol":symbol,"target_session":key,**ladder,"corporate_action_screen":split,"ticker_recycling_risk":recycle,"eligible_for_phase0b":not split.get("suspect"),"verified_ge20":False})
                self.redis.set_json(self.historical_census_key(f"candidates:{key}"),candidates)
                self.redis.set_json(self.historical_census_key(f"quality:{key}"),quality)
                completed.add(key); total_candidates += len(candidates)
                self.redis.set_json(self.historical_census_key("completed_sessions"),sorted(completed)); self.redis.set_json(self.historical_census_key("candidate_count"),total_candidates)
                self._set_historical_census_state(status="RUNNING",phase="CENSUS",message=f"Completed historical census session {key}",current_session=key,current_year=target.year,year_symbol_count=len(symbols),completed_sessions=len(completed),remaining_sessions=len(sessions)-len(completed),candidate_count=total_candidates,last_session_candidates=len(candidates),last_session_quality=quality,phase0b_allowed=False)
            report={"version":VERSION,"build":BUILD,"census_id":HISTORICAL_CENSUS_SPEC["census_id"],"historical_census_sha256":HISTORICAL_CENSUS_SHA256,"reconstruction_sha256":HISTORICAL_UNIVERSE_RECONSTRUCTION_SHA256,"status":"COMPLETED","period":HISTORICAL_CENSUS_SPEC["period"],"sessions":len(sessions),"completed_sessions":len(completed),"coarse_candidates":total_candidates,"verified_ge20_count":0,"phase0b_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
            self.redis.set_json(self.historical_census_key("report"),report)
            self._set_historical_census_state(status="COMPLETED",phase="STOP_REVIEW",message="Historical Census completed; STOP and audit before Phase 0B",completed_sessions=len(completed),remaining_sessions=0,candidate_count=total_candidates,phase0b_allowed=False,stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Historical Census failed")
            self._set_historical_census_state(status="ERROR",phase="BLOCKED",message="Historical Census failed closed",phase0b_allowed=False,last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.historical_census_lock: self.historical_census_thread=None

    def start_historical_census(self) -> tuple[bool,str]:
        allowed,reason=self._historical_census_gate()
        if not allowed:return False,reason
        with self.historical_census_lock:
            if self.historical_census_thread and self.historical_census_thread.is_alive():return False,"already_running"
            self.historical_census_stop_event.clear()
            self.historical_census_thread=threading.Thread(target=self.historical_census_loop,name="historical-census-2019-2026",daemon=True)
            self.historical_census_thread.start()
        return True,"started"

    def historical_census_audit_key(self, suffix: str) -> str:
        return self.key(f"historical_census_audit:v1:{suffix}")

    def _set_historical_census_audit_state(self, **updates: Any) -> None:
        with self.historical_census_audit_lock:
            self.historical_census_audit_state.update(updates)
            self.historical_census_audit_state["updated_at"] = iso()
            snapshot = dict(self.historical_census_audit_state)
        if self.redis.configured:
            self.redis.set_json(self.historical_census_audit_key("status"), snapshot)

    def _historical_census_audit_gate(self) -> tuple[bool, str]:
        if not self.redis.configured:
            return False, "Redis is required"
        report = self.redis.get_json(self.historical_census_key("report"), None)
        if not report or report.get("status") != "COMPLETED":
            return False, "Completed Historical Census report is required"
        if str(report.get("historical_census_sha256")) != HISTORICAL_CENSUS_SHA256:
            return False, "Stored Historical Census SHA does not match frozen v1.7.2 census"
        completed = self.redis.get_json(self.historical_census_key("completed_sessions"), []) or []
        if len(completed) != int(report.get("sessions") or 0) or len(completed) < 1800:
            return False, "Historical Census completed-session index is incomplete"
        return True, "allowed"

    @staticmethod
    def _audit_percentiles(values: list[int]) -> dict[str, float | int | None]:
        if not values:
            return {"min":None,"p25":None,"median":None,"p75":None,"p90":None,"p95":None,"max":None,"mean":None}
        ordered=sorted(values); n=len(ordered)
        def q(p: float) -> float:
            pos=(n-1)*p; lo=int(math.floor(pos)); hi=int(math.ceil(pos))
            if lo==hi:return float(ordered[lo])
            return float(ordered[lo]+(ordered[hi]-ordered[lo])*(pos-lo))
        return {"min":ordered[0],"p25":round(q(.25),2),"median":round(q(.5),2),"p75":round(q(.75),2),"p90":round(q(.9),2),"p95":round(q(.95),2),"max":ordered[-1],"mean":round(sum(ordered)/n,2)}

    def historical_census_audit_loop(self) -> None:
        try:
            allowed, reason = self._historical_census_audit_gate()
            if not allowed: raise RuntimeError(reason)
            census_report=self.redis.get_json(self.historical_census_key("report"),{})
            sessions=sorted(self.redis.get_json(self.historical_census_key("completed_sessions"),[]) or [])
            by_year={str(y):{"sessions":0,"candidates":0,"same_bar_ambiguous":0,"split_suspects":0,"ticker_recycling_risk":0,"symbols_expected":0,"symbols_with_bars":0,"symbols_without_bars":0} for y in range(2019,2027)}
            threshold_counts={str(x):0 for x in (5,10,15,20,30,50)}
            gain_bands={"20_to_lt30":0,"30_to_lt50":0,"ge50":0}
            unique_symbols=set(); symbol_counts=defaultdict(int); session_counts=[]; top_sessions=[]
            totals={"candidates":0,"eligible_for_phase0b":0,"ineligible_split_suspect":0,"same_bar_ambiguous":0,"ticker_recycling_risk":0,"quality_split_suspects":0,"quality_same_bar_ambiguous":0,"quality_ticker_recycling_risk":0,"symbols_expected":0,"symbols_with_bars":0,"symbols_without_bars":0}
            for idx,key in enumerate(sessions,1):
                candidates=self.redis.get_json(self.historical_census_key(f"candidates:{key}"),[]) or []
                quality=self.redis.get_json(self.historical_census_key(f"quality:{key}"),{}) or {}
                year=str(key)[:4]; yc=by_year.setdefault(year,{"sessions":0,"candidates":0,"same_bar_ambiguous":0,"split_suspects":0,"ticker_recycling_risk":0,"symbols_expected":0,"symbols_with_bars":0,"symbols_without_bars":0})
                yc["sessions"]+=1; yc["candidates"]+=len(candidates)
                for field in ("same_bar_ambiguous","split_suspects","ticker_recycling_risk","symbols_expected","symbols_with_bars","symbols_without_bars"):
                    val=int(quality.get(field,0) or 0); yc[field]+=val
                    if field in ("same_bar_ambiguous","split_suspects","ticker_recycling_risk"): totals[f"quality_{field}"]+=val
                    else: totals[field]+=val
                session_counts.append(len(candidates)); top_sessions.append((len(candidates),key))
                totals["candidates"]+=len(candidates)
                for c in candidates:
                    sym=str(c.get("symbol") or ""); unique_symbols.add(sym); symbol_counts[sym]+=1
                    hits=c.get("ladder_hits") or {}
                    for level in threshold_counts:
                        if hits.get(level): threshold_counts[level]+=1
                    gain=float(c.get("max_coarse_gain_pct") or 0.0)
                    if gain>=50: gain_bands["ge50"]+=1
                    elif gain>=30: gain_bands["30_to_lt50"]+=1
                    else: gain_bands["20_to_lt30"]+=1
                    if c.get("eligible_for_phase0b"): totals["eligible_for_phase0b"]+=1
                    else: totals["ineligible_split_suspect"]+=1
                    if c.get("same_bar_order_ambiguous_ge20"): totals["same_bar_ambiguous"]+=1
                    if c.get("ticker_recycling_risk"): totals["ticker_recycling_risk"]+=1
                if idx % 50 == 0 or idx == len(sessions):
                    self._set_historical_census_audit_state(status="RUNNING",phase="AUDIT",message=f"Audited {idx}/{len(sessions)} stored sessions",sessions_audited=idx,total_sessions=len(sessions),phase0b_allowed=False)
            top_sessions=[{"session":k,"candidates":n} for n,k in sorted(top_sessions,reverse=True)[:20]]
            top_symbols=[{"symbol":sym,"candidate_cycles":n} for sym,n in sorted(symbol_counts.items(),key=lambda kv:(-kv[1],kv[0]))[:20]]
            bars_den=totals["symbols_with_bars"]+totals["symbols_without_bars"]
            report={"version":VERSION,"build":BUILD,"audit_id":"IPR-HISTORICAL-CENSUS-AUDIT-2026-09-07-A","status":"COMPLETED","source_historical_census_sha256":HISTORICAL_CENSUS_SHA256,"source_reconstruction_sha256":census_report.get("reconstruction_sha256"),"period":census_report.get("period"),"sessions":len(sessions),"candidate_scope_note":"Stored Census candidates are already coarse >=20% candidates. Therefore +5/+10/+15 counts are descriptive within that stored >=20% set, not counts for the full scanned universe.","totals":{**totals,"unique_symbols":len(unique_symbols),"bars_coverage_pct":round(totals["symbols_with_bars"]/bars_den*100,4) if bars_den else None},"threshold_counts_within_stored_ge20_candidates":threshold_counts,"max_coarse_gain_bands":gain_bands,"candidates_by_year":by_year,"candidates_per_session":self._audit_percentiles(session_counts),"top_sessions":top_sessions,"top_symbols":top_symbols,"integrity":{"stored_candidate_count_matches_census_report":totals["candidates"]==int(census_report.get("coarse_candidates") or -1),"candidate_vs_quality_same_bar_match":totals["same_bar_ambiguous"]==totals["quality_same_bar_ambiguous"],"candidate_vs_quality_split_match":totals["ineligible_split_suspect"]==totals["quality_split_suspects"],"candidate_vs_quality_recycling_match":totals["ticker_recycling_risk"]==totals["quality_ticker_recycling_risk"]},"phase0b_allowed":False,"stop_and_review_required":True,"alpaca_requests_made":0,"census_rescan_performed":False,"completed_at":iso()}
            self.redis.set_json(self.historical_census_audit_key("report"),report)
            self._set_historical_census_audit_state(status="COMPLETED",phase="STOP_REVIEW",message="Historical Census audit completed; STOP and review before Phase 0B",sessions_audited=len(sessions),total_sessions=len(sessions),phase0b_allowed=False,stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Historical Census audit failed")
            self._set_historical_census_audit_state(status="ERROR",phase="BLOCKED",message="Historical Census audit failed closed",phase0b_allowed=False,last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.historical_census_audit_lock:self.historical_census_audit_thread=None

    def start_historical_census_audit(self) -> tuple[bool,str]:
        allowed,reason=self._historical_census_audit_gate()
        if not allowed:return False,reason
        with self.historical_census_audit_lock:
            if self.historical_census_audit_thread and self.historical_census_audit_thread.is_alive():return False,"already_running"
            self.historical_census_audit_thread=threading.Thread(target=self.historical_census_audit_loop,name="historical-census-audit",daemon=True)
            self.historical_census_audit_thread.start()
        return True,"started"

    def instrument_cleanup_key(self, suffix: str) -> str:
        return self.key(f"instrument_cleanup_audit:v1:{suffix}")

    def _set_instrument_cleanup_state(self, **updates: Any) -> None:
        with self.instrument_cleanup_lock:
            self.instrument_cleanup_state.update(updates)
            self.instrument_cleanup_state["updated_at"] = iso()
            snapshot = dict(self.instrument_cleanup_state)
        if self.redis.configured:
            self.redis.set_json(self.instrument_cleanup_key("status"), snapshot)

    def _instrument_cleanup_gate(self) -> tuple[bool, str]:
        if not self.redis.configured:
            return False, "Redis is required"
        census = self.redis.get_json(self.historical_census_key("report"), None)
        audit = self.redis.get_json(self.historical_census_audit_key("report"), None)
        if not census or census.get("status") != "COMPLETED" or int(census.get("coarse_candidates") or 0) != 338323:
            return False, "Frozen completed Historical Census (338323 candidates) is required"
        if str(census.get("historical_census_sha256")) != HISTORICAL_CENSUS_SHA256:
            return False, "Historical Census SHA mismatch"
        if not audit or audit.get("status") != "COMPLETED" or not (audit.get("integrity") or {}).get("stored_candidate_count_matches_census_report"):
            return False, "Completed v1.7.3 Census Audit with passing integrity is required"
        if int((audit.get("totals") or {}).get("candidates") or 0) != 338323:
            return False, "Census Audit candidate count mismatch"
        return True, "allowed"

    @staticmethod
    def _instrument_name_classification(asset: dict[str, Any] | None) -> dict[str, Any]:
        if not asset:
            return {"bucket":"unresolved_no_metadata","subtype":"unknown","evidence":None,"name":None}
        name = " ".join(str(asset.get("name") or "").strip().split())
        low = name.lower()
        # Strong name evidence only. Symbol suffixes are deliberately not classification evidence.
        rules = [
            ("warrant", (r"\bwarrants?\b", r"\bwts?\b")),
            ("unit", (r"\bunits?\b",)),
            ("right", (r"\brights?\b",)),
            ("preferred", (r"\bpreferred\b", r"\bpreference shares?\b")),
            ("fund_etp", (r"\betf\b", r"exchange[- ]traded fund", r"\betn\b", r"exchange[- ]traded note")),
            ("debt_security", (r"\bsenior notes?\b", r"\bdebentures?\b", r"\bbonds? due\b")),
        ]
        for subtype, pats in rules:
            for pat in pats:
                if re.search(pat, low):
                    return {"bucket":"metadata_non_common","subtype":subtype,"evidence":f"asset_name:{pat}","name":name}
        common_patterns = (
            r"\bcommon stock\b", r"\bcommon shares?\b", r"\bordinary shares?\b",
            r"american depositary shares?", r"american depositary receipts?", r"\badr\b",
        )
        for pat in common_patterns:
            if re.search(pat, low):
                return {"bucket":"metadata_common_like","subtype":"common_or_ordinary_equity","evidence":f"asset_name:{pat}","name":name}
        return {"bucket":"unresolved_metadata_ambiguous","subtype":"unknown","evidence":"asset_name_not_decisive","name":name}

    @staticmethod
    def _suffix_diagnostics(symbol: str) -> list[str]:
        out=[]
        if symbol.endswith("W"): out.append("suffix_W_possible_warrant")
        if symbol.endswith("U"): out.append("suffix_U_possible_unit")
        if symbol.endswith("R"): out.append("suffix_R_possible_right")
        if symbol.endswith("P"): out.append("suffix_P_possible_preferred")
        return out

    def instrument_cleanup_audit_loop(self) -> None:
        try:
            allowed, reason = self._instrument_cleanup_gate()
            if not allowed:
                raise RuntimeError(reason)
            self._set_instrument_cleanup_state(status="RUNNING", phase="LOAD", message="Streaming stored Census sessions to collect candidate symbols; no Census rescan and no market-bar requests", sessions_loaded=0, phase0b_allowed=False)
            census = self.redis.get_json(self.historical_census_key("report"), {})
            sessions = list(self.redis.get_json(self.historical_census_key("completed_sessions"), []) or [])
            if len(sessions) != 1926:
                raise RuntimeError(f"Expected 1926 stored sessions, found {len(sessions)}")
            records = self.redis.get_json(self.universe_reconstruction_key("records"), {}) or {}

            # PASS 1 (streaming): collect only unique symbols.  v1.7.4 retained all
            # 338k candidate dictionaries in RAM and could be OOM-killed on Render.
            candidate_symbols=set()
            loaded_candidate_count=0
            for idx,key in enumerate(sessions,1):
                rows=self.redis.get_json(self.historical_census_key(f"candidates:{key}"), []) or []
                loaded_candidate_count += len(rows)
                candidate_symbols.update(str(x.get("symbol") or "").upper() for x in rows if x.get("symbol"))
                if idx % 50 == 0 or idx == len(sessions):
                    self._set_instrument_cleanup_state(status="RUNNING",phase="LOAD",message=f"Streamed {idx}/{len(sessions)} stored sessions; no candidate rows retained in RAM",sessions_loaded=idx,total_sessions=len(sessions),loaded_candidate_count=loaded_candidate_count,unique_candidate_symbols=len(candidate_symbols),phase0b_allowed=False)
                del rows
            if loaded_candidate_count != 338323:
                raise RuntimeError(f"Frozen Census candidate count mismatch while streaming: {loaded_candidate_count}")

            self._set_instrument_cleanup_state(status="RUNNING", phase="METADATA", message="Fetching one Alpaca /v2/assets metadata snapshot; no historical bars", unique_candidate_symbols=len(candidate_symbols), phase0b_allowed=False)
            assets = self.alpaca.assets_by_status(None)
            asset_map={str(a.get("symbol") or "").upper():a for a in assets if str(a.get("symbol") or "").upper() in candidate_symbols}
            symbol_map={}
            symbol_bucket_counts=defaultdict(int); symbol_subtype_counts=defaultdict(int)
            suffix_diag_counts=defaultdict(int)
            for sym in sorted(candidate_symbols):
                rec=records.get(sym) or {}
                base=self._instrument_name_classification(asset_map.get(sym))
                recycling=bool(rec.get("ticker_recycling_risk"))
                if recycling:
                    base={**base,"pre_recycling_bucket":base["bucket"],"bucket":"unresolved_ticker_recycling","evidence":"ticker_recycling_risk_blocks_cross_era_metadata_classification"}
                diags=self._suffix_diagnostics(sym)
                base.update({"symbol":sym,"ticker_recycling_risk":recycling,"suffix_diagnostics":diags,"metadata_present":sym in asset_map})
                symbol_map[sym]=base
                symbol_bucket_counts[base["bucket"]]+=1; symbol_subtype_counts[base["subtype"]]+=1
                for d in diags:suffix_diag_counts[d]+=1
            del assets, asset_map, records

            # PASS 2 (streaming): re-read one session at a time and aggregate only.
            totals=defaultdict(int); by_year={str(y):defaultdict(int) for y in range(2019,2027)}
            subtype_candidate_counts=defaultdict(int); top_non_common=defaultdict(int); top_unresolved=defaultdict(int); top_common=defaultdict(int)
            for idx,key in enumerate(sessions,1):
                rows=self.redis.get_json(self.historical_census_key(f"candidates:{key}"), []) or []
                year=str(key)[:4]
                for c in rows:
                    sym=str(c.get("symbol") or "").upper(); cls=symbol_map.get(sym) or {"bucket":"unresolved_no_metadata","subtype":"unknown"}
                    bucket=cls["bucket"]; subtype=cls.get("subtype") or "unknown"
                    totals["candidates"]+=1; totals[bucket]+=1; by_year[year]["candidates"]+=1; by_year[year][bucket]+=1
                    subtype_candidate_counts[subtype]+=1
                    if c.get("corporate_action_screen",{}).get("suspect"):
                        totals["split_suspect"]+=1; by_year[year]["split_suspect"]+=1
                    if c.get("same_bar_order_ambiguous_ge20"):
                        totals["same_bar_ambiguous"]+=1; by_year[year]["same_bar_ambiguous"]+=1
                    clean = bucket=="metadata_common_like" and not c.get("corporate_action_screen",{}).get("suspect") and not cls.get("ticker_recycling_risk")
                    if clean:
                        totals["recommended_clean_for_phase0b"]+=1; by_year[year]["recommended_clean_for_phase0b"]+=1; top_common[sym]+=1
                    elif bucket=="metadata_non_common": top_non_common[sym]+=1
                    else: top_unresolved[sym]+=1
                if idx%50==0 or idx==len(sessions):
                    self._set_instrument_cleanup_state(status="RUNNING",phase="AUDIT",message=f"Classified {idx}/{len(sessions)} stored sessions (streaming)",sessions_audited=idx,total_sessions=len(sessions),phase0b_allowed=False)
                del rows
            unresolved=sum(v for k,v in totals.items() if k.startswith("unresolved_"))
            totals["unresolved_total"]=unresolved
            totals["excluded_metadata_non_common"]=totals.get("metadata_non_common",0)
            report={
                "version":VERSION,"build":BUILD,"audit_id":"IPR-INSTRUMENT-TYPE-CLEANUP-AUDIT-2026-09-07-B","status":"COMPLETED",
                "source_historical_census_sha256":census.get("historical_census_sha256"),"source_reconstruction_sha256":census.get("reconstruction_sha256"),
                "period":census.get("period"),"sessions":len(sessions),"unique_candidate_symbols":len(candidate_symbols),
                "implementation":{"streaming_two_pass":True,"all_candidate_rows_retained_in_ram":False,"reason":"Avoid Render OOM/process death observed in v1.7.4 LOAD"},
                "policy":{
                    "goal":"Separate likely common/ordinary equity from non-common instruments before Phase 0B without altering stored Census candidates.",
                    "metadata_source":"One current Alpaca /v2/assets?asset_class=us_equity snapshot including active and inactive assets; no market-bar request.",
                    "point_in_time_warning":"Alpaca asset metadata is not treated as historical point-in-time entity identity. Any ticker-recycling-risk symbol is unresolved regardless of current name metadata.",
                    "suffix_rule":"W/U/R/P suffixes are diagnostics only and never sufficient for exclusion.",
                    "non_common_rule":"Only strong asset-name evidence for warrant/unit/right/preferred/fund-ETP/debt is classified metadata_non_common.",
                    "common_rule":"Only strong asset-name evidence for common stock/common shares/ordinary shares/ADR-ADS is classified metadata_common_like.",
                    "unresolved_rule":"Missing or ambiguous metadata remains unresolved and is not silently deleted.",
                    "split_rule":"Existing Census split suspects remain ineligible for recommended clean Phase 0B.",
                },
                "requests":{"alpaca_asset_metadata_requests":1,"alpaca_market_bar_requests":0,"census_rescan_performed":False},
                "symbol_classification_counts":dict(symbol_bucket_counts),"symbol_subtype_counts":dict(symbol_subtype_counts),"suffix_diagnostic_symbol_counts":dict(suffix_diag_counts),
                "candidate_classification_totals":dict(totals),"candidate_subtype_counts":dict(subtype_candidate_counts),
                "candidates_by_year":{y:dict(v) for y,v in by_year.items()},
                "top_metadata_non_common_symbols":[{"symbol":s,"candidate_cycles":n,"classification":symbol_map[s]} for s,n in sorted(top_non_common.items(),key=lambda kv:(-kv[1],kv[0]))[:30]],
                "top_unresolved_symbols":[{"symbol":s,"candidate_cycles":n,"classification":symbol_map[s]} for s,n in sorted(top_unresolved.items(),key=lambda kv:(-kv[1],kv[0]))[:30]],
                "top_metadata_common_like_symbols":[{"symbol":s,"candidate_cycles":n,"classification":symbol_map[s]} for s,n in sorted(top_common.items(),key=lambda kv:(-kv[1],kv[0]))[:30]],
                "integrity":{
                    "candidate_count_matches_frozen_census":totals["candidates"]==338323,
                    "stream_load_count_matches_frozen_census":loaded_candidate_count==338323,
                    "sessions_match_frozen_census":len(sessions)==1926,
                    "classification_partition_matches":totals["candidates"]==(totals.get("metadata_common_like",0)+totals.get("metadata_non_common",0)+unresolved),
                    "original_candidates_mutated":False,
                },
                "phase0b_allowed":False,"stop_and_review_required":True,"completed_at":iso(),
            }
            self.redis.set_json(self.instrument_cleanup_key("symbol_classification"),symbol_map)
            self.redis.set_json(self.instrument_cleanup_key("report"),report)
            self._set_instrument_cleanup_state(status="COMPLETED",phase="STOP_REVIEW",message="Instrument-type cleanup audit completed; STOP and review before Phase 0B",sessions_audited=len(sessions),total_sessions=len(sessions),phase0b_allowed=False,stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Instrument-type cleanup audit failed")
            self._set_instrument_cleanup_state(status="ERROR",phase="BLOCKED",message="Instrument-type cleanup audit failed closed",phase0b_allowed=False,last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.instrument_cleanup_lock:
                self.instrument_cleanup_thread=None

    def start_instrument_cleanup_audit(self) -> tuple[bool,str]:
        allowed,reason=self._instrument_cleanup_gate()
        if not allowed:return False,reason
        if not self.alpaca.configured:return False,"Alpaca is required for the single asset-metadata snapshot"
        with self.instrument_cleanup_lock:
            if self.instrument_cleanup_thread and self.instrument_cleanup_thread.is_alive():return False,"already_running"
            self.instrument_cleanup_thread=threading.Thread(target=self.instrument_cleanup_audit_loop,name="instrument-type-cleanup-audit",daemon=True)
            self.instrument_cleanup_thread.start()
        return True,"started"

    def pre0b_audit_key(self, suffix: str) -> str:
        return self.key(f"pre0b_resolution_normalization_audit:v1:{suffix}")

    def _set_pre0b_audit_state(self, **updates: Any) -> None:
        with self.pre0b_audit_lock:
            self.pre0b_audit_state.update(updates)
            self.pre0b_audit_state["updated_at"] = iso()
            snap = dict(self.pre0b_audit_state)
        if self.redis.configured:
            self.redis.set_json(self.pre0b_audit_key("status"), snap)

    def _pre0b_audit_gate(self) -> tuple[bool, str]:
        if not self.redis.configured:
            return False, "Redis is required"
        cleanup = self.redis.get_json(self.instrument_cleanup_key("report"), None)
        classes = self.redis.get_json(self.instrument_cleanup_key("symbol_classification"), None)
        census_audit = self.redis.get_json(self.historical_census_audit_key("report"), None)
        if not cleanup or cleanup.get("status") != "COMPLETED" or cleanup.get("version") != "1.7.5":
            return False, "Completed v1.7.5 instrument cleanup is required"
        integ = cleanup.get("integrity") or {}
        if not all(integ.get(k) for k in ("candidate_count_matches_frozen_census","stream_load_count_matches_frozen_census","sessions_match_frozen_census","classification_partition_matches")):
            return False, "v1.7.5 cleanup integrity must pass"
        if int((cleanup.get("candidate_classification_totals") or {}).get("candidates") or 0) != 338323:
            return False, "Frozen candidate count mismatch"
        if not isinstance(classes, dict) or len(classes) != 8514:
            return False, "Frozen v1.7.5 symbol classification map is required"
        if not census_audit or census_audit.get("status") != "COMPLETED":
            return False, "Completed Census audit is required for annual denominators"
        return True, "allowed"

    @staticmethod
    def _resolve_ambiguous_asset_name(name: str | None) -> dict[str, str] | None:
        """Second-pass conservative resolver. No suffix-only decisions."""
        low = " ".join(str(name or "").lower().split())
        if not low:
            return None
        non_common = (
            ("preferred", (r"\bpfd\b", r"\bpfd ser\b", r"\bpref(?:erred)?\b")),
            ("warrant", (r"\bwarrant", r"\bwt exp\b", r"\bwts\b")),
            ("right", (r"\bright(?:s)?\b",)),
            ("unit", (r"\bunit(?:s)?\b",)),
        )
        for subtype, pats in non_common:
            for pat in pats:
                if re.search(pat, low):
                    return {"bucket":"metadata_non_common","subtype":subtype,"evidence":f"second_pass_asset_name:{pat}"}
        common = (
            r"\bads\b", r"american depositary shs?", r"depositary shs?",
            r"\bcom par\b", r"\bcom new\b", r"\bcommon\b",
        )
        for pat in common:
            if re.search(pat, low):
                return {"bucket":"metadata_common_like","subtype":"common_or_ordinary_equity","evidence":f"second_pass_asset_name:{pat}"}
        return None

    def pre0b_resolution_normalization_audit_loop(self) -> None:
        try:
            allowed, reason = self._pre0b_audit_gate()
            if not allowed:
                raise RuntimeError(reason)
            self._set_pre0b_audit_state(status="RUNNING", phase="LOAD", message="Loading frozen cleanup classifications and annual coverage denominators; no Census rescan", phase0b_allowed=False)
            cleanup = self.redis.get_json(self.instrument_cleanup_key("report"), {}) or {}
            symbol_map = self.redis.get_json(self.instrument_cleanup_key("symbol_classification"), {}) or {}
            census_audit = self.redis.get_json(self.historical_census_audit_key("report"), {}) or {}
            sessions = list(self.redis.get_json(self.historical_census_key("completed_sessions"), []) or [])
            if len(sessions) != 1926:
                raise RuntimeError(f"Expected 1926 sessions, found {len(sessions)}")

            # One current asset snapshot only to re-examine ambiguous names; no bars.
            self._set_pre0b_audit_state(status="RUNNING", phase="METADATA", message="Refreshing one asset-name snapshot for unresolved-name resolution; no market bars", phase0b_allowed=False)
            assets = self.alpaca.assets_by_status(None)
            asset_map = {str(a.get("symbol") or "").upper(): a for a in assets}
            resolved_map = {}
            resolution_symbol_counts = defaultdict(int)
            for sym, old in symbol_map.items():
                new = dict(old)
                if old.get("bucket") == "unresolved_metadata_ambiguous" and not old.get("ticker_recycling_risk"):
                    asset = asset_map.get(sym) or {}
                    resolution = self._resolve_ambiguous_asset_name(asset.get("name") or old.get("name"))
                    if resolution:
                        new.update(resolution)
                        new["resolution_changed"] = True
                    else:
                        new["resolution_changed"] = False
                else:
                    new["resolution_changed"] = False
                resolved_map[sym] = new
                resolution_symbol_counts[new.get("bucket") or "unknown"] += 1
            del assets, asset_map

            totals = defaultdict(int)
            by_year = {str(y): defaultdict(int) for y in range(2019, 2027)}
            changed_candidates = 0
            for idx, key in enumerate(sessions, 1):
                rows = self.redis.get_json(self.historical_census_key(f"candidates:{key}"), []) or []
                year = str(key)[:4]
                for c in rows:
                    sym = str(c.get("symbol") or "").upper()
                    old = symbol_map.get(sym) or {"bucket":"unresolved_no_metadata"}
                    cls = resolved_map.get(sym) or old
                    bucket = cls.get("bucket") or "unresolved_no_metadata"
                    split = bool((c.get("corporate_action_screen") or {}).get("suspect"))
                    amb = bool(c.get("same_bar_order_ambiguous_ge20"))
                    totals["candidates"] += 1; by_year[year]["candidates"] += 1
                    totals[bucket] += 1; by_year[year][bucket] += 1
                    if cls.get("resolution_changed"):
                        changed_candidates += 1; totals["resolved_from_ambiguous"] += 1; by_year[year]["resolved_from_ambiguous"] += 1
                    if split:
                        totals["split_suspect"] += 1; by_year[year]["split_suspect"] += 1
                    clean = bucket == "metadata_common_like" and not split and not cls.get("ticker_recycling_risk")
                    if clean:
                        totals["recommended_clean"] += 1; by_year[year]["recommended_clean"] += 1
                        if amb:
                            totals["same_bar_ambiguous_within_clean"] += 1; by_year[year]["same_bar_ambiguous_within_clean"] += 1
                        else:
                            totals["temporally_ordered_coarse_within_clean"] += 1; by_year[year]["temporally_ordered_coarse_within_clean"] += 1
                    if amb:
                        totals["same_bar_ambiguous_all"] += 1; by_year[year]["same_bar_ambiguous_all"] += 1
                if idx % 100 == 0 or idx == len(sessions):
                    self._set_pre0b_audit_state(status="RUNNING", phase="AUDIT", message=f"Audited {idx}/{len(sessions)} stored sessions (streaming)", sessions_audited=idx, total_sessions=len(sessions), phase0b_allowed=False)
                del rows

            old_year = census_audit.get("candidates_by_year") or {}
            annual = {}
            for y in map(str, range(2019, 2027)):
                den = int((old_year.get(y) or {}).get("symbols_with_bars") or 0)
                vals = dict(by_year[y])
                clean = int(vals.get("recommended_clean") or 0)
                raw = int(vals.get("candidates") or 0)
                vals["symbols_with_bars_denominator"] = den
                vals["raw_candidates_per_1000_symbol_sessions_with_bars"] = round(raw / den * 1000, 6) if den else None
                vals["clean_candidates_per_1000_symbol_sessions_with_bars"] = round(clean / den * 1000, 6) if den else None
                vals["clean_same_bar_ambiguous_pct"] = round((int(vals.get("same_bar_ambiguous_within_clean") or 0) / clean * 100), 4) if clean else None
                annual[y] = vals

            unresolved = sum(int(totals.get(k) or 0) for k in ("unresolved_metadata_ambiguous","unresolved_no_metadata","unresolved_ticker_recycling"))
            clean = int(totals.get("recommended_clean") or 0)
            report = {
                "version": VERSION, "build": BUILD, "audit_id":"IPR-PRE-PHASE0B-RESOLUTION-NORMALIZATION-AUDIT-2026-09-07-A", "status":"COMPLETED",
                "source_cleanup_audit_id": cleanup.get("audit_id"), "source_historical_census_sha256": cleanup.get("source_historical_census_sha256"),
                "period": cleanup.get("period"), "sessions": len(sessions), "candidate_count": int(totals.get("candidates") or 0),
                "resolution_policy": {
                    "purpose":"Resolve only additional decisive asset-name cases among v1.7.5 ambiguous metadata; never use suffix alone.",
                    "ticker_recycling":"Always remains unresolved.", "missing_metadata":"Always remains unresolved.",
                    "same_bar":"Not resolved here. It is measured inside the clean set and reserved for Phase 0B 1-minute chronological verification; any residual 1-minute intrabar ambiguity must remain still_ambiguous.",
                    "annual_normalization":"candidate count divided by actual symbol-sessions with bars from frozen v1.7.3 Census Audit.",
                },
                "requests":{"alpaca_asset_metadata_requests":1,"alpaca_market_bar_requests":0,"census_rescan_performed":False},
                "symbol_bucket_counts_after_resolution": dict(resolution_symbol_counts),
                "candidate_totals_after_resolution": {**dict(totals), "unresolved_total":unresolved, "recommended_clean":clean,
                    "same_bar_ambiguous_within_clean_pct": round(int(totals.get("same_bar_ambiguous_within_clean") or 0)/clean*100,4) if clean else None},
                "annual_normalized_rates": annual,
                "integrity": {
                    "candidate_count_matches_frozen_census": int(totals.get("candidates") or 0) == 338323,
                    "sessions_match_frozen_census": len(sessions) == 1926,
                    "same_bar_all_matches_v1_7_5": int(totals.get("same_bar_ambiguous_all") or 0) == int((cleanup.get("candidate_classification_totals") or {}).get("same_bar_ambiguous") or -1),
                    "original_candidates_mutated": False,
                },
                "phase0b_allowed":False, "stop_and_review_required":True, "completed_at":iso(),
            }
            self.redis.set_json(self.pre0b_audit_key("resolved_symbol_classification"), resolved_map)
            self.redis.set_json(self.pre0b_audit_key("report"), report)
            self._set_pre0b_audit_state(status="COMPLETED", phase="STOP_REVIEW", message="Pre-Phase0B resolution/normalization audit completed; STOP and review before Phase 0B design", sessions_audited=len(sessions), total_sessions=len(sessions), phase0b_allowed=False, stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Pre-Phase0B audit failed")
            self._set_pre0b_audit_state(status="ERROR", phase="BLOCKED", message="Pre-Phase0B audit failed closed", phase0b_allowed=False, last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.pre0b_audit_lock:
                self.pre0b_audit_thread = None

    def start_pre0b_resolution_normalization_audit(self) -> tuple[bool, str]:
        allowed, reason = self._pre0b_audit_gate()
        if not allowed:
            return False, reason
        if not self.alpaca.configured:
            return False, "Alpaca is required for one asset-metadata snapshot"
        with self.pre0b_audit_lock:
            if self.pre0b_audit_thread and self.pre0b_audit_thread.is_alive():
                return False, "already_running"
            self.pre0b_audit_thread = threading.Thread(target=self.pre0b_resolution_normalization_audit_loop, name="pre0b-resolution-normalization-audit", daemon=True)
            self.pre0b_audit_thread.start()
        return True, "started"

    def phase0b_window_probe_key(self, suffix: str) -> str:
        return self.key(f"phase0b_window_probe:v1:{suffix}")

    def _set_phase0b_window_probe_state(self, **updates: Any) -> None:
        with self.phase0b_window_probe_lock:
            self.phase0b_window_probe_state.update(updates)
            self.phase0b_window_probe_state["updated_at"] = iso()
            snap = dict(self.phase0b_window_probe_state)
        if self.redis.configured:
            self.redis.set_json(self.phase0b_window_probe_key("status"), snap)

    def _phase0b_window_probe_gate(self) -> tuple[bool, str]:
        if not (self.redis.configured and self.alpaca.configured):
            return False, "Alpaca and Redis are required"
        report = self.redis.get_json(self.pre0b_audit_key("report"), None)
        if not report or report.get("status") != "COMPLETED" or int(report.get("candidate_count") or 0) != 338323:
            return False, "Completed v1.7.6 Pre-Phase0B audit over the frozen 338,323-candidate Census is required"
        if int((report.get("candidate_totals_after_resolution") or {}).get("recommended_clean") or 0) != 205028:
            return False, "Frozen clean-candidate count must be 205,028"
        return True, "allowed"

    @staticmethod
    def _minute_verify(rows: list[dict[str, Any]], threshold_pct: float = 20.0) -> dict[str, Any]:
        running_min = None; running_min_ts = None; max_gain = 0.0
        for row in sorted(rows, key=lambda x: str(x.get("t") or "")):
            try: lo, hi = float(row.get("l")), float(row.get("h"))
            except (TypeError, ValueError): continue
            if not (math.isfinite(lo) and math.isfinite(hi) and lo > 0 and hi > 0): continue
            ts = str(row.get("t") or "")
            if running_min is None or lo < running_min:
                running_min, running_min_ts = lo, ts
            gain = (hi / running_min - 1.0) * 100.0
            max_gain = max(max_gain, gain)
            if gain + 1e-12 >= threshold_pct:
                if running_min_ts == ts:
                    return {"classification":"still_ambiguous","t1":running_min_ts,"t2":ts,"t1_low":running_min,"t2_high":hi,"gain_pct":gain,"max_gain_pct":max_gain}
                return {"classification":"verified","t1":running_min_ts,"t2":ts,"t1_low":running_min,"t2_high":hi,"gain_pct":gain,"max_gain_pct":max_gain}
        return {"classification":"failed","t1":running_min_ts,"t2":None,"t1_low":running_min,"t2_high":None,"gain_pct":None,"max_gain_pct":max_gain}

    def _phase0b_probe_merge_1m(self, symbol: str, target: date, start: datetime, end: datetime) -> list[dict[str, Any]]:
        sip = self.alpaca.bars([symbol], start, end, feed="sip", adjustment="raw", timeframe="1Min").get(symbol, [])
        boats = [] if target < date.fromisoformat(HISTORICAL_CENSUS_SPEC["boats_launch_date"]) else self.alpaca.bars([symbol], start, end, feed="boats", adjustment="raw", timeframe="1Min").get(symbol, [])
        merged = {}
        for source, source_rows in (("sip", sip), ("boats", boats)):
            for row in source_rows:
                ts = str(row.get("t") or "")
                if not ts: continue
                sess = self._probe_session(ts, target)
                if source == "boats" and sess != "Overnight": continue
                if ts not in merged or source == "sip": merged[ts] = {**row, "source":source, "session":sess}
        return [merged[k] for k in sorted(merged)]

    def _phase0b_window_probe_sample(self) -> list[dict[str, Any]]:
        resolved = self.redis.get_json(self.pre0b_audit_key("resolved_symbol_classification"), {}) or {}
        sessions = list(self.redis.get_json(self.historical_census_key("completed_sessions"), []) or [])
        buckets = {(str(y), a): [] for y in range(2019, 2027) for a in (False, True)}
        for key in sessions:
            y = str(key)[:4]
            for c in self.redis.get_json(self.historical_census_key(f"candidates:{key}"), []) or []:
                sym = str(c.get("symbol") or "").upper(); cls = resolved.get(sym) or {}
                split = bool((c.get("corporate_action_screen") or {}).get("suspect"))
                clean = cls.get("bucket") == "metadata_common_like" and not split and not cls.get("ticker_recycling_risk")
                if not clean: continue
                amb = bool(c.get("same_bar_order_ambiguous_ge20"))
                arr = buckets.get((y, amb))
                if arr is not None and len(arr) < 4:
                    arr.append({"symbol":sym,"target_session":key,"coarse_ambiguous":amb,"coarse_t1":c.get("running_min_ts"),"coarse_t2":((c.get("ladder_first_ts") or {}).get("20"))})
        return [x for y in map(str, range(2019,2027)) for amb in (False,True) for x in buckets[(y,amb)]]

    def phase0b_window_probe_loop(self) -> None:
        try:
            allowed, reason = self._phase0b_window_probe_gate()
            if not allowed: raise RuntimeError(reason)
            sample = self._phase0b_window_probe_sample()
            if len(sample) < 48: raise RuntimeError(f"Probe sample too small: {len(sample)}")
            self._set_phase0b_window_probe_state(status="RUNNING",phase="VERIFY",message=f"Validating full-cycle 1m ground truth vs ±60m coarse-informed window on {len(sample)} cases",sample_size=len(sample),completed=0,phase0b_allowed=False)
            results=[]; mismatches=0; comparable=0
            for i,c in enumerate(sample,1):
                target=date.fromisoformat(str(c["target_session"])); full_start,full_end=self._probe_cycle_bounds(target)
                full_rows=self._phase0b_probe_merge_1m(c["symbol"],target,full_start,full_end)
                full=self._minute_verify(full_rows)
                coarse_ts=[x for x in (c.get("coarse_t1"),c.get("coarse_t2")) if x]
                if coarse_ts:
                    dts=[datetime.fromisoformat(str(x).replace("Z","+00:00")) for x in coarse_ts]
                    b=int(PHASE0B_WINDOW_PROBE_SPEC["buffer_minutes_each_side"])
                    win_start=max(full_start,min(dts)-timedelta(minutes=b)); win_end=min(full_end,max(dts)+timedelta(minutes=b))
                    win_rows=[r for r in full_rows if win_start <= datetime.fromisoformat(str(r.get("t")).replace("Z","+00:00")) <= win_end]
                    window=self._minute_verify(win_rows)
                else:
                    win_start=win_end=None; window={"classification":"insufficient_coarse_bounds"}
                same=window.get("classification")==full.get("classification")
                if full_rows and coarse_ts:
                    comparable+=1
                    if not same: mismatches+=1
                results.append({**c,"full_cycle":full,"buffered_window":window,"classification_match":same,"full_1m_bars":len(full_rows),"window_1m_bars":len(win_rows) if coarse_ts else 0,"window_start":iso(win_start) if win_start else None,"window_end":iso(win_end) if win_end else None})
                if i%8==0 or i==len(sample): self._set_phase0b_window_probe_state(status="RUNNING",phase="VERIFY",message=f"Verified {i}/{len(sample)} probe cases",sample_size=len(sample),completed=i,mismatches=mismatches,phase0b_allowed=False)
            optimization_allowed = comparable == len(sample) and mismatches == 0
            report={"version":VERSION,"build":BUILD,"probe_id":PHASE0B_WINDOW_PROBE_SPEC["probe_id"],"probe_sha256":PHASE0B_WINDOW_PROBE_SHA256,"status":"COMPLETED","spec":PHASE0B_WINDOW_PROBE_SPEC,"sample_size":len(sample),"comparable_cases":comparable,"classification_mismatches":mismatches,"window_optimization_allowed":optimization_allowed,"full_cycle_remains_ground_truth":True,"phase0b_allowed":False,"stop_and_review_required":True,"results":results,"completed_at":iso()}
            self.redis.set_json(self.phase0b_window_probe_key("report"),report)
            self._set_phase0b_window_probe_state(status="COMPLETED",phase="STOP_REVIEW",message="Phase 0B windowing validation probe completed; STOP and review before Phase 0B implementation",sample_size=len(sample),completed=len(sample),mismatches=mismatches,window_optimization_allowed=optimization_allowed,phase0b_allowed=False,stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Phase 0B windowing probe failed")
            self._set_phase0b_window_probe_state(status="ERROR",phase="BLOCKED",message="Phase 0B windowing validation probe failed closed",phase0b_allowed=False,last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.phase0b_window_probe_lock: self.phase0b_window_probe_thread=None

    def start_phase0b_window_probe(self) -> tuple[bool,str]:
        allowed,reason=self._phase0b_window_probe_gate()
        if not allowed: return False,reason
        with self.phase0b_window_probe_lock:
            if self.phase0b_window_probe_thread and self.phase0b_window_probe_thread.is_alive(): return False,"already_running"
            self.phase0b_window_probe_thread=threading.Thread(target=self.phase0b_window_probe_loop,name="phase0b-windowing-validation-probe",daemon=True); self.phase0b_window_probe_thread.start()
        return True,"started"

    def phase0b_full_key(self, suffix: str) -> str:
        return self.key(f"phase0b_full:v1:{suffix}")

    def _set_phase0b_full_state(self, **updates: Any) -> None:
        with self.phase0b_full_lock:
            self.phase0b_full_state.update(updates)
            self.phase0b_full_state["updated_at"] = iso()
            snap = dict(self.phase0b_full_state)
        if self.redis.configured:
            self.redis.set_json(self.phase0b_full_key("status"), snap)

    def _phase0b_full_gate(self) -> tuple[bool, str]:
        if not (self.redis.configured and self.alpaca.configured):
            return False, "Alpaca and Redis are required"
        pre = self.redis.get_json(self.pre0b_audit_key("report"), None)
        if not pre or pre.get("status") != "COMPLETED":
            return False, "Completed v1.7.6 Pre-Phase0B audit is required"
        if int((pre.get("candidate_totals_after_resolution") or {}).get("recommended_clean") or 0) != 205028:
            return False, "Frozen clean-candidate count must be 205,028"
        probe = self.redis.get_json(self.phase0b_window_probe_key("report"), None)
        if not probe or probe.get("status") != "COMPLETED" or int(probe.get("classification_mismatches") or 0) < 1:
            return False, "Completed v1.7.7 window probe with rejected optimization is required"
        if probe.get("window_optimization_allowed") is not False or probe.get("full_cycle_remains_ground_truth") is not True:
            return False, "Window optimization must be rejected and full cycle frozen as ground truth"
        return True, "allowed"

    @staticmethod
    def _minute_verify_full(rows: list[dict[str, Any]]) -> dict[str, Any]:
        ladders = [5.0,10.0,15.0,20.0,30.0,50.0]
        running_min=None; running_min_ts=None; max_gain=0.0; first={str(int(x)):None for x in ladders}
        first20=None
        for row in sorted(rows,key=lambda x:str(x.get("t") or "")):
            try: lo,hi=float(row.get("l")),float(row.get("h"))
            except (TypeError,ValueError): continue
            if not (math.isfinite(lo) and math.isfinite(hi) and lo>0 and hi>0): continue
            ts=str(row.get("t") or "")
            new_min = running_min is None or lo < running_min
            if new_min: running_min,running_min_ts=lo,ts
            gain=(hi/running_min-1.0)*100.0
            max_gain=max(max_gain,gain)
            for level in ladders:
                k=str(int(level))
                if first[k] is None and gain+1e-12>=level: first[k]=ts
            if first20 is None and gain+1e-12>=20.0:
                first20={"classification":"still_ambiguous" if new_min else "verified","t1":running_min_ts,"t2":ts,"t1_low":running_min,"t2_high":hi,"gain_pct":gain}
        if first20:
            return {**first20,"max_gain_pct":max_gain,"ladder_first_ts":first}
        return {"classification":"failed","t1":running_min_ts,"t2":None,"t1_low":running_min,"t2_high":None,"gain_pct":None,"max_gain_pct":max_gain,"ladder_first_ts":first}

    def _phase0b_clean_candidates_for_session(self, session: str, resolved: dict[str, Any]) -> list[dict[str, Any]]:
        out=[]
        for c in self.redis.get_json(self.historical_census_key(f"candidates:{session}"), []) or []:
            sym=str(c.get("symbol") or "").upper(); cls=resolved.get(sym) or {}
            split=bool((c.get("corporate_action_screen") or {}).get("suspect"))
            if cls.get("bucket") != "metadata_common_like" or split or cls.get("ticker_recycling_risk"): continue
            out.append(c)
        return out

    def _phase0b_fetch_session_rows(self, symbols: list[str], target: date, start: datetime, end: datetime) -> dict[str,list[dict[str,Any]]]:
        merged={s:{} for s in symbols}; batch_size=int(PHASE0B_FULL_SPEC["batch_size"])
        for off in range(0,len(symbols),batch_size):
            batch=symbols[off:off+batch_size]
            sip=self.alpaca.bars(batch,start,end,feed="sip",adjustment="raw",timeframe="1Min")
            boats={} if target < date.fromisoformat(HISTORICAL_CENSUS_SPEC["boats_launch_date"]) else self.alpaca.bars(batch,start,end,feed="boats",adjustment="raw",timeframe="1Min")
            for sym in batch:
                dst=merged[sym]
                for row in boats.get(sym,[]) or []:
                    ts=str(row.get("t") or "")
                    if ts and self._probe_session(ts,target)=="Overnight": dst[ts]={**row,"source":"boats","session":"Overnight"}
                for row in sip.get(sym,[]) or []:
                    ts=str(row.get("t") or "")
                    if ts: dst[ts]={**row,"source":"sip","session":self._probe_session(ts,target)}
        return {s:[d[k] for k in sorted(d)] for s,d in merged.items()}

    def phase0b_full_loop(self) -> None:
        try:
            allowed,reason=self._phase0b_full_gate()
            if not allowed: raise RuntimeError(reason)
            resolved=self.redis.get_json(self.pre0b_audit_key("resolved_symbol_classification"),{}) or {}
            sessions=list(self.redis.get_json(self.historical_census_key("completed_sessions"),[]) or [])
            completed=set(self.redis.get_json(self.phase0b_full_key("completed_sessions"),[]) or [])
            totals=self.redis.get_json(self.phase0b_full_key("totals"),{}) or {"processed":0,"verified":0,"still_ambiguous":0,"failed":0,"sessions":0}
            self.phase0b_full_stop_event.clear()
            self._set_phase0b_full_state(status="RUNNING",phase="VERIFY",message="Full-cycle 1-minute Phase 0B verification; no window optimization",total_sessions=len(sessions),completed_sessions=len(completed),**totals,feature_discovery_allowed=False)
            for session in sessions:
                if session in completed: continue
                if self.phase0b_full_stop_event.is_set():
                    self._set_phase0b_full_state(status="PAUSED",phase="VERIFY",message="Phase 0B paused at a session boundary; resume will not refetch completed sessions",total_sessions=len(sessions),completed_sessions=len(completed),**totals,feature_discovery_allowed=False); return
                candidates=self._phase0b_clean_candidates_for_session(session,resolved)
                target=date.fromisoformat(session); start,end=self._probe_cycle_bounds(target)
                rows_by_symbol=self._phase0b_fetch_session_rows([str(c.get("symbol") or "").upper() for c in candidates],target,start,end) if candidates else {}
                results=[]
                for c in candidates:
                    sym=str(c.get("symbol") or "").upper(); rows=rows_by_symbol.get(sym,[]); vr=self._minute_verify_full(rows)
                    result={"symbol":sym,"target_session":session,"coarse_ambiguous":bool(c.get("same_bar_order_ambiguous_ge20")),"classification":vr["classification"],"t1":vr.get("t1"),"t2":vr.get("t2"),"t1_low":vr.get("t1_low"),"t2_high":vr.get("t2_high"),"gain_pct":vr.get("gain_pct"),"max_gain_pct":vr.get("max_gain_pct"),"ladder_first_ts":vr.get("ladder_first_ts"),"full_cycle_1m_bars":len(rows)}
                    results.append(result); totals["processed"]+=1; totals[vr["classification"]]+=1
                self.redis.set_json(self.phase0b_full_key(f"results:{session}"),results)
                completed.add(session); totals["sessions"]=len(completed)
                self.redis.set_json(self.phase0b_full_key("completed_sessions"),sorted(completed)); self.redis.set_json(self.phase0b_full_key("totals"),totals)
                self._set_phase0b_full_state(status="RUNNING",phase="VERIFY",message=f"Verified full-cycle 1m session {session}",current_session=session,total_sessions=len(sessions),completed_sessions=len(completed),remaining_sessions=len(sessions)-len(completed),last_session_candidates=len(candidates),**totals,feature_discovery_allowed=False)
            if int(totals.get("processed") or 0) != 205028:
                raise RuntimeError(f"Fail-closed: processed {totals.get('processed')} clean cases, expected 205028")
            report={"version":VERSION,"build":BUILD,"verification_id":PHASE0B_FULL_SPEC["verification_id"],"phase0b_sha256":PHASE0B_FULL_SHA256,"status":"COMPLETED","spec":PHASE0B_FULL_SPEC,"totals":totals,"completed_sessions":len(completed),"total_sessions":len(sessions),"window_optimization_used":False,"full_cycle_ground_truth":True,"feature_discovery_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
            self.redis.set_json(self.phase0b_full_key("report"),report)
            self._set_phase0b_full_state(status="COMPLETED",phase="STOP_REVIEW",message="Phase 0B full-cycle verification completed; STOP and review before Feature Discovery",total_sessions=len(sessions),completed_sessions=len(completed),**totals,feature_discovery_allowed=False,stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Phase 0B full-cycle verification failed")
            self._set_phase0b_full_state(status="ERROR",phase="BLOCKED",message="Phase 0B full-cycle verification failed closed",feature_discovery_allowed=False,last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.phase0b_full_lock: self.phase0b_full_thread=None

    def phase0b_dataset_audit_key(self, suffix: str) -> str:
        return self.key(f"phase0b_dataset_audit:v1:{suffix}")

    def _set_phase0b_dataset_audit_state(self, **updates: Any) -> None:
        with self.phase0b_dataset_audit_lock:
            self.phase0b_dataset_audit_state.update(updates)
            self.phase0b_dataset_audit_state["updated_at"] = iso()
            snap = dict(self.phase0b_dataset_audit_state)
        if self.redis.configured:
            self.redis.set_json(self.phase0b_dataset_audit_key("status"), snap)

    def _phase0b_dataset_audit_gate(self) -> tuple[bool, str]:
        if not self.redis.configured:
            return False, "Redis is required"
        report = self.redis.get_json(self.phase0b_full_key("report"), None)
        if not report or report.get("status") != "COMPLETED":
            return False, "Completed Phase 0B full-cycle report is required"
        totals = report.get("totals") or {}
        if int(totals.get("processed") or 0) != 205028 or int(totals.get("verified") or 0) != 169628:
            return False, "Frozen Phase 0B totals do not match the reviewed dataset"
        if report.get("full_cycle_ground_truth") is not True or report.get("window_optimization_used") is not False:
            return False, "Only the reviewed full-cycle ground-truth dataset may be audited"
        return True, "allowed"

    @staticmethod
    def _audit_verified_rate(verified: int, processed: int) -> float:
        return round(100.0 * int(verified) / int(processed), 6) if int(processed) > 0 else 0.0

    @staticmethod
    def _audit_extremes(items: list[dict[str, Any]], limit: int = 25) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Deterministic, opposite orderings. Never infer extremes from insertion order.
        largest = sorted(items, key=lambda x: (-float(x["gain_pct"]), str(x["symbol"]), str(x["target_session"])))[:limit]
        smallest = sorted(items, key=lambda x: (float(x["gain_pct"]), str(x["symbol"]), str(x["target_session"])))[:limit]
        return largest, smallest

    def phase0b_dataset_audit_loop(self) -> None:
        try:
            allowed, reason = self._phase0b_dataset_audit_gate()
            if not allowed: raise RuntimeError(reason)
            full_report = self.redis.get_json(self.phase0b_full_key("report"), {}) or {}
            sessions = list(self.redis.get_json(self.phase0b_full_key("completed_sessions"), []) or [])
            expected_sessions = int(full_report.get("total_sessions") or 0)
            if len(sessions) != expected_sessions or expected_sessions != 1926:
                raise RuntimeError(f"Fail-closed: completed session index has {len(sessions)}, expected 1926")
            self._set_phase0b_dataset_audit_state(status="RUNNING", phase="AUDIT", message="Read-only Phase 0B dataset audit; no Alpaca requests", total_sessions=len(sessions), completed_sessions=0, feature_discovery_allowed=False)
            years={}; phases={"AH":0,"Overnight":0,"Premarket":0,"Regular":0,"Other":0}; symbols={}; processed=0
            classifications={"verified":0,"still_ambiguous":0,"failed":0}; verified_ladders={"20":0,"30":0,"50":0}
            bars_zero=0; bars_positive=0; bars_sum=0; largest=[]; smallest=[]
            for idx, session in enumerate(sessions, 1):
                rows = self.redis.get_json(self.phase0b_full_key(f"results:{session}"), None)
                if rows is None: raise RuntimeError(f"Fail-closed: missing persisted Phase 0B results for {session}")
                y=str(session)[:4]; yr=years.setdefault(y,{"processed":0,"verified":0,"still_ambiguous":0,"failed":0,"verified_rate_pct":0.0})
                for r in rows:
                    cls=str(r.get("classification") or "")
                    if cls not in classifications: raise RuntimeError(f"Unexpected classification {cls!r} in {session}")
                    processed+=1; classifications[cls]+=1; yr["processed"]+=1; yr[cls]+=1
                    n=int(r.get("full_cycle_1m_bars") or 0); bars_sum+=n
                    if n>0: bars_positive+=1
                    else: bars_zero+=1
                    if cls=="verified":
                        sym=str(r.get("symbol") or "").upper(); symbols[sym]=symbols.get(sym,0)+1
                        g=float(r.get("gain_pct") or 0.0)
                        for level in (20,30,50):
                            if g+1e-12>=level: verified_ladders[str(level)]+=1
                        phase=self._probe_session(str(r.get("t2") or ""), date.fromisoformat(session)) if r.get("t2") else "Other"
                        phases[phase if phase in phases else "Other"]+=1
                        item={"symbol":sym,"target_session":session,"gain_pct":g,"t1":r.get("t1"),"t2":r.get("t2"),"bars":n}
                        largest.append(item); smallest.append(item)
                if idx % 100 == 0 or idx == len(sessions):
                    self._set_phase0b_dataset_audit_state(status="RUNNING",phase="AUDIT",message=f"Audited persisted Phase 0B session {session}",total_sessions=len(sessions),completed_sessions=idx,processed=processed,feature_discovery_allowed=False)
            for yr in years.values(): yr["verified_rate_pct"] = self._audit_verified_rate(yr["verified"], yr["processed"])
            largest, smallest = self._audit_extremes(largest, 25)
            repeat_hist={};
            for count in symbols.values(): repeat_hist[str(count)]=repeat_hist.get(str(count),0)+1
            top_repeat=[{"symbol":s,"verified_events":c} for s,c in sorted(symbols.items(),key=lambda kv:(-kv[1],kv[0]))[:25]]
            expected=full_report.get("totals") or {}; checks={
                "processed_matches": processed==int(expected.get("processed") or -1)==205028,
                "verified_matches": classifications["verified"]==int(expected.get("verified") or -1)==169628,
                "ambiguous_matches": classifications["still_ambiguous"]==int(expected.get("still_ambiguous") or -1),
                "failed_matches": classifications["failed"]==int(expected.get("failed") or -1),
                "classification_partition_matches": sum(classifications.values())==processed,
                "year_partition_matches": sum(v["processed"] for v in years.values())==processed,
                "year_classification_partitions_match": all(v["verified"] + v["still_ambiguous"] + v["failed"] == v["processed"] for v in years.values()),
                "year_verified_rates_exact": all(v["verified_rate_pct"] == self._audit_verified_rate(v["verified"], v["processed"]) for v in years.values()),
                "verified_phase_partition_matches": sum(phases.values())==classifications["verified"],
                "verified_ladder_monotonic": verified_ladders["20"] >= verified_ladders["30"] >= verified_ladders["50"] >= 0,
                "verified_20_ladder_matches_verified": verified_ladders["20"] == classifications["verified"],
                "largest_sorted_desc": all(largest[i]["gain_pct"] >= largest[i+1]["gain_pct"] for i in range(len(largest)-1)),
                "smallest_sorted_asc": all(smallest[i]["gain_pct"] <= smallest[i+1]["gain_pct"] for i in range(len(smallest)-1)),
                "extreme_lengths_valid": len(largest) == min(25, classifications["verified"]) and len(smallest) == min(25, classifications["verified"]),
                "bar_coverage_partition_matches": bars_zero+bars_positive==processed,
                "all_sessions_present": len(sessions)==1926,
                "no_alpaca_requests_by_design": True,
            }
            passed=all(checks.values())
            report={"version":VERSION,"build":BUILD,"audit_id":PHASE0B_DATASET_AUDIT_SPEC["audit_id"],"audit_sha256":PHASE0B_DATASET_AUDIT_SHA256,"status":"COMPLETED" if passed else "FAILED","spec":PHASE0B_DATASET_AUDIT_SPEC,"source_phase0b_sha256":full_report.get("phase0b_sha256"),"totals":{"processed":processed,**classifications,"unique_verified_symbols":len(symbols)},"by_year":years,"verified_gain_ladders":verified_ladders,"verified_first_20_phase":phases,"repeat_verified_events":{"histogram":repeat_hist,"top_symbols":top_repeat},"bar_coverage":{"cases_with_positive_1m_bars":bars_positive,"cases_with_zero_1m_bars":bars_zero,"mean_1m_bars_per_case":round(bars_sum/processed,4) if processed else 0.0},"extreme_verified_events":{"largest_25":largest,"smallest_25":smallest},"integrity_checks":checks,"integrity_passed":passed,"feature_discovery_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
            self.redis.set_json(self.phase0b_dataset_audit_key("report"),report)
            self._set_phase0b_dataset_audit_state(status="COMPLETED" if passed else "ERROR",phase="STOP_REVIEW" if passed else "BLOCKED",message="Phase 0B dataset audit completed; STOP and review before Feature Discovery" if passed else "Phase 0B dataset audit failed integrity checks",processed=processed,total_sessions=len(sessions),completed_sessions=len(sessions),integrity_passed=passed,feature_discovery_allowed=False,stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Phase 0B dataset audit failed")
            self._set_phase0b_dataset_audit_state(status="ERROR",phase="BLOCKED",message="Phase 0B dataset audit failed closed",last_error=f"{type(exc).__name__}: {exc}",feature_discovery_allowed=False)
        finally:
            with self.phase0b_dataset_audit_lock: self.phase0b_dataset_audit_thread=None

    def start_phase0b_dataset_audit(self) -> tuple[bool,str]:
        allowed,reason=self._phase0b_dataset_audit_gate()
        if not allowed:return False,reason
        with self.phase0b_dataset_audit_lock:
            if self.phase0b_dataset_audit_thread and self.phase0b_dataset_audit_thread.is_alive(): return False,"already_running"
            self.phase0b_dataset_audit_thread=threading.Thread(target=self.phase0b_dataset_audit_loop,name="phase0b-dataset-audit",daemon=True); self.phase0b_dataset_audit_thread.start()
        return True,"started"

    def feature_discovery_key(self, suffix: str) -> str:
        return self.key(f"feature_discovery:v1:{suffix}")

    def _set_feature_discovery_state(self, **updates: Any) -> None:
        with self.feature_discovery_lock:
            self.feature_discovery_state.update(updates); self.feature_discovery_state["updated_at"] = iso(); snap=dict(self.feature_discovery_state)
        if self.redis.configured: self.redis.set_json(self.feature_discovery_key("status"), snap)

    def _feature_discovery_gate(self) -> tuple[bool,str]:
        if not (self.redis.configured and self.alpaca.configured): return False,"Redis and Alpaca are required"
        audit=self.redis.get_json(self.phase0b_dataset_audit_key("report"),None)
        if not audit or audit.get("integrity_passed") is not True: return False,"Hardened Phase 0B Dataset Audit must pass"
        t=audit.get("totals") or {}
        if int(t.get("processed") or 0)!=205028 or int(t.get("verified") or 0)!=169628: return False,"Frozen Phase 0B totals do not match"
        if str(audit.get("audit_id")) != FEATURE_DISCOVERY_PROTOCOL_SPEC["source_gate"]["source_audit_id"]: return False,"Frozen source audit id mismatch"
        return True,"allowed"

    @staticmethod
    def _fd_price_band(price: Any) -> str:
        try: x=float(price)
        except (TypeError,ValueError): return "unknown"
        if not math.isfinite(x) or x<=0:return "unknown"
        return str(int(math.floor(math.log(x,2))))

    @staticmethod
    def _fd_phase(ts: str, target: date) -> str:
        return IndependentPriorityRadar._probe_session(ts,target) if ts else "Other"

    @staticmethod
    def _fd_coarse_cutoff(c: dict[str,Any]) -> str | None:
        lf=c.get("ladder_first_ts") or {}
        return lf.get("20") or c.get("max_high_ts")

    def _fd_fetch_session_rows(self, symbols: list[str], target: date, start: datetime, end: datetime) -> dict[str,list[dict[str,Any]]]:
        merged={sym:{} for sym in symbols}; batch_size=200
        for off in range(0,len(symbols),batch_size):
            batch=symbols[off:off+batch_size]
            sip=self.alpaca.bars(batch,start,end,feed="sip",adjustment="raw",timeframe="5Min")
            boats={} if target < date.fromisoformat(HISTORICAL_CENSUS_SPEC["boats_launch_date"]) else self.alpaca.bars(batch,start,end,feed="boats",adjustment="raw",timeframe="5Min")
            for sym in batch:
                dst=merged[sym]
                for row in boats.get(sym,[]) or []:
                    ts=str(row.get("t") or "")
                    if ts and self._probe_session(ts,target)=="Overnight": dst[ts]={**row,"source":"boats","session":"Overnight"}
                for row in sip.get(sym,[]) or []:
                    ts=str(row.get("t") or "")
                    if ts: dst[ts]={**row,"source":"sip","session":self._probe_session(ts,target)}
        return {sym:[d[k] for k in sorted(d)] for sym,d in merged.items()}

    @staticmethod
    def _fd_features(rows: list[dict[str,Any]], cutoff: datetime) -> dict[str,float] | None:
        rr=[]
        for r in rows:
            try:
                ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00")); o=float(r.get("o")); h=float(r.get("h")); l=float(r.get("l")); c=float(r.get("c")); v=float(r.get("v") or 0)
            except Exception: continue
            if ts >= cutoff or min(o,h,l,c)<=0: continue
            rr.append((ts,o,h,l,c,max(v,0.0)))
        rr.sort(key=lambda x:x[0])
        if len(rr)<4:return None
        last=rr[-1]; closes=np.array([x[4] for x in rr],dtype=float); highs=np.array([x[2] for x in rr],dtype=float); lows=np.array([x[3] for x in rr],dtype=float); vols=np.array([x[5] for x in rr],dtype=float)
        def ret(n):
            if len(closes)<=n:return float("nan")
            return float((closes[-1]/closes[-1-n]-1)*100)
        n12=min(12,len(rr)); n6=min(6,len(rr)); prev12=vols[-24:-12] if len(vols)>=24 else vols[:-n12]
        dv=closes*vols; typical=(highs+lows+closes)/3; denom=float(vols[-n12:].sum()); vwap=float((typical[-n12:]*vols[-n12:]).sum()/denom) if denom>0 else float(np.mean(typical[-n12:]))
        ranges=(highs-lows)/closes*100
        prior_high=float(np.max(highs[:-1])) if len(highs)>1 else highs[-1]
        recent_low=float(np.min(lows[-n12:])); recent_high=float(np.max(highs[-n12:]))
        span=max(recent_high-recent_low,1e-12)
        return {
            "ret_5m":ret(1),"ret_15m":ret(3),"ret_30m":ret(6),"ret_60m":ret(12),
            "accel_15_vs_60":ret(3)-(ret(12)/4 if math.isfinite(ret(12)) else 0.0),
            "volume_60m":float(vols[-n12:].sum()),"dollar_volume_60m":float(dv[-n12:].sum()),
            "volume_ratio_prev60":float(vols[-n12:].mean()/max(float(prev12.mean()) if len(prev12) else 1.0,1.0)),
            "range_pct_30m":float(np.mean(ranges[-n6:])),"range_expansion":float(np.mean(ranges[-n6:])/max(float(np.mean(ranges[-2*n6:-n6])) if len(ranges)>=2*n6 else float(np.mean(ranges)),1e-9)),
            "vwap_distance_pct":float((closes[-1]/vwap-1)*100) if vwap>0 else float("nan"),
            "close_position_60m":float((closes[-1]-recent_low)/span),
            "distance_prior_high_pct":float((closes[-1]/prior_high-1)*100) if prior_high>0 else float("nan"),
            "drawdown_from_60m_high_pct":float((closes[-1]/recent_high-1)*100),
            "higher_low_30m":float(1.0 if len(lows)>=6 and np.min(lows[-3:])>np.min(lows[-6:-3]) else 0.0),
            "bars_available":float(len(rr)),
        }

    @staticmethod
    def _fd_bh(rows: list[dict[str,Any]]) -> None:
        fam=defaultdict(list)
        for i,r in enumerate(rows):
            p=r.get("p_value")
            if isinstance(p,(int,float)) and math.isfinite(p): fam[r["family"]].append((float(p),i))
        for arr in fam.values():
            arr.sort(); m=len(arr); q=[1.0]*m; running=1.0
            for j in range(m-1,-1,-1):
                p,i=arr[j]; running=min(running,p*m/(j+1)); q[j]=running
            for (_,i),qq in zip(arr,q): rows[i]["fdr_q"]=min(1.0,qq)

    @staticmethod
    def _fd_family(name:str)->str:
        if name.startswith("ret_") or name.startswith("accel"): return "price/return path and acceleration"
        if "volume" in name: return "volume and dollar-volume participation"
        if "range" in name: return "range/volatility expansion and compression"
        if "vwap" in name or "close_position" in name or "prior_high" in name:return "VWAP/location and close-position structure"
        if "drawdown" in name or "higher_low" in name:return "persistence/recovery/pullback asymmetry"
        return "liquidity/spread where historically available"

    @staticmethod
    def _fd_symbol_cluster_bootstrap(pos_by_symbol:dict[str,float], hard_by_symbol:dict[str,float], seed_key:str, reps:int=2000)->dict[str,Any]:
        """Deterministic symbol-cluster multiplier bootstrap using one Rademacher weight per symbol cluster."""
        symbols=sorted(set(pos_by_symbol) | set(hard_by_symbol))
        if len(symbols)<2 or not pos_by_symbol or not hard_by_symbol:
            return {"method":"symbol_cluster_multiplier_bootstrap","replicates":0,"se":None,"ci95":[None,None],"p_value":1.0,"clusters":len(symbols)}
        mp=float(np.mean(list(pos_by_symbol.values()))); mh=float(np.mean(list(hard_by_symbol.values()))); obs=mp-mh
        np_=float(len(pos_by_symbol)); nh_=float(len(hard_by_symbol))
        # Cluster influence for the difference in equal-symbol means.  If a symbol occurs
        # in both classes, both contributions remain inside the same cluster and therefore
        # receive the same multiplier -- this is the key cluster-preserving property.
        psi=np.asarray([((pos_by_symbol[x]-mp)/np_ if x in pos_by_symbol else 0.0)-((hard_by_symbol[x]-mh)/nh_ if x in hard_by_symbol else 0.0) for x in symbols],dtype=float)
        seed=int(hashlib.sha256(seed_key.encode("utf-8")).hexdigest()[:16],16) & 0xFFFFFFFF
        rng=np.random.default_rng(seed); deltas=[]; left=int(reps); batch=200
        while left>0:
            b=min(batch,left); w=rng.integers(0,2,size=(b,len(symbols)),dtype=np.int8).astype(float)*2.0-1.0
            deltas.extend((w @ psi).tolist()); left-=b
        a=np.asarray(deltas,dtype=float); se=float(a.std(ddof=1)) if len(a)>1 else None
        qlo,qhi=(float(x) for x in np.quantile(a,[0.025,0.975])); ci=[obs-qhi,obs-qlo]
        pv=(float(np.sum(np.abs(a)>=abs(obs)))+1.0)/(len(a)+1.0)
        return {"method":"symbol_cluster_multiplier_bootstrap","replicates":len(a),"se":se,"ci95":ci,"p_value":min(1.0,pv),"clusters":len(symbols),"observed_difference":obs}

    def _fd_summarize(self, observations:list[dict[str,Any]], min_events:int, min_symbols:int)->list[dict[str,Any]]:
        # equal-symbol weights within each class; cluster-aware uncertainty from symbol means
        out=[]; anchors=FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"]
        feature_names=sorted({k for o in observations for a in o.get("anchors",{}).values() for k in a})
        for anchor in anchors:
            ak=str(anchor)
            for fn in feature_names:
                bycls={"positive":defaultdict(list),"hard_negative":defaultdict(list),"random_control":defaultdict(list)}
                years=defaultdict(set); counts=defaultdict(int)
                for o in observations:
                    val=(o.get("anchors",{}).get(ak) or {}).get(fn)
                    if not isinstance(val,(int,float)) or not math.isfinite(val):continue
                    cls=o["class"]; bycls[cls][o["symbol"]].append(float(val)); years[cls].add(int(o["year"])); counts[cls]+=1
                def symvals(cls): return np.array([float(np.mean(v)) for v in bycls[cls].values()],dtype=float)
                p=symvals("positive"); h=symvals("hard_negative"); r=symvals("random_control")
                if len(p)==0 or len(h)==0:continue
                mp,mh=float(p.mean()),float(h.mean()); pooled=math.sqrt((float(p.var(ddof=1)) if len(p)>1 else 0)+(float(h.var(ddof=1)) if len(h)>1 else 0))/math.sqrt(2) if len(p)+len(h)>2 else 0
                effect=(mp-mh)/pooled if pooled>1e-12 else 0.0
                se=math.sqrt((float(p.var(ddof=1))/len(p) if len(p)>1 else 0)+(float(h.var(ddof=1))/len(h) if len(h)>1 else 0))
                z=(mp-mh)/se if se>1e-12 else 0.0; pv=math.erfc(abs(z)/math.sqrt(2)) if se>1e-12 else 1.0
                support=counts["positive"]>=min_events and len(p)>=min_symbols and len(years["positive"])>=4
                out.append({"anchor_minutes":anchor,"feature":fn,"family":self._fd_family(fn),"positive_events":counts["positive"],"positive_symbols":len(p),"hard_negative_events":counts["hard_negative"],"hard_negative_symbols":len(h),"random_events":counts["random_control"],"positive_mean_symbol_weighted":mp,"hard_negative_mean_symbol_weighted":mh,"random_mean_symbol_weighted":float(r.mean()) if len(r) else None,"standardized_effect_pos_vs_hard":effect,"p_value":pv,"support_passed":support,"positive_year_support":sorted(years["positive"]),"fdr_q":None})
        self._fd_bh(out); return out

    def feature_discovery_loop(self)->None:
        try:
            allowed,reason=self._feature_discovery_gate()
            if not allowed: raise RuntimeError(reason)
            sessions=[s for s in (self.redis.get_json(self.phase0b_full_key("completed_sessions"),[]) or []) if 2019<=int(str(s)[:4])<=2024]
            # Freeze min-N before any feature values are fetched/calculated.
            pos_count=0; pos_syms=set()
            for sess in sessions:
                for r in self.redis.get_json(self.phase0b_full_key(f"results:{sess}"),[]) or []:
                    if r.get("classification")=="verified": pos_count+=1; pos_syms.add(str(r.get("symbol") or ""))
            min_events=max(500,int(math.ceil(pos_count*0.005))); min_symbols=max(100,int(math.ceil(len(pos_syms)*0.03)))
            existing_min=self.redis.get_json(self.feature_discovery_key("min_n_frozen"),None)
            if isinstance(existing_min,dict) and int(existing_min.get("eligible_discovery_positive_events") or -1)==pos_count and int(existing_min.get("eligible_discovery_positive_symbols") or -1)==len(pos_syms) and int(existing_min.get("min_positive_events") or -1)==min_events and int(existing_min.get("min_positive_symbols") or -1)==min_symbols:
                minspec=existing_min  # preserve the original pre-effect freeze timestamp across restarts
            else:
                minspec={"eligible_discovery_positive_events":pos_count,"eligible_discovery_positive_symbols":len(pos_syms),"min_positive_events":min_events,"min_positive_symbols":min_symbols,"min_years":4,"formula":FEATURE_DISCOVERY_EXEC_SPEC["min_n_formula"],"frozen_at":iso()}
                self.redis.set_json(self.feature_discovery_key("min_n_frozen"),minspec)
            completed=set(self.redis.get_json(self.feature_discovery_key("completed_sessions"),[]) or [])
            self.feature_discovery_stop_event.clear(); total_obs=int(self.redis.get_json(self.feature_discovery_key("observation_count"),0) or 0)
            self._set_feature_discovery_state(status="RUNNING",phase="DISCOVERY_EXTRACT",message="Discovery 2019-2024 only; 2025/2026 locked",total_sessions=len(sessions),completed_sessions=len(completed),observations=total_obs,min_n=minspec,validation_2025_opened=False,holdout_2026_opened=False)
            for si,sess in enumerate(sessions,1):
                if sess in completed:continue
                if self.feature_discovery_stop_event.is_set():
                    self._set_feature_discovery_state(status="PAUSED",phase="DISCOVERY_EXTRACT",message="Paused at session boundary; resume is safe",completed_sessions=len(completed),total_sessions=len(sessions),observations=total_obs,validation_2025_opened=False,holdout_2026_opened=False);return
                target=date.fromisoformat(sess); p0=self.redis.get_json(self.phase0b_full_key(f"results:{sess}"),[]) or []; coarse={str(c.get("symbol") or "").upper():c for c in (self.redis.get_json(self.historical_census_key(f"candidates:{sess}"),[]) or [])}
                positives=[r for r in p0 if r.get("classification")=="verified"]; failed=[r for r in p0 if r.get("classification")=="failed"]
                # Context index uses only frozen labels/context, never predictive feature values.
                fctx=[]
                for r in failed:
                    sym=str(r.get("symbol") or "").upper(); c=coarse.get(sym,{}) ; cut=self._fd_coarse_cutoff(c)
                    if not cut:continue
                    fctx.append((r,sym,cut,self._fd_phase(cut,target),self._fd_price_band(r.get("t1_low"))))
                selected=[]
                for pi,p in enumerate(positives):
                    psym=str(p.get("symbol") or "").upper(); phase=self._fd_phase(str(p.get("t2") or ""),target); pb=self._fd_price_band(p.get("t1_low")); exact=[x for x in fctx if x[3]==phase and x[4]==pb]; pool=exact or [x for x in fctx if x[3]==phase] or fctx
                    hard=[]
                    if pool:
                        base=int(hashlib.sha256(f"{sess}|{psym}|{p.get('t2')}".encode()).hexdigest()[:12],16)
                        for j in range(min(3,len(pool))): hard.append(pool[(base+j*7919)%len(pool)])
                        rnd=pool[(base+104729)%len(pool)]
                    else: rnd=None
                    match=hashlib.sha256(f"{sess}|{psym}|{p.get('t2')}|{pi}".encode()).hexdigest()[:20]
                    selected.append(("positive",p,psym,str(p.get("t2")),match))
                    for x in hard:selected.append(("hard_negative",x[0],x[1],x[2],match))
                    if rnd:selected.append(("random_control",rnd[0],rnd[1],rnd[2],match))
                syms=sorted({x[2] for x in selected}); start,end=self._probe_cycle_bounds(target); rows_by=self._fd_fetch_session_rows(syms,target,start,end) if syms else {}
                obs=[]
                for cls,r,sym,cut,match in selected:
                    try: cutoff=datetime.fromisoformat(str(cut).replace("Z","+00:00"))
                    except Exception:continue
                    anchors={}
                    for off in FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"]:
                        f=self._fd_features(rows_by.get(sym,[]),cutoff-timedelta(minutes=off))
                        if f:anchors[str(off)]=f
                    if not anchors:continue
                    obs.append({"class":cls,"symbol":sym,"target_session":sess,"year":int(sess[:4]),"phase":self._fd_phase(cut,target),"price_band":self._fd_price_band(r.get("t1_low")),"match_id":match,"cutoff":cut,"anchors":anchors,"strength_30":bool((r.get("ladder_first_ts") or {}).get("30")) if cls=="positive" else False,"strength_50":bool((r.get("ladder_first_ts") or {}).get("50")) if cls=="positive" else False})
                # Frozen contextual coverage check: controls with grossly different pre-cutoff bar availability are omitted.
                grouped=defaultdict(list)
                for o in obs: grouped[o["match_id"]].append(o)
                filtered=[]
                for grp in grouped.values():
                    pos=next((o for o in grp if o["class"]=="positive"),None)
                    if not pos: continue
                    pa=(pos.get("anchors",{}).get("5") or {}).get("bars_available")
                    filtered.append(pos)
                    for o in grp:
                        if o is pos: continue
                        ca=(o.get("anchors",{}).get("5") or {}).get("bars_available")
                        if isinstance(pa,(int,float)) and isinstance(ca,(int,float)) and pa>0 and 0.5 <= ca/pa <= 2.0: filtered.append(o)
                obs=filtered
                self.redis.set_json(self.feature_discovery_key(f"observations:{sess}"),obs); total_obs+=len(obs); completed.add(sess); self.redis.set_json(self.feature_discovery_key("completed_sessions"),sorted(completed)); self.redis.set_json(self.feature_discovery_key("observation_count"),total_obs)
                self._set_feature_discovery_state(status="RUNNING",phase="DISCOVERY_EXTRACT",message=f"Feature Discovery extracted session {sess}",current_session=sess,total_sessions=len(sessions),completed_sessions=len(completed),remaining_sessions=len(sessions)-len(completed),last_session_observations=len(obs),observations=total_obs,min_n=minspec,validation_2025_opened=False,holdout_2026_opened=False)
            # Finalization is deliberately restart-safe and streaming.  Never materialize all
            # observations at once: Render may restart a small instance under that memory spike.
            # A restart may redo FINALIZE from persisted per-session observations, but it will
            # never re-fetch/re-extract completed market sessions.
            self._set_feature_discovery_state(status="RUNNING",phase="DISCOVERY_FINALIZE",message="Finalizing persisted Discovery observations; 2025/2026 remain locked",completed_sessions=len(completed),total_sessions=len(sessions),remaining_sessions=0,observations=total_obs,min_n=minspec,finalize_sessions_scanned=0,validation_2025_opened=False,holdout_2026_opened=False)
            agg=defaultdict(lambda: {"sum":0.0,"n":0})
            years=defaultdict(set); event_counts=defaultdict(int); class_counts=__import__('collections').Counter(); final_obs=0
            feature_names=set()
            for fi,sess in enumerate(sessions,1):
                sobs=self.redis.get_json(self.feature_discovery_key(f"observations:{sess}"),[]) or []
                final_obs += len(sobs)
                for o in sobs:
                    cls=str(o.get("class") or ""); sym=str(o.get("symbol") or ""); yr=int(o.get("year") or str(sess)[:4])
                    class_counts[cls]+=1
                    for ak,vals in (o.get("anchors") or {}).items():
                        if not isinstance(vals,dict): continue
                        for fn,val in vals.items():
                            if not isinstance(val,(int,float)) or not math.isfinite(val): continue
                            feature_names.add(fn); key=(str(ak),fn,cls,sym); agg[key]["sum"]+=float(val); agg[key]["n"]+=1
                            event_counts[(str(ak),fn,cls)]+=1; years[(str(ak),fn,cls)].add(yr)
                if fi==1 or fi%25==0 or fi==len(sessions):
                    self._set_feature_discovery_state(status="RUNNING",phase="DISCOVERY_FINALIZE",message=f"Finalizing persisted observations: {fi}/{len(sessions)} sessions",completed_sessions=len(completed),total_sessions=len(sessions),remaining_sessions=0,observations=total_obs,min_n=minspec,finalize_sessions_scanned=fi,finalize_total_sessions=len(sessions),validation_2025_opened=False,holdout_2026_opened=False)
            stats=[]
            for anchor in FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"]:
                ak=str(anchor)
                for fn in sorted(feature_names):
                    bycls={"positive":{},"hard_negative":{},"random_control":{}}
                    for (a,f,cls,sym),v in agg.items():
                        if a==ak and f==fn and cls in bycls and v["n"]: bycls[cls][sym]=v["sum"]/v["n"]
                    p=np.array(list(bycls["positive"].values()),dtype=float); h=np.array(list(bycls["hard_negative"].values()),dtype=float); r=np.array(list(bycls["random_control"].values()),dtype=float)
                    if len(p)==0 or len(h)==0: continue
                    mp,mh=float(p.mean()),float(h.mean()); pooled=math.sqrt((float(p.var(ddof=1)) if len(p)>1 else 0)+(float(h.var(ddof=1)) if len(h)>1 else 0))/math.sqrt(2) if len(p)+len(h)>2 else 0
                    effect=(mp-mh)/pooled if pooled>1e-12 else 0.0
                    boot=self._fd_symbol_cluster_bootstrap(bycls["positive"],bycls["hard_negative"],f"{FEATURE_DISCOVERY_EXEC_SPEC['run_id']}|{ak}|{fn}",2000); pv=float(boot["p_value"])
                    pc=event_counts[(ak,fn,"positive")]; pys=years[(ak,fn,"positive")]
                    support=pc>=min_events and len(p)>=min_symbols and len(pys)>=4
                    stats.append({"anchor_minutes":anchor,"feature":fn,"family":self._fd_family(fn),"positive_events":pc,"positive_symbols":len(p),"hard_negative_events":event_counts[(ak,fn,"hard_negative")],"hard_negative_symbols":len(h),"random_events":event_counts[(ak,fn,"random_control")],"positive_mean_symbol_weighted":mp,"hard_negative_mean_symbol_weighted":mh,"random_mean_symbol_weighted":float(r.mean()) if len(r) else None,"standardized_effect_pos_vs_hard":effect,"inference":boot,"p_value":pv,"support_passed":support,"positive_year_support":sorted(pys),"fdr_q":None})
            self._fd_bh(stats)
            promoted=[r for r in stats if r.get("support_passed") and isinstance(r.get("fdr_q"),(int,float)) and r["fdr_q"]<=0.05 and abs(float(r.get("standardized_effect_pos_vs_hard") or 0))>=0.10]
            report={"version":VERSION,"build":BUILD,"run_id":FEATURE_DISCOVERY_EXEC_SPEC["run_id"],"execution_sha256":FEATURE_DISCOVERY_EXEC_SHA256,"protocol_sha256":FEATURE_DISCOVERY_PROTOCOL_SHA256,"status":"COMPLETED","phase":"STOP_REVIEW","scope":"2019-2024 Discovery only","min_n_frozen":minspec,"sessions":len(sessions),"observations":final_obs,"class_counts":dict(class_counts),"feature_tests":stats,"promotion_candidates_discovery_only":promoted,"promotion_candidate_count":len(promoted),"finalization_mode":"restart_safe_streaming_symbol_cluster_bootstrap_from_persisted_session_observations","validation_2025_opened":False,"holdout_2026_opened":False,"validation_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
            self.redis.set_json(self.feature_discovery_key("report"),report); self._set_feature_discovery_state(status="COMPLETED",phase="STOP_REVIEW",message="Discovery 2019-2024 completed; STOP and review before any 2025 validation",completed_sessions=len(sessions),total_sessions=len(sessions),remaining_sessions=0,observations=final_obs,promotion_candidate_count=len(promoted),finalize_sessions_scanned=len(sessions),validation_2025_opened=False,holdout_2026_opened=False,stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Feature Discovery failed"); self._set_feature_discovery_state(status="ERROR",phase="BLOCKED",message="Feature Discovery failed closed",last_error=f"{type(exc).__name__}: {exc}",validation_2025_opened=False,holdout_2026_opened=False)
        finally:
            with self.feature_discovery_lock:self.feature_discovery_thread=None

    def start_feature_discovery(self)->tuple[bool,str]:
        allowed,reason=self._feature_discovery_gate()
        if not allowed:return False,reason
        with self.feature_discovery_lock:
            if self.feature_discovery_thread and self.feature_discovery_thread.is_alive():return False,"already_running"
            self.feature_discovery_thread=threading.Thread(target=self.feature_discovery_loop,name="feature-discovery-2019-2024",daemon=True); self.feature_discovery_thread.start()
        return True,"started"

    def feature_discovery_audit_key(self, suffix: str) -> str:
        return self.key(f"feature_discovery_audit:v1:{suffix}")

    def _set_feature_discovery_audit_state(self, **updates: Any) -> None:
        with self.feature_discovery_audit_lock:
            self.feature_discovery_audit_state.update(updates); self.feature_discovery_audit_state["updated_at"] = iso(); snap=dict(self.feature_discovery_audit_state)
        if self.redis.configured: self.redis.set_json(self.feature_discovery_audit_key("status"), snap)

    def _feature_discovery_audit_gate(self) -> tuple[bool,str]:
        if not self.redis.configured: return False,"Redis is required"
        report=self.redis.get_json(self.feature_discovery_key("report"),None)
        if not isinstance(report,dict) or report.get("status")!="COMPLETED" or report.get("phase")!="STOP_REVIEW": return False,"Completed Discovery STOP_REVIEW report is required"
        if str(report.get("run_id"))!=FEATURE_DISCOVERY_EXEC_SPEC["run_id"]: return False,"Discovery run id mismatch"
        if str(report.get("protocol_sha256"))!=FEATURE_DISCOVERY_PROTOCOL_SHA256: return False,"Discovery protocol hash mismatch"
        if str(report.get("execution_sha256"))!=FEATURE_DISCOVERY_EXEC_SHA256: return False,"Discovery execution hash mismatch"
        if report.get("validation_2025_opened") is not False or report.get("holdout_2026_opened") is not False: return False,"Validation/Holdout contamination flag"
        integ=FEATURE_DISCOVERY_AUDIT_SPEC["integrity"]
        if int(report.get("sessions") or 0)!=integ["expected_sessions"] or int(report.get("observations") or 0)!=integ["expected_observations"]: return False,"Discovery session/observation totals mismatch"
        if dict(report.get("class_counts") or {})!=integ["expected_class_counts"]: return False,"Discovery class counts mismatch"
        mn=report.get("min_n_frozen") or {}; exp=integ["expected_min_n"]
        for k,v in exp.items():
            if mn.get(k)!=v: return False,f"Frozen min_n mismatch: {k}"
        completed=self.redis.get_json(self.feature_discovery_key("completed_sessions"),[]) or []
        disc=[str(x) for x in completed if 2019<=int(str(x)[:4])<=2024]
        if len(disc)!=integ["expected_sessions"] or any(int(x[:4])>2024 for x in disc): return False,"Persisted Discovery session set mismatch"
        return True,"allowed"

    @staticmethod
    def _fd_effect_from_symbol_values(pos: dict[str,list[float]], hard: dict[str,list[float]]) -> dict[str,Any]:
        p=np.asarray([float(np.mean(v)) for v in pos.values() if v],dtype=float); h=np.asarray([float(np.mean(v)) for v in hard.values() if v],dtype=float)
        if len(p)==0 or len(h)==0:return {"effect":None,"positive_mean":None,"hard_negative_mean":None,"positive_symbols":len(p),"hard_negative_symbols":len(h)}
        mp,mh=float(p.mean()),float(h.mean()); pooled=math.sqrt(((float(p.var(ddof=1)) if len(p)>1 else 0.0)+(float(h.var(ddof=1)) if len(h)>1 else 0.0))/2.0)
        return {"effect":((mp-mh)/pooled if pooled>1e-12 else 0.0),"positive_mean":mp,"hard_negative_mean":mh,"positive_symbols":len(p),"hard_negative_symbols":len(h)}

    def feature_discovery_audit_loop(self)->None:
        try:
            allowed,reason=self._feature_discovery_audit_gate()
            if not allowed: raise RuntimeError(reason)
            source=self.redis.get_json(self.feature_discovery_key("report"),{}) or {}
            sessions=sorted(str(x) for x in (self.redis.get_json(self.feature_discovery_key("completed_sessions"),[]) or []) if 2019<=int(str(x)[:4])<=2024)
            source_tests={(int(x["anchor_minutes"]),str(x["feature"])):x for x in (source.get("feature_tests") or [])}
            diagnostics=[]; total_observations=0; class_counts={}
            self._set_feature_discovery_audit_state(status="RUNNING",phase="AUDIT_SCAN",message="Read-only stability audit from persisted 2019-2024 observations; 2025/2026 locked",anchor_index=0,total_anchors=len(FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"]),validation_2025_opened=False,holdout_2026_opened=False)
            # Memory-bounded design: process one frozen anchor at a time. This intentionally
            # rereads persisted Redis observations six times rather than holding multi-million
            # subgroup cells in RAM on a small Render instance. No Alpaca request is made.
            for ai,a in enumerate(FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"],1):
                ak=str(a)
                year_agg=defaultdict(lambda:defaultdict(lambda:defaultdict(lambda:{"sum":0.0,"n":0})))
                phase_agg=defaultdict(lambda:defaultdict(lambda:defaultdict(lambda:{"sum":0.0,"n":0})))
                ladder_agg=defaultdict(lambda:defaultdict(lambda:defaultdict(lambda:{"sum":0.0,"n":0})))
                local_counts=__import__('collections').Counter(); scanned=0
                for si,sess in enumerate(sessions,1):
                    sobs=self.redis.get_json(self.feature_discovery_key(f"observations:{sess}"),[]) or []
                    if ai==1:
                        total_observations+=len(sobs)
                        for o in sobs: local_counts[str(o.get("class") or "")]+=1
                    for o in sobs:
                        cls=str(o.get("class") or ""); sym=str(o.get("symbol") or ""); yr=int(o.get("year") or sess[:4]); ph=str(o.get("phase") or "Other"); vals=(o.get("anchors") or {}).get(ak) or {}
                        if not isinstance(vals,dict):continue
                        for fn,val in vals.items():
                            if not isinstance(val,(int,float)) or not math.isfinite(val):continue
                            x=float(val)
                            yc=year_agg[(fn,yr)][cls][sym]; yc["sum"]+=x; yc["n"]+=1
                            pc=phase_agg[(fn,ph)][cls][sym]; pc["sum"]+=x; pc["n"]+=1
                            if cls=="positive":
                                lc=ladder_agg[(fn,"20")][cls][sym]; lc["sum"]+=x; lc["n"]+=1
                                if o.get("strength_30"):
                                    lc=ladder_agg[(fn,"30")][cls][sym]; lc["sum"]+=x; lc["n"]+=1
                                if o.get("strength_50"):
                                    lc=ladder_agg[(fn,"50")][cls][sym]; lc["sum"]+=x; lc["n"]+=1
                    scanned=si
                    if si==1 or si%100==0 or si==len(sessions): self._set_feature_discovery_audit_state(status="RUNNING",phase="AUDIT_SCAN",message=f"Audit anchor {a}m: {si}/{len(sessions)} persisted sessions",anchor_minutes=a,anchor_index=ai,total_anchors=len(FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"]),sessions_scanned=si,total_sessions=len(sessions),validation_2025_opened=False,holdout_2026_opened=False)
                if ai==1:
                    class_counts=dict(local_counts)
                    integ=FEATURE_DISCOVERY_AUDIT_SPEC["integrity"]
                    if total_observations!=integ["expected_observations"] or class_counts!=integ["expected_class_counts"]: raise RuntimeError("Persisted observation integrity totals changed during audit")
                features=sorted(fn for aa,fn in source_tests if aa==a)
                for fn in features:
                    src=source_tests[(a,fn)]; global_effect=float(src.get("standardized_effect_pos_vs_hard") or 0.0); direction=1 if global_effect>0 else (-1 if global_effect<0 else 0)
                    def vals(cells): return {sym:(v["sum"]/v["n"]) for sym,v in cells.items() if v["n"]}
                    yr_rows=[]
                    for yr in range(2019,2025):
                        d=year_agg[(fn,yr)]; p=vals(d["positive"]); h=vals(d["hard_negative"]); e=self._fd_effect_from_symbol_values({k:[v] for k,v in p.items()},{k:[v] for k,v in h.items()}); e.update({"year":yr,"positive_events":sum(v["n"] for v in d["positive"].values()),"hard_negative_events":sum(v["n"] for v in d["hard_negative"].values())}); e["same_direction"]=bool(e["effect"] is not None and direction!=0 and e["effect"]*direction>0); yr_rows.append(e)
                    usable_years=[x for x in yr_rows if x["effect"] is not None]; same_years=sum(bool(x["same_direction"]) for x in usable_years)
                    phase_rows=[]
                    phases=sorted(ph for f,ph in phase_agg if f==fn)
                    for ph in phases:
                        d=phase_agg[(fn,ph)]; p=vals(d["positive"]); h=vals(d["hard_negative"]); pe=sum(v["n"] for v in d["positive"].values()); he=sum(v["n"] for v in d["hard_negative"].values()); e=self._fd_effect_from_symbol_values({k:[v] for k,v in p.items()},{k:[v] for k,v in h.items()}); supported=pe>=100 and he>=100 and e["positive_symbols"]>=30 and e["hard_negative_symbols"]>=30; e.update({"phase":ph,"positive_events":pe,"hard_negative_events":he,"support_passed":supported,"same_direction":bool(supported and e["effect"] is not None and direction!=0 and e["effect"]*direction>0)}); phase_rows.append(e)
                    supported_ph=[x for x in phase_rows if x["support_passed"]]; same_ph=sum(bool(x["same_direction"]) for x in supported_ph)
                    hard_mean=float(src.get("hard_negative_mean_symbol_weighted")); lm={}
                    for lev in ("20","30","50"):
                        d=ladder_agg[(fn,lev)]["positive"]; arr=np.asarray([v["sum"]/v["n"] for v in d.values() if v["n"]],dtype=float); mean=float(arr.mean()) if len(arr) else None; lm[lev]={"mean":mean,"symbols":len(arr),"distance_in_discovery_direction":((mean-hard_mean)*direction if mean is not None else None)}
                    ds=[lm[x]["distance_in_discovery_direction"] for x in ("20","30","50")]; monotonic=bool(all(x is not None for x in ds) and ds[0]<=ds[1]<=ds[2])
                    diagnostics.append({"anchor_minutes":a,"feature":fn,"family":src.get("family"),"source_fdr_q":src.get("fdr_q"),"source_support_passed":src.get("support_passed"),"source_standardized_effect":global_effect,"source_promoted":bool(src.get("support_passed") and isinstance(src.get("fdr_q"),(int,float)) and src["fdr_q"]<=0.05 and abs(global_effect)>=0.10),"year_stability":{"same_direction_years":same_years,"usable_years":len(usable_years),"all_years_same_direction":same_years==len(usable_years) and len(usable_years)==6,"by_year":yr_rows},"phase_stability":{"same_direction_supported_phases":same_ph,"supported_phases":len(supported_ph),"all_supported_phases_same_direction":same_ph==len(supported_ph) and len(supported_ph)>0,"by_phase":phase_rows},"ladder_monotonicity":{"monotonic_20_30_50":monotonic,"levels":lm}})
                del year_agg,phase_agg,ladder_agg
            promoted=[x for x in diagnostics if x["source_promoted"]]
            summary={"tests":len(diagnostics),"source_promoted":len(promoted),"promoted_all_6_years_same_direction":sum(x["year_stability"]["all_years_same_direction"] for x in promoted),"promoted_all_supported_phases_same_direction":sum(x["phase_stability"]["all_supported_phases_same_direction"] for x in promoted),"promoted_ladder_monotonic_20_30_50":sum(x["ladder_monotonicity"]["monotonic_20_30_50"] for x in promoted)}
            report={"version":VERSION,"build":BUILD,"audit_id":FEATURE_DISCOVERY_AUDIT_SPEC["audit_id"],"audit_sha256":FEATURE_DISCOVERY_AUDIT_SHA256,"status":"COMPLETED","phase":"STOP_REVIEW","scope":"2019-2024 persisted Discovery observations only","source_run_id":source.get("run_id"),"source_protocol_sha256":source.get("protocol_sha256"),"source_execution_sha256":source.get("execution_sha256"),"source_completed_at":source.get("completed_at"),"min_n_frozen":source.get("min_n_frozen"),"sessions":len(sessions),"observations":total_observations,"class_counts":class_counts,"summary":summary,"diagnostics":diagnostics,"audit_mode":"memory_bounded_anchor_streaming_from_persisted_observations","alpaca_requests_made":0,"validation_2025_opened":False,"holdout_2026_opened":False,"validation_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
            self.redis.set_json(self.feature_discovery_audit_key("report"),report); self._set_feature_discovery_audit_state(status="COMPLETED",phase="STOP_REVIEW",message="Discovery Audit completed; STOP and review before any 2025 validation",anchor_index=len(FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"]),total_anchors=len(FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"]),sessions_scanned=len(sessions),total_sessions=len(sessions),observations_scanned=total_observations,summary=summary,validation_2025_opened=False,holdout_2026_opened=False,validation_allowed=False,stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Feature Discovery Audit failed"); self._set_feature_discovery_audit_state(status="ERROR",phase="BLOCKED",message="Discovery Audit failed closed",last_error=f"{type(exc).__name__}: {exc}",validation_2025_opened=False,holdout_2026_opened=False,validation_allowed=False)
        finally:
            with self.feature_discovery_audit_lock:self.feature_discovery_audit_thread=None

    def start_feature_discovery_audit(self)->tuple[bool,str]:
        allowed,reason=self._feature_discovery_audit_gate()
        if not allowed:return False,reason
        with self.feature_discovery_audit_lock:
            if self.feature_discovery_audit_thread and self.feature_discovery_audit_thread.is_alive():return False,"already_running"
            self.feature_discovery_audit_thread=threading.Thread(target=self.feature_discovery_audit_loop,name="feature-discovery-audit-2019-2024",daemon=True); self.feature_discovery_audit_thread.start()
        return True,"started"

    def feature_scoring_freeze_key(self, suffix: str) -> str:
        return self.key(f"feature_scoring_freeze:v1:{suffix}")

    def _set_feature_scoring_freeze_state(self, **updates: Any) -> None:
        with self.feature_scoring_freeze_lock:
            self.feature_scoring_freeze_state.update(updates); self.feature_scoring_freeze_state["updated_at"] = iso(); snap=dict(self.feature_scoring_freeze_state)
        if self.redis.configured: self.redis.set_json(self.feature_scoring_freeze_key("status"), snap)

    @staticmethod
    def _fsf_structural_proxy(feature: str) -> bool:
        n=str(feature or "").lower(); rule=FEATURE_SCORING_FREEZE_SPEC["pre_selection_eligibility_rule"]
        return n in set(rule["structural_proxy_exact_names"]) or any(tok in n for tok in rule["structural_proxy_name_tokens"])

    def _feature_scoring_freeze_gate(self) -> tuple[bool,str]:
        if not self.redis.configured: return False,"Redis is required"
        audit=self.redis.get_json(self.feature_discovery_audit_key("report"),None)
        if not isinstance(audit,dict) or audit.get("status")!="COMPLETED" or audit.get("phase")!="STOP_REVIEW": return False,"Completed Discovery Audit STOP_REVIEW report is required"
        if str(audit.get("audit_id"))!=FEATURE_DISCOVERY_AUDIT_SPEC["audit_id"] or str(audit.get("audit_sha256"))!=FEATURE_DISCOVERY_AUDIT_SHA256: return False,"Discovery Audit identity/hash mismatch"
        if audit.get("validation_2025_opened") is not False or audit.get("holdout_2026_opened") is not False: return False,"Validation/Holdout contamination flag"
        x=FEATURE_SCORING_FREEZE_SPEC["integrity"]; sm=audit.get("summary") or {}
        if int(audit.get("sessions") or 0)!=x["expected_sessions"] or int(audit.get("observations") or 0)!=x["expected_observations"]: return False,"Audit totals mismatch"
        if dict(audit.get("class_counts") or {})!=x["expected_class_counts"]: return False,"Audit class counts mismatch"
        if int(sm.get("source_promoted") or 0)!=x["expected_source_promoted"] or int(sm.get("promoted_all_6_years_same_direction") or 0)!=x["expected_year_stable"] or int(sm.get("promoted_all_supported_phases_same_direction") or 0)!=x["expected_phase_stable"] or int(sm.get("promoted_ladder_monotonic_20_30_50") or 0)!=x["expected_ladder_monotonic"]: return False,"Audit summary mismatch"
        return True,"allowed"

    @staticmethod
    def _fsf_choose_by_frozen_anchor_priority(rows: list[dict[str,Any]], priority_minutes: list[int]) -> list[dict[str,Any]]:
        by_feature={}
        for r in rows:
            f=str(r.get("feature")); a=int(r.get("anchor_minutes") or 0)
            by_feature.setdefault(f,{})[a]=r
        out=[]
        for f in sorted(by_feature):
            amap=by_feature[f]
            chosen=next((amap[a] for a in priority_minutes if a in amap),None)
            if chosen is not None: out.append(chosen)
        return sorted(out,key=lambda r:(priority_minutes.index(int(r.get("anchor_minutes") or 0)),str(r.get("feature"))))

    @staticmethod
    def _fsf_choose_earliest(rows: list[dict[str,Any]]) -> list[dict[str,Any]]:
        # Backward-compatible helper for legacy tests only; production freeze uses explicit role priorities.
        return IndependentPriorityRadar._fsf_choose_by_frozen_anchor_priority(rows,[240,120,60,30,15,5])

    def feature_scoring_freeze_loop(self)->None:
        try:
            allowed,reason=self._feature_scoring_freeze_gate()
            if not allowed: raise RuntimeError(reason)
            audit=self.redis.get_json(self.feature_discovery_audit_key("report"),{}) or {}
            source=self.redis.get_json(self.feature_discovery_key("report"),{}) or {}
            src={(int(x["anchor_minutes"]),str(x["feature"])):x for x in (source.get("feature_tests") or [])}
            promoted=[d for d in (audit.get("diagnostics") or []) if d.get("source_promoted")]
            stable=[d for d in promoted if d.get("year_stability",{}).get("all_years_same_direction") and d.get("phase_stability",{}).get("all_supported_phases_same_direction")]
            structural=[d for d in stable if self._fsf_structural_proxy(d.get("feature"))]
            eligible=[d for d in stable if not self._fsf_structural_proxy(d.get("feature"))]
            ap=FEATURE_SCORING_FREEZE_SPEC["anchor_selection_policy"]
            early=self._fsf_choose_by_frozen_anchor_priority([d for d in eligible if int(d.get("anchor_minutes") or 0) in ap["early_core_priority_minutes"]],ap["early_core_priority_minutes"])
            confirm=self._fsf_choose_by_frozen_anchor_priority([d for d in eligible if int(d.get("anchor_minutes") or 0) in ap["confirmation_priority_minutes"]],ap["confirmation_priority_minutes"])
            severity=self._fsf_choose_by_frozen_anchor_priority([d for d in eligible if d.get("ladder_monotonicity",{}).get("monotonic_20_30_50")],ap["severity_quality_priority_minutes"])
            def enrich(rows):
                out=[]
                for d in rows:
                    a=int(d["anchor_minutes"]); f=str(d["feature"]); t=src.get((a,f)) or {}; eff=float(t.get("standardized_effect_pos_vs_hard") or 0); pm=t.get("positive_mean_symbol_weighted"); hm=t.get("hard_negative_mean_symbol_weighted")
                    if not isinstance(pm,(int,float)) or not isinstance(hm,(int,float)) or abs(eff)<1e-12: continue
                    scale=abs((float(pm)-float(hm))/eff)
                    if not math.isfinite(scale) or scale<=1e-12: continue
                    out.append({"feature":f,"anchor_minutes":a,"family":d.get("family"),"direction":1 if eff>0 else -1,"standardized_effect":eff,"hard_negative_center":float(hm),"pooled_symbol_sd":scale,"raw_weight":abs(eff),"year_stable":True,"phase_stable":True,"ladder_monotonic":bool(d.get("ladder_monotonicity",{}).get("monotonic_20_30_50"))})
                z=sum(x["raw_weight"] for x in out) or 1.0
                for x in out:x["weight"]=x["raw_weight"]/z
                return out
            roles={"early_core":enrich(early),"confirmation":enrich(confirm),"severity_quality":enrich(severity)}
            if not roles["early_core"]: raise RuntimeError("No eligible Early Core features after frozen rules")
            sessions=sorted(str(x) for x in (self.redis.get_json(self.feature_discovery_key("completed_sessions"),[]) or []) if 2019<=int(str(x)[:4])<=2024)
            role_scores={k:defaultdict(list) for k in roles}; role_pos={k:[] for k in roles}; counts=__import__('collections').Counter()
            self._set_feature_scoring_freeze_state(status="RUNNING",phase="CALIBRATE_2019_2024",message="Calibrating frozen scores from persisted 2019-2024 observations only; 2025/2026 locked",sessions_scanned=0,total_sessions=len(sessions),validation_2025_opened=False,holdout_2026_opened=False)
            for si,sess in enumerate(sessions,1):
                obs=self.redis.get_json(self.feature_discovery_key(f"observations:{sess}"),[]) or []
                for o in obs:
                    cls=str(o.get("class") or ""); sym=str(o.get("symbol") or ""); counts[cls]+=1
                    anchors=o.get("anchors") or {}
                    for role,defs in roles.items():
                        num=den=0.0
                        for d in defs:
                            v=(anchors.get(str(d["anchor_minutes"])) or {}).get(d["feature"])
                            if not isinstance(v,(int,float)) or not math.isfinite(v): continue
                            c=d["direction"]*(float(v)-d["hard_negative_center"])/d["pooled_symbol_sd"]; c=max(-3.0,min(3.0,c)); num+=d["weight"]*c; den+=d["weight"]
                        if den<0.5: continue
                        score=num/den
                        if cls=="hard_negative": role_scores[role][sym].append(score)
                        elif cls=="positive": role_pos[role].append(score)
                if si==1 or si%100==0 or si==len(sessions): self._set_feature_scoring_freeze_state(status="RUNNING",phase="CALIBRATE_2019_2024",message=f"Frozen score calibration: {si}/{len(sessions)} persisted sessions",sessions_scanned=si,total_sessions=len(sessions),validation_2025_opened=False,holdout_2026_opened=False)
            calibration={}
            for role in roles:
                hs=np.asarray([float(np.mean(v)) for v in role_scores[role].values() if v],dtype=float); ps=np.asarray(role_pos[role],dtype=float)
                thr=float(np.quantile(hs,0.95)) if len(hs) else None
                calibration[role]={"hard_negative_symbols":len(hs),"positive_events_scored":len(ps),"threshold_rule":"95th percentile of per-symbol mean hard-negative scores","frozen_threshold":thr,"development_positive_event_pass_rate":(float(np.mean(ps>=thr)) if thr is not None and len(ps) else None),"development_hard_negative_symbol_pass_rate":(float(np.mean(hs>=thr)) if thr is not None and len(hs) else None)}
            report={"version":VERSION,"build":BUILD,"freeze_id":FEATURE_SCORING_FREEZE_SPEC["freeze_id"],"freeze_spec_sha256":FEATURE_SCORING_FREEZE_SHA256,"status":"COMPLETED","phase":"FROZEN_STOP_REVIEW","scope":"2019-2024 only","source_audit_id":audit.get("audit_id"),"source_audit_sha256":audit.get("audit_sha256"),"source_protocol_sha256":audit.get("source_protocol_sha256"),"source_execution_sha256":audit.get("source_execution_sha256"),"eligibility_rule":FEATURE_SCORING_FREEZE_SPEC["pre_selection_eligibility_rule"],"anchor_selection_policy":FEATURE_SCORING_FREEZE_SPEC["anchor_selection_policy"],"structural_proxies_excluded":[{"feature":d.get("feature"),"anchor_minutes":d.get("anchor_minutes"),"reason":"structural/data-availability proxy excluded by pre-selection eligibility rule regardless of effect"} for d in structural],"source_intersection_all_three_count":sum(bool(d.get("year_stability",{}).get("all_years_same_direction") and d.get("phase_stability",{}).get("all_supported_phases_same_direction") and d.get("ladder_monotonicity",{}).get("monotonic_20_30_50")) for d in promoted),"roles":roles,"role_counts":{k:len(v) for k,v in roles.items()},"calibration":calibration,"sessions":len(sessions),"class_counts_seen":dict(counts),"alpaca_requests_made":0,"validation_2025_opened":False,"holdout_2026_opened":False,"validation_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
            canonical=dict(report); canonical.pop("completed_at",None); report["frozen_model_sha256"]=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
            self.redis.set_json(self.feature_scoring_freeze_key("report"),report); self._set_feature_scoring_freeze_state(status="COMPLETED",phase="FROZEN_STOP_REVIEW",message="Feature/Scoring Freeze completed; STOP and review before opening 2025",sessions_scanned=len(sessions),total_sessions=len(sessions),role_counts=report["role_counts"],source_intersection_all_three_count=report["source_intersection_all_three_count"],validation_2025_opened=False,holdout_2026_opened=False,validation_allowed=False,stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Feature/Scoring Freeze failed"); self._set_feature_scoring_freeze_state(status="ERROR",phase="BLOCKED",message="Feature/Scoring Freeze failed closed",last_error=f"{type(exc).__name__}: {exc}",validation_2025_opened=False,holdout_2026_opened=False,validation_allowed=False)
        finally:
            with self.feature_scoring_freeze_lock:self.feature_scoring_freeze_thread=None

    def start_feature_scoring_freeze(self)->tuple[bool,str]:
        allowed,reason=self._feature_scoring_freeze_gate()
        if not allowed:return False,reason
        with self.feature_scoring_freeze_lock:
            if self.feature_scoring_freeze_thread and self.feature_scoring_freeze_thread.is_alive():return False,"already_running"
            self.feature_scoring_freeze_thread=threading.Thread(target=self.feature_scoring_freeze_loop,name="feature-scoring-freeze-2019-2024",daemon=True); self.feature_scoring_freeze_thread.start()
        return True,"started"

    def validation_criteria_key(self, suffix: str) -> str:
        return self.key(f"validation_success_criteria:v1:{suffix}")

    def _validation_criteria_gate(self) -> tuple[bool,str]:
        if not self.redis.configured: return False,"Redis is required"
        report=self.redis.get_json(self.feature_scoring_freeze_key("report"),None)
        if not isinstance(report,dict) or report.get("status")!="COMPLETED" or report.get("phase")!="FROZEN_STOP_REVIEW": return False,"Completed frozen model report is required"
        if report.get("validation_2025_opened") is not False or report.get("holdout_2026_opened") is not False: return False,"Validation/Holdout contamination flag"
        if str(report.get("freeze_id"))!=VALIDATION_SUCCESS_CRITERIA_SPEC["required_freeze_id"]: return False,"Frozen model freeze_id mismatch"
        if str(report.get("frozen_model_sha256"))!=VALIDATION_SUCCESS_CRITERIA_SPEC["required_frozen_model_sha256"]: return False,"Frozen model SHA256 mismatch"
        return True,"allowed"

    def freeze_validation_success_criteria(self) -> tuple[bool,str]:
        allowed,reason=self._validation_criteria_gate()
        if not allowed:return False,reason
        model=self.redis.get_json(self.feature_scoring_freeze_key("report"),{}) or {}
        cal=model.get("calibration") or {}
        derived={}
        for role,rule in VALIDATION_SUCCESS_CRITERIA_SPEC["role_rules"].items():
            dev=float((cal.get(role) or {}).get("development_positive_event_pass_rate"))
            derived[role]={
                "development_positive_event_pass_rate":dev,
                "pass_min_2025_positive_event_pass_rate":dev*float(rule["pass_min_development_recall_retention"]),
                "weak_min_2025_positive_event_pass_rate":dev*float(rule["weak_min_development_recall_retention"]),
                "max_2025_hard_negative_symbol_pass_rate":float(rule["max_hard_negative_symbol_pass_rate"]),
                "critical":bool(rule.get("critical")),
            }
        report={"version":VERSION,"build":BUILD,"criteria_id":VALIDATION_SUCCESS_CRITERIA_SPEC["criteria_id"],"criteria_sha256":VALIDATION_SUCCESS_CRITERIA_SHA256,"status":"COMPLETED","phase":"CRITERIA_FROZEN_STOP_REVIEW","scope":"Frozen before any 2025/2026 read","source_freeze_id":model.get("freeze_id"),"source_frozen_model_sha256":model.get("frozen_model_sha256"),"criteria":VALIDATION_SUCCESS_CRITERIA_SPEC,"derived_numeric_thresholds":derived,"alpaca_requests_made":0,"validation_2025_opened":False,"holdout_2026_opened":False,"validation_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
        canonical=dict(report); canonical.pop("completed_at",None); report["criteria_artifact_sha256"]=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        self.redis.set_json(self.validation_criteria_key("report"),report)
        state={"status":"COMPLETED","phase":"CRITERIA_FROZEN_STOP_REVIEW","message":"Validation success criteria frozen; STOP and review before opening 2025","criteria_id":VALIDATION_SUCCESS_CRITERIA_SPEC["criteria_id"],"criteria_sha256":VALIDATION_SUCCESS_CRITERIA_SHA256,"criteria_artifact_sha256":report["criteria_artifact_sha256"],"validation_2025_opened":False,"holdout_2026_opened":False,"validation_allowed":False,"updated_at":iso()}
        self.validation_criteria_state=state; self.redis.set_json(self.validation_criteria_key("status"),state)
        return True,"frozen"

    def holdout_criteria_key(self, suffix: str) -> str:
        return self.key(f"holdout_success_criteria:v1:{suffix}")

    def _holdout_criteria_gate(self) -> tuple[bool,str]:
        if not self.redis.configured: return False,"Redis is required"
        model=self.redis.get_json(self.feature_scoring_freeze_key("report"),None)
        precrit=self.redis.get_json(self.validation_criteria_key("report"),None)
        val=self.redis.get_json(self.validation_2025_key("report"),None)
        if not isinstance(model,dict) or str(model.get("frozen_model_sha256"))!=HOLDOUT_SUCCESS_CRITERIA_SPEC["required_frozen_model_sha256"]: return False,"Frozen model SHA256 mismatch"
        if not isinstance(precrit,dict) or str(precrit.get("criteria_sha256"))!=HOLDOUT_SUCCESS_CRITERIA_SPEC["required_pre2025_criteria_sha256"]: return False,"Pre-2025 criteria SHA256 mismatch"
        if not isinstance(val,dict) or val.get("status")!="COMPLETED" or val.get("phase")!="VALIDATION_2025_STOP_REVIEW": return False,"Completed 2025 Validation STOP_REVIEW report required"
        if str(val.get("validation_result_sha256"))!=HOLDOUT_SUCCESS_CRITERIA_SPEC["required_validation_result_sha256"]: return False,"2025 Validation result SHA256 mismatch"
        if str(val.get("overall_classification"))!=HOLDOUT_SUCCESS_CRITERIA_SPEC["required_validation_overall_classification"]: return False,"2025 Validation classification mismatch"
        if val.get("holdout_2026_opened") is not False or val.get("holdout_2026_read") is not False: return False,"2026 contamination flag"
        return True,"allowed"

    def freeze_holdout_success_criteria(self) -> tuple[bool,str]:
        allowed,reason=self._holdout_criteria_gate()
        if not allowed:return False,reason
        model=self.redis.get_json(self.feature_scoring_freeze_key("report"),{}) or {}
        val=self.redis.get_json(self.validation_2025_key("report"),{}) or {}
        cal=model.get("calibration") or {}; vr=val.get("role_results") or {}; derived={}
        pr=HOLDOUT_SUCCESS_CRITERIA_SPEC["performance_rules"]
        for role in ("early_core","confirmation","severity_quality"):
            dev=float((cal.get(role) or {}).get("development_positive_event_pass_rate"))
            rv=vr.get(role) or {}; raw=int(rv.get("positive_events_raw_selected") or 0); scored=int(rv.get("positive_events_scoreable") or 0)
            if raw<=0: return False,f"Missing 2025 positive scoreability denominator for {role}"
            base=scored/raw
            derived[role]={
                "critical": role in HOLDOUT_SUCCESS_CRITERIA_SPEC["critical_roles"],
                "development_positive_event_pass_rate":dev,
                "pass_min_2026_positive_event_pass_rate":dev*float(pr["pass_min_development_recall_retention"]),
                "weak_min_2026_positive_event_pass_rate":dev*float(pr["weak_min_development_recall_retention"]),
                "max_2026_hard_negative_symbol_pass_rate":float(pr["max_hard_negative_symbol_pass_rate"]),
                "validation_2025_positive_scoreability_diagnostic":base,
                "scoreability_cutoff_for_official_classification":None,
            }
        report={"version":VERSION,"build":BUILD,"criteria_id":HOLDOUT_SUCCESS_CRITERIA_SPEC["criteria_id"],"criteria_sha256":HOLDOUT_SUCCESS_CRITERIA_SHA256,"status":"COMPLETED","phase":"HOLDOUT_CRITERIA_FROZEN_STOP_REVIEW","scope":"Frozen before any 2026 read","source_frozen_model_sha256":model.get("frozen_model_sha256"),"source_validation_result_sha256":val.get("validation_result_sha256"),"criteria":HOLDOUT_SUCCESS_CRITERIA_SPEC,"derived_numeric_thresholds":derived,"alpaca_requests_made":0,"holdout_2026_opened":False,"holdout_2026_read":False,"holdout_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
        canonical=dict(report);canonical.pop("completed_at",None);report["criteria_artifact_sha256"]=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        self.redis.set_json(self.holdout_criteria_key("report"),report)
        state={"status":"COMPLETED","phase":"HOLDOUT_CRITERIA_FROZEN_STOP_REVIEW","message":"2026 Holdout protocol locked; original pre-2025 criteria inherited; scoreability diagnostic only; STOP and review before opening 2026","criteria_id":HOLDOUT_SUCCESS_CRITERIA_SPEC["criteria_id"],"criteria_sha256":HOLDOUT_SUCCESS_CRITERIA_SHA256,"criteria_artifact_sha256":report["criteria_artifact_sha256"],"holdout_2026_opened":False,"holdout_2026_read":False,"holdout_allowed":False,"updated_at":iso()}
        self.holdout_criteria_state=state;self.redis.set_json(self.holdout_criteria_key("status"),state)
        return True,"frozen"

    def start_phase0b_full(self) -> tuple[bool,str]:
        allowed,reason=self._phase0b_full_gate()
        if not allowed: return False,reason
        with self.phase0b_full_lock:
            if self.phase0b_full_thread and self.phase0b_full_thread.is_alive(): return False,"already_running"
            self.phase0b_full_thread=threading.Thread(target=self.phase0b_full_loop,name="phase0b-full-cycle-verification",daemon=True); self.phase0b_full_thread.start()
        return True,"started"

    def validation_2025_key(self,suffix): return self.key(f"frozen_model_validation_2025:v1:{suffix}")
    def _set_validation_2025_state(self,**u):
        with self.validation_2025_lock: self.validation_2025_state.update(u); self.validation_2025_state["updated_at"]=iso(); snap=dict(self.validation_2025_state)
        if self.redis.configured:self.redis.set_json(self.validation_2025_key("status"),snap)
    def _validation_2025_gate(self):
        if not self.redis.configured:return False,"Redis required"
        m=self.redis.get_json(self.feature_scoring_freeze_key("report"),None); c=self.redis.get_json(self.validation_criteria_key("report"),None)
        if not isinstance(m,dict) or m.get("frozen_model_sha256")!=VALIDATION_2025_SPEC["required_frozen_model_sha256"]:return False,"Frozen model SHA mismatch"
        if not isinstance(c,dict) or c.get("criteria_sha256")!=VALIDATION_2025_SPEC["required_criteria_sha256"] or c.get("criteria_artifact_sha256")!=VALIDATION_2025_SPEC["required_criteria_artifact_sha256"]:return False,"Frozen criteria SHA mismatch"
        if m.get("holdout_2026_opened") is not False or c.get("holdout_2026_opened") is not False:return False,"2026 contamination flag"
        old=self.redis.get_json(self.validation_2025_key("report"),None)
        if isinstance(old,dict) and old.get("status")=="COMPLETED":return False,"2025 validation already completed; rerun prohibited"
        return True,"allowed"
    @staticmethod
    def _v25_class(pr,hr,lim):
        if pr is None or hr is None or hr>float(lim["max_2025_hard_negative_symbol_pass_rate"]):return "FAIL"
        if pr>=float(lim["pass_min_2025_positive_event_pass_rate"]):return "PASS"
        if pr>=float(lim["weak_min_2025_positive_event_pass_rate"]):return "WEAK_PASS"
        return "FAIL"
    def frozen_model_validation_2025_loop(self):
        try:
            ok,why=self._validation_2025_gate()
            if not ok:raise RuntimeError(why)
            m=self.redis.get_json(self.feature_scoring_freeze_key("report"),{}) or {}; c=self.redis.get_json(self.validation_criteria_key("report"),{}) or {}; roles=m["roles"]; cal=m["calibration"]; lim=c["derived_numeric_thresholds"]
            sessions=sorted(str(x) for x in (self.redis.get_json(self.phase0b_full_key("completed_sessions"),[]) or []) if str(x).startswith("2025-"))
            if not sessions:raise RuntimeError("No persisted 2025 Phase0B sessions")
            done=set(self.redis.get_json(self.validation_2025_key("completed_sessions"),[]) or []); self.validation_2025_stop_event.clear(); self._set_validation_2025_state(status="RUNNING",phase="VALIDATE_2025",message="2025 opened; 2026 hard-locked",validation_2025_opened=True,holdout_2026_opened=False,total_sessions=len(sessions),sessions_scanned=len(done))
            for si,sess in enumerate(sessions,1):
                if sess in done:continue
                if self.validation_2025_stop_event.is_set():self._set_validation_2025_state(status="PAUSED",phase="VALIDATE_2025",validation_2025_opened=True,holdout_2026_opened=False,sessions_scanned=len(done),total_sessions=len(sessions));return
                target=date.fromisoformat(sess); p0=self.redis.get_json(self.phase0b_full_key(f"results:{sess}"),None)
                if p0 is None:raise RuntimeError(f"Missing 2025 Phase0B {sess}")
                coarse={str(x.get("symbol") or "").upper():x for x in (self.redis.get_json(self.historical_census_key(f"candidates:{sess}"),[]) or [])}; pos=[x for x in p0 if x.get("classification")=="verified"]; fail=[x for x in p0 if x.get("classification")=="failed"]; fctx=[]
                for r in fail:
                    sym=str(r.get("symbol") or "").upper(); cut=self._fd_coarse_cutoff(coarse.get(sym,{}))
                    if cut:fctx.append((r,sym,cut,self._fd_phase(cut,target),self._fd_price_band(r.get("t1_low"))))
                sel=[]
                for pi,r in enumerate(pos):
                    sym=str(r.get("symbol") or "").upper(); phase=self._fd_phase(str(r.get("t2") or ""),target); pb=self._fd_price_band(r.get("t1_low")); exact=[x for x in fctx if x[3]==phase and x[4]==pb]; pool=exact or [x for x in fctx if x[3]==phase] or fctx; base=int(hashlib.sha256(f"{sess}|{sym}|{r.get('t2')}".encode()).hexdigest()[:12],16); match=hashlib.sha256(f"{sess}|{sym}|{r.get('t2')}|{pi}".encode()).hexdigest()[:20]; sel.append(("positive",r,sym,str(r.get("t2")),match))
                    for j in range(min(3,len(pool))):x=pool[(base+j*7919)%len(pool)];sel.append(("hard_negative",x[0],x[1],x[2],match))
                syms=sorted({x[2] for x in sel}); start,end=self._probe_cycle_bounds(target); rows=self._fd_fetch_session_rows(syms,target,start,end) if syms else {}; obs=[]
                for cls,r,sym,cut,match in sel:
                    try:dt=datetime.fromisoformat(str(cut).replace("Z","+00:00"))
                    except:continue
                    aa={}
                    for off in FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"]:
                        f=self._fd_features(rows.get(sym,[]),dt-timedelta(minutes=off))
                        if f:aa[str(off)]=f
                    if aa:obs.append({"class":cls,"symbol":sym,"match_id":match,"anchors":aa})
                groups=defaultdict(list)
                for o in obs:groups[o["match_id"]].append(o)
                filt=[]
                for g in groups.values():
                    po=next((o for o in g if o["class"]=="positive"),None)
                    if not po:continue
                    pa=(po["anchors"].get("5") or {}).get("bars_available");filt.append(po)
                    for o in g:
                        if o is po:continue
                        ca=(o["anchors"].get("5") or {}).get("bars_available")
                        if isinstance(pa,(int,float)) and isinstance(ca,(int,float)) and pa>0 and .5<=ca/pa<=2:filt.append(o)
                scored=[]
                for o in filt:
                    ss={}
                    for role,defs in roles.items():
                        num=den=0.0
                        for d in defs:
                            v=(o["anchors"].get(str(d["anchor_minutes"])) or {}).get(d["feature"])
                            if not isinstance(v,(int,float)) or not math.isfinite(v):continue
                            z=d["direction"]*(float(v)-d["hard_negative_center"])/d["pooled_symbol_sd"];num+=d["weight"]*max(-3,min(3,z));den+=d["weight"]
                        if den>=.5:ss[role]=num/den
                    scored.append({"class":o["class"],"symbol":o["symbol"],"scores":ss})
                self.redis.set_json(self.validation_2025_key(f"scores:{sess}"),scored);done.add(sess);self.redis.set_json(self.validation_2025_key("completed_sessions"),sorted(done))
                if si==1 or si%25==0 or si==len(sessions):self._set_validation_2025_state(status="RUNNING",phase="VALIDATE_2025",message=f"2025 frozen validation {len(done)}/{len(sessions)}",validation_2025_opened=True,holdout_2026_opened=False,sessions_scanned=len(done),total_sessions=len(sessions),current_session=sess)
            ps=defaultdict(list);hs=defaultdict(lambda:defaultdict(list));raw=defaultdict(int)
            for sess in sessions:
                for o in self.redis.get_json(self.validation_2025_key(f"scores:{sess}"),[]) or []:
                    raw[o["class"]]+=1
                    for role,v in o["scores"].items():
                        if o["class"]=="positive":ps[role].append(float(v))
                        elif o["class"]=="hard_negative":hs[role][o["symbol"]].append(float(v))
            rr={}
            for role in roles:
                thr=float(cal[role]["frozen_threshold"]);pa=np.asarray(ps[role]);ha=np.asarray([np.mean(v) for v in hs[role].values()]);pr=float(np.mean(pa>=thr)) if len(pa) else None;hr=float(np.mean(ha>=thr)) if len(ha) else None;rr[role]={"classification":self._v25_class(pr,hr,lim[role]),"frozen_threshold":thr,"positive_event_pass_rate":pr,"hard_negative_symbol_pass_rate":hr,"positive_events_scoreable":len(pa),"hard_negative_symbols_scoreable":len(ha),"positive_events_raw_selected":raw["positive"],"hard_negative_events_raw_selected":raw["hard_negative"],"criteria":lim[role]}
            critical=[rr["early_core"]["classification"],rr["confirmation"]["classification"]];overall="FAIL" if "FAIL" in critical else ("PASS" if critical==["PASS","PASS"] else "WEAK_PASS")
            report={"version":VERSION,"build":BUILD,"validation_id":VALIDATION_2025_SPEC["validation_id"],"validation_spec_sha256":VALIDATION_2025_SHA256,"status":"COMPLETED","phase":"VALIDATION_2025_STOP_REVIEW","scope":"2025 only","source_frozen_model_sha256":m["frozen_model_sha256"],"source_criteria_sha256":c["criteria_sha256"],"source_criteria_artifact_sha256":c["criteria_artifact_sha256"],"sessions":len(sessions),"role_results":rr,"overall_classification":overall,"validation_2025_opened":True,"holdout_2026_opened":False,"holdout_2026_read":False,"model_mutated":False,"thresholds_recalibrated":False,"stop_and_review_required":True,"completed_at":iso()};canon=dict(report);canon.pop("completed_at");report["validation_result_sha256"]=hashlib.sha256(json.dumps(canon,sort_keys=True,separators=(",", ":"),allow_nan=False).encode()).hexdigest();self.redis.set_json(self.validation_2025_key("report"),report);self._set_validation_2025_state(status="COMPLETED",phase="VALIDATION_2025_STOP_REVIEW",message=f"2025 Validation {overall}; STOP REVIEW; 2026 locked",overall_classification=overall,validation_2025_opened=True,holdout_2026_opened=False,sessions_scanned=len(sessions),total_sessions=len(sessions),stop_and_review_required=True)
        except Exception as e:logging.exception("2025 validation failed");self._set_validation_2025_state(status="ERROR",phase="BLOCKED",message="2025 validation failed closed; 2026 locked",last_error=f"{type(e).__name__}: {e}",validation_2025_opened=True,holdout_2026_opened=False)
        finally:
            with self.validation_2025_lock:self.validation_2025_thread=None
    def start_frozen_model_validation_2025(self):
        ok,why=self._validation_2025_gate()
        if not ok:return False,why
        with self.validation_2025_lock:
            if self.validation_2025_thread and self.validation_2025_thread.is_alive():return False,"already_running"
            self.validation_2025_thread=threading.Thread(target=self.frozen_model_validation_2025_loop,daemon=True);self.validation_2025_thread.start()
        return True,"started"

    def final_holdout_2026_key(self,suffix): return self.key(f"final_frozen_holdout_2026:v1:{suffix}")
    def _set_final_holdout_2026_state(self,**u):
        with self.final_holdout_2026_lock:
            self.final_holdout_2026_state.update(u); self.final_holdout_2026_state["updated_at"]=iso(); snap=dict(self.final_holdout_2026_state)
        if self.redis.configured:self.redis.set_json(self.final_holdout_2026_key("status"),snap)
    def _final_holdout_2026_gate(self):
        if not self.redis.configured:return False,"Redis required"
        m=self.redis.get_json(self.feature_scoring_freeze_key("report"),None)
        pre=self.redis.get_json(self.validation_criteria_key("report"),None)
        val=self.redis.get_json(self.validation_2025_key("report"),None)
        hc=self.redis.get_json(self.holdout_criteria_key("report"),None)
        if not isinstance(m,dict) or m.get("frozen_model_sha256")!=FINAL_HOLDOUT_2026_SPEC["required_frozen_model_sha256"]:return False,"Frozen model SHA mismatch"
        if not isinstance(pre,dict) or pre.get("criteria_sha256")!=FINAL_HOLDOUT_2026_SPEC["required_pre2025_criteria_sha256"]:return False,"Pre-2025 criteria SHA mismatch"
        if not isinstance(val,dict) or val.get("validation_result_sha256")!=FINAL_HOLDOUT_2026_SPEC["required_validation_result_sha256"] or val.get("overall_classification")!="PASS":return False,"2025 Validation provenance mismatch"
        if not isinstance(hc,dict) or hc.get("status")!="COMPLETED" or hc.get("phase")!="HOLDOUT_CRITERIA_FROZEN_STOP_REVIEW":return False,"Completed Holdout Protocol Lock required"
        if hc.get("criteria_id")!=FINAL_HOLDOUT_2026_SPEC["required_holdout_criteria_id"] or hc.get("criteria_sha256")!=FINAL_HOLDOUT_2026_SPEC["required_holdout_criteria_sha256"] or hc.get("criteria_artifact_sha256")!=FINAL_HOLDOUT_2026_SPEC["required_holdout_criteria_artifact_sha256"]:return False,"Holdout Protocol Lock SHA mismatch"
        if hc.get("holdout_2026_opened") is not False or hc.get("holdout_2026_read") is not False:return False,"2026 contamination flag"
        old=self.redis.get_json(self.final_holdout_2026_key("report"),None)
        if isinstance(old,dict) and old.get("status")=="COMPLETED":return False,"2026 final holdout already completed; rerun prohibited"
        return True,"allowed"
    @staticmethod
    def _h26_class(pr,hr,dev,perf):
        if pr is None or hr is None or dev is None or hr>float(perf["max_hard_negative_symbol_pass_rate"]):return "FAIL"
        if pr>=float(dev)*float(perf["pass_min_development_recall_retention"]):return "PASS"
        if pr>=float(dev)*float(perf["weak_min_development_recall_retention"]):return "WEAK_PASS"
        return "FAIL"
    def final_frozen_holdout_2026_loop(self):
        opened=False
        try:
            ok,why=self._final_holdout_2026_gate()
            if not ok:raise RuntimeError(why)
            m=self.redis.get_json(self.feature_scoring_freeze_key("report"),{}) or {}
            val=self.redis.get_json(self.validation_2025_key("report"),{}) or {}
            hc=self.redis.get_json(self.holdout_criteria_key("report"),{}) or {}
            roles=m["roles"]; cal=m["calibration"]; perf=hc["criteria"]["performance_rules"]
            sessions=sorted(str(x) for x in (self.redis.get_json(self.phase0b_full_key("completed_sessions"),[]) or []) if str(x).startswith("2026-") and str(x)<=FINAL_HOLDOUT_2026_SPEC["last_allowed_session"])
            if not sessions:raise RuntimeError("No persisted 2026 Phase0B sessions through 2026-08-31")
            done=set(self.redis.get_json(self.final_holdout_2026_key("completed_sessions"),[]) or [])
            if any(not str(x).startswith("2026-") or str(x)>FINAL_HOLDOUT_2026_SPEC["last_allowed_session"] for x in done):raise RuntimeError("Invalid persisted holdout resume session")
            self.final_holdout_2026_stop_event.clear(); opened=True
            self._set_final_holdout_2026_state(status="RUNNING",phase="FINAL_HOLDOUT_2026",message="2026 final holdout opened; frozen model and criteria locked",holdout_2026_opened=True,holdout_2026_read=True,total_sessions=len(sessions),sessions_scanned=len(done))
            for si,sess in enumerate(sessions,1):
                if sess in done:continue
                if self.final_holdout_2026_stop_event.is_set():
                    self._set_final_holdout_2026_state(status="PAUSED",phase="FINAL_HOLDOUT_2026",holdout_2026_opened=True,holdout_2026_read=True,sessions_scanned=len(done),total_sessions=len(sessions));return
                target=date.fromisoformat(sess); p0=self.redis.get_json(self.phase0b_full_key(f"results:{sess}"),None)
                if p0 is None:raise RuntimeError(f"Missing 2026 Phase0B {sess}")
                coarse={str(x.get("symbol") or "").upper():x for x in (self.redis.get_json(self.historical_census_key(f"candidates:{sess}"),[]) or [])}
                pos=[x for x in p0 if x.get("classification")=="verified"]; fail=[x for x in p0 if x.get("classification")=="failed"]; fctx=[]
                for r in fail:
                    sym=str(r.get("symbol") or "").upper(); cut=self._fd_coarse_cutoff(coarse.get(sym,{}))
                    if cut:fctx.append((r,sym,cut,self._fd_phase(cut,target),self._fd_price_band(r.get("t1_low"))))
                sel=[]
                for pi,r in enumerate(pos):
                    sym=str(r.get("symbol") or "").upper(); phase=self._fd_phase(str(r.get("t2") or ""),target); pb=self._fd_price_band(r.get("t1_low")); exact=[x for x in fctx if x[3]==phase and x[4]==pb]; pool=exact or [x for x in fctx if x[3]==phase] or fctx; base=int(hashlib.sha256(f"{sess}|{sym}|{r.get('t2')}".encode()).hexdigest()[:12],16); match=hashlib.sha256(f"{sess}|{sym}|{r.get('t2')}|{pi}".encode()).hexdigest()[:20]; sel.append(("positive",r,sym,str(r.get("t2")),match))
                    for j in range(min(3,len(pool))):x=pool[(base+j*7919)%len(pool)];sel.append(("hard_negative",x[0],x[1],x[2],match))
                syms=sorted({x[2] for x in sel}); start,end=self._probe_cycle_bounds(target); rows=self._fd_fetch_session_rows(syms,target,start,end) if syms else {}; obs=[]
                for cls,r,sym,cut,match in sel:
                    try:dt=datetime.fromisoformat(str(cut).replace("Z","+00:00"))
                    except:continue
                    aa={}
                    for off in FEATURE_DISCOVERY_EXEC_SPEC["anchors_minutes"]:
                        f=self._fd_features(rows.get(sym,[]),dt-timedelta(minutes=off))
                        if f:aa[str(off)]=f
                    if aa:obs.append({"class":cls,"symbol":sym,"match_id":match,"anchors":aa})
                groups=defaultdict(list)
                for o in obs:groups[o["match_id"]].append(o)
                filt=[]
                for g in groups.values():
                    po=next((o for o in g if o["class"]=="positive"),None)
                    if not po:continue
                    pa=(po["anchors"].get("5") or {}).get("bars_available");filt.append(po)
                    for o in g:
                        if o is po:continue
                        ca=(o["anchors"].get("5") or {}).get("bars_available")
                        if isinstance(pa,(int,float)) and isinstance(ca,(int,float)) and pa>0 and .5<=ca/pa<=2:filt.append(o)
                scored=[]
                for o in filt:
                    ss={}
                    for role,defs in roles.items():
                        num=den=0.0
                        for d in defs:
                            v=(o["anchors"].get(str(d["anchor_minutes"])) or {}).get(d["feature"])
                            if not isinstance(v,(int,float)) or not math.isfinite(v):continue
                            z=d["direction"]*(float(v)-d["hard_negative_center"])/d["pooled_symbol_sd"];num+=d["weight"]*max(-3,min(3,z));den+=d["weight"]
                        if den>=.5:ss[role]=num/den
                    scored.append({"class":o["class"],"symbol":o["symbol"],"scores":ss})
                self.redis.set_json(self.final_holdout_2026_key(f"scores:{sess}"),scored);done.add(sess);self.redis.set_json(self.final_holdout_2026_key("completed_sessions"),sorted(done))
                if si==1 or si%20==0 or si==len(sessions):self._set_final_holdout_2026_state(status="RUNNING",phase="FINAL_HOLDOUT_2026",message=f"2026 final frozen holdout {len(done)}/{len(sessions)}",holdout_2026_opened=True,holdout_2026_read=True,sessions_scanned=len(done),total_sessions=len(sessions),current_session=sess)
            ps=defaultdict(list);hs=defaultdict(lambda:defaultdict(list));raw=defaultdict(int)
            for sess in sessions:
                for o in self.redis.get_json(self.final_holdout_2026_key(f"scores:{sess}"),[]) or []:
                    raw[o["class"]]+=1
                    for role,v in o["scores"].items():
                        if o["class"]=="positive":ps[role].append(float(v))
                        elif o["class"]=="hard_negative":hs[role][o["symbol"]].append(float(v))
            rr={}
            for role in roles:
                thr=float(cal[role]["frozen_threshold"]); dev=float(cal[role]["development_positive_event_pass_rate"]); pa=np.asarray(ps[role]); ha=np.asarray([np.mean(v) for v in hs[role].values()]); pr=float(np.mean(pa>=thr)) if len(pa) else None; hr=float(np.mean(ha>=thr)) if len(ha) else None; rawp=int(raw["positive"]); scoreability=(len(pa)/rawp if rawp else None); v25=(val.get("role_results") or {}).get(role,{})
                rr[role]={"classification":self._h26_class(pr,hr,dev,perf),"frozen_threshold":thr,"development_positive_event_pass_rate":dev,"pass_min_2026_positive_event_pass_rate":dev*float(perf["pass_min_development_recall_retention"]),"weak_min_2026_positive_event_pass_rate":dev*float(perf["weak_min_development_recall_retention"]),"max_2026_hard_negative_symbol_pass_rate":float(perf["max_hard_negative_symbol_pass_rate"]),"positive_event_pass_rate":pr,"hard_negative_symbol_pass_rate":hr,"positive_events_scoreable":len(pa),"hard_negative_symbols_scoreable":len(ha),"positive_events_raw_selected":rawp,"hard_negative_events_raw_selected":int(raw["hard_negative"]),"positive_scoreability_rate":scoreability,"scoreability_classification_role":"diagnostic_only","validation_2025_positive_event_pass_rate":v25.get("positive_event_pass_rate"),"validation_2025_hard_negative_symbol_pass_rate":v25.get("hard_negative_symbol_pass_rate"),"validation_2025_positive_scoreability_rate":((float(v25.get("positive_events_scoreable"))/float(v25.get("positive_events_raw_selected"))) if v25.get("positive_events_raw_selected") else None)}
            critical=[rr["early_core"]["classification"],rr["confirmation"]["classification"]];overall="FAIL" if "FAIL" in critical else ("PASS" if critical==["PASS","PASS"] else "WEAK_PASS")
            report={"version":VERSION,"build":BUILD,"holdout_id":FINAL_HOLDOUT_2026_SPEC["holdout_id"],"holdout_spec_sha256":FINAL_HOLDOUT_2026_SHA256,"status":"COMPLETED","phase":"FINAL_HOLDOUT_2026_STOP_REVIEW","scope":FINAL_HOLDOUT_2026_SPEC["scope"],"source_frozen_model_sha256":m["frozen_model_sha256"],"source_pre2025_criteria_sha256":FINAL_HOLDOUT_2026_SPEC["required_pre2025_criteria_sha256"],"source_validation_result_sha256":val.get("validation_result_sha256"),"source_holdout_criteria_sha256":hc.get("criteria_sha256"),"source_holdout_criteria_artifact_sha256":hc.get("criteria_artifact_sha256"),"sessions":len(sessions),"first_session":sessions[0],"last_session":sessions[-1],"role_results":rr,"overall_classification":overall,"holdout_2026_opened":True,"holdout_2026_read":True,"model_mutated":False,"thresholds_recalibrated":False,"criteria_reinterpreted":False,"scoreability_diagnostic_only":True,"stop_and_review_required":True,"live_or_profitability_claim_allowed":False,"completed_at":iso()};canon=dict(report);canon.pop("completed_at");report["holdout_result_sha256"]=hashlib.sha256(json.dumps(canon,sort_keys=True,separators=(",", ":"),allow_nan=False).encode()).hexdigest();self.redis.set_json(self.final_holdout_2026_key("report"),report);self._set_final_holdout_2026_state(status="COMPLETED",phase="FINAL_HOLDOUT_2026_STOP_REVIEW",message=f"2026 Final Holdout {overall}; STOP REVIEW",overall_classification=overall,holdout_2026_opened=True,holdout_2026_read=True,sessions_scanned=len(sessions),total_sessions=len(sessions),first_session=sessions[0],last_session=sessions[-1],stop_and_review_required=True)
        except Exception as e:
            logging.exception("2026 final holdout failed");self._set_final_holdout_2026_state(status="ERROR",phase="BLOCKED",message="2026 final holdout failed closed",last_error=f"{type(e).__name__}: {e}",holdout_2026_opened=opened,holdout_2026_read=opened)
        finally:
            with self.final_holdout_2026_lock:self.final_holdout_2026_thread=None
    def start_final_frozen_holdout_2026(self):
        ok,why=self._final_holdout_2026_gate()
        if not ok:return False,why
        with self.final_holdout_2026_lock:
            if self.final_holdout_2026_thread and self.final_holdout_2026_thread.is_alive():return False,"already_running"
            self.final_holdout_2026_thread=threading.Thread(target=self.final_frozen_holdout_2026_loop,name="final-frozen-holdout-2026",daemon=True);self.final_holdout_2026_thread.start()
        return True,"started"

    def causal_translation_protocol_key(self,suffix): return self.key(f"causal_trading_translation_protocol:v1:{suffix}")
    def _causal_translation_protocol_gate(self):
        if not self.redis.configured:return False,"Redis required"
        h=self.redis.get_json(self.final_holdout_2026_key("report"),None)
        if not isinstance(h,dict) or h.get("status")!="COMPLETED" or h.get("phase")!="FINAL_HOLDOUT_2026_STOP_REVIEW":return False,"Completed Final 2026 Holdout STOP_REVIEW required"
        if h.get("overall_classification")!="PASS" or h.get("holdout_result_sha256")!=CAUSAL_TRADING_TRANSLATION_SPEC["required_final_holdout_result_sha256"]:return False,"Final Holdout result provenance mismatch"
        if h.get("source_frozen_model_sha256")!=CAUSAL_TRADING_TRANSLATION_SPEC["required_frozen_model_sha256"]:return False,"Frozen model provenance mismatch"
        return True,"allowed"
    def freeze_causal_translation_protocol(self):
        ok,why=self._causal_translation_protocol_gate()
        if not ok:return False,why
        old=self.redis.get_json(self.causal_translation_protocol_key("report"),None)
        if isinstance(old,dict) and old.get("status")=="COMPLETED":return False,"already_frozen"
        report={"version":VERSION,"build":BUILD,"translation_id":CAUSAL_TRADING_TRANSLATION_SPEC["translation_id"],"translation_spec":CAUSAL_TRADING_TRANSLATION_SPEC,"translation_spec_sha256":CAUSAL_TRADING_TRANSLATION_SHA256,"status":"COMPLETED","phase":"CAUSAL_TRANSLATION_PROTOCOL_FROZEN_STOP_REVIEW","source_final_holdout_result_sha256":CAUSAL_TRADING_TRANSLATION_SPEC["required_final_holdout_result_sha256"],"source_frozen_model_sha256":CAUSAL_TRADING_TRANSLATION_SPEC["required_frozen_model_sha256"],"post_signal_paths_read":False,"alpaca_requests_made":0,"model_mutated":False,"thresholds_recalibrated":False,"stop_and_review_required":True,"completed_at":iso()}
        canon=dict(report);canon.pop("completed_at");report["translation_protocol_artifact_sha256"]=hashlib.sha256(json.dumps(canon,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        self.redis.set_json(self.causal_translation_protocol_key("report"),report)
        self.redis.set_json(self.causal_translation_protocol_key("status"),{"status":"COMPLETED","phase":"CAUSAL_TRANSLATION_PROTOCOL_FROZEN_STOP_REVIEW","message":"Causal Trading Translation diagnostic protocol frozen; no post-signal paths read; STOP REVIEW before replay","translation_id":CAUSAL_TRADING_TRANSLATION_SPEC["translation_id"],"post_signal_paths_read":False,"updated_at":iso()})
        return True,"frozen"


    def early_causal_entry_research_key(self, suffix: str) -> str:
        return self.key(f"early_causal_entry_research:v1:{suffix}")

    def _early_causal_entry_research_protocol_gate(self):
        if not self.redis.configured:
            return False, "Redis required"
        r = self.redis.get_json(self.causal_diagnostic_replay_key("report"), None)
        if not isinstance(r, dict) or r.get("status") != "COMPLETED" or r.get("phase") != "CAUSAL_DIAGNOSTIC_REPLAY_STOP_REVIEW":
            return False, "Completed Causal Diagnostic Replay STOP_REVIEW required"
        if r.get("replay_result_sha256") != EARLY_CAUSAL_ENTRY_RESEARCH_SPEC["required_causal_replay_result_sha256"]:
            return False, "Causal replay result provenance mismatch"
        if r.get("replay_spec_sha256") != EARLY_CAUSAL_ENTRY_RESEARCH_SPEC["required_causal_replay_spec_sha256"]:
            return False, "Causal replay spec provenance mismatch"
        if r.get("source_frozen_model_sha256") != EARLY_CAUSAL_ENTRY_RESEARCH_SPEC["required_frozen_model_sha256"]:
            return False, "Frozen model provenance mismatch"
        if r.get("model_mutated") is not False or r.get("thresholds_recalibrated") is not False:
            return False, "Replay immutability provenance failed"
        return True, "allowed"

    def freeze_early_causal_entry_research_protocol(self):
        ok, why = self._early_causal_entry_research_protocol_gate()
        if not ok:
            return False, why
        old = self.redis.get_json(self.early_causal_entry_research_key("report"), None)
        if isinstance(old, dict) and old.get("status") == "COMPLETED":
            return False, "already_frozen"
        report = {
            "version": VERSION, "build": BUILD,
            "research_id": EARLY_CAUSAL_ENTRY_RESEARCH_SPEC["research_id"],
            "research_spec": EARLY_CAUSAL_ENTRY_RESEARCH_SPEC,
            "research_spec_sha256": EARLY_CAUSAL_ENTRY_RESEARCH_SHA256,
            "status": "COMPLETED", "phase": "EARLY_CAUSAL_ENTRY_RESEARCH_PROTOCOL_FROZEN_STOP_REVIEW",
            "source_causal_replay_result_sha256": EARLY_CAUSAL_ENTRY_RESEARCH_SPEC["required_causal_replay_result_sha256"],
            "source_frozen_model_sha256": EARLY_CAUSAL_ENTRY_RESEARCH_SPEC["required_frozen_model_sha256"],
            "new_post_2026_08_31_data_read": False, "alpaca_requests_made": 0,
            "model_mutated": False, "thresholds_recalibrated": False,
            "entry_stop_exit_rules_selected": False, "fresh_oos_preserved": True,
            "stop_and_review_required": True, "completed_at": iso()
        }
        canon = dict(report); canon.pop("completed_at")
        report["research_protocol_artifact_sha256"] = hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.redis.set_json(self.early_causal_entry_research_key("report"), report)
        self.redis.set_json(self.early_causal_entry_research_key("status"), {
            "status": "COMPLETED", "phase": "EARLY_CAUSAL_ENTRY_RESEARCH_PROTOCOL_FROZEN_STOP_REVIEW",
            "message": "Early Causal Entry Research protocol frozen; fresh post-2026-08-31 OOS remains unread; STOP REVIEW before research execution",
            "research_id": EARLY_CAUSAL_ENTRY_RESEARCH_SPEC["research_id"], "alpaca_requests_made": 0,
            "new_post_2026_08_31_data_read": False, "updated_at": iso()
        })
        return True, "frozen"

    def early_causal_entry_exec_key(self, suffix: str) -> str:
        return self.key(f"early_causal_entry_exec:v1:{suffix}")

    def early_causal_entry_threshold_amendment_key(self, suffix: str) -> str:
        return self.key(f"early_causal_entry_threshold_amendment:v1:{suffix}")

    def _early_causal_entry_threshold_amendment_gate(self):
        if not self.redis.configured:return False,"Redis required"
        fr=self.redis.get_json(self.early_causal_entry_research_key("report"),{}) or {}
        if fr.get("status")!="COMPLETED" or fr.get("phase")!="EARLY_CAUSAL_ENTRY_RESEARCH_PROTOCOL_FROZEN_STOP_REVIEW":return False,"Frozen Early Causal Entry research protocol required"
        if fr.get("research_spec_sha256")!=EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SPEC["required_research_spec_sha256"]:return False,"Research spec SHA mismatch"
        if fr.get("research_protocol_artifact_sha256")!=EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SPEC["required_protocol_artifact_sha256"]:return False,"Research protocol artifact SHA mismatch"
        existing_exec=self.redis.get_json(self.early_causal_entry_exec_key("status"),{}) or {}
        completed=self.redis.get_json(self.early_causal_entry_exec_key("completed_sessions"),[]) or []
        report=self.redis.get_json(self.early_causal_entry_exec_key("report"),None)
        if existing_exec.get("status") not in (None,"IDLE") or completed or report:return False,"Execution already started; amendment freeze forbidden"
        return True,"allowed"

    def freeze_early_causal_entry_threshold_amendment(self):
        ok,why=self._early_causal_entry_threshold_amendment_gate()
        if not ok:return False,why
        old=self.redis.get_json(self.early_causal_entry_threshold_amendment_key("report"),None)
        if old:
            if old.get("amendment_spec_sha256")==EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SHA256:return True,"already_frozen"
            return False,"Different amendment already frozen"
        report={"version":VERSION,"build":BUILD,"status":"COMPLETED","phase":"EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_FROZEN_STOP_REVIEW","amendment_spec":EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SPEC,"amendment_spec_sha256":EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SHA256,"research_execution_started":False,"alpaca_requests_made":0,"new_post_2026_08_31_data_read":False,"model_mutated":False,"thresholds_recalibrated_from_results":False,"completed_at":iso()}
        canon=dict(report);canon.pop("completed_at");report["amendment_artifact_sha256"]=hashlib.sha256(json.dumps(canon,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        self.redis.set_json(self.early_causal_entry_threshold_amendment_key("report"),report)
        return True,"frozen"

    def _set_early_causal_entry_exec_state(self, **updates: Any) -> None:
        with self.early_causal_entry_exec_lock:
            self.early_causal_entry_exec_state.update(updates); self.early_causal_entry_exec_state["updated_at"] = iso(); snap=dict(self.early_causal_entry_exec_state)
        if self.redis.configured: self.redis.set_json(self.early_causal_entry_exec_key("status"),snap)

    def _early_causal_entry_exec_gate(self):
        if not self.redis.configured:return False,"Redis required"
        amend=self.redis.get_json(self.early_causal_entry_threshold_amendment_key("report"),{}) or {}
        if amend.get("status")!="COMPLETED" or amend.get("phase")!="EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_FROZEN_STOP_REVIEW":return False,"Frozen threshold-family amendment required before execution"
        if amend.get("amendment_spec_sha256")!=EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SHA256:return False,"Threshold amendment SHA mismatch"
        if amend.get("research_execution_started") is not False or amend.get("new_post_2026_08_31_data_read") is not False:return False,"Threshold amendment timing/OOS lock failed"
        fr=self.redis.get_json(self.early_causal_entry_research_key("report"),{}) or {}
        if fr.get("status")!="COMPLETED" or fr.get("phase")!="EARLY_CAUSAL_ENTRY_RESEARCH_PROTOCOL_FROZEN_STOP_REVIEW":return False,"Frozen Early Causal Entry research protocol required"
        if fr.get("research_spec_sha256")!=EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_research_spec_sha256"]:return False,"Research spec SHA mismatch"
        if fr.get("research_protocol_artifact_sha256")!=EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_protocol_artifact_sha256"]:return False,"Research protocol artifact SHA mismatch"
        if fr.get("source_causal_replay_result_sha256")!=EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_causal_replay_result_sha256"]:return False,"Causal replay provenance mismatch"
        if fr.get("source_frozen_model_sha256")!=EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_frozen_model_sha256"]:return False,"Frozen model provenance mismatch"
        if fr.get("fresh_oos_preserved") is not True or fr.get("new_post_2026_08_31_data_read") is not False:return False,"Fresh OOS lock failed"
        m=self.redis.get_json(self.feature_scoring_freeze_key("report"),{}) or {}
        if m.get("frozen_model_sha256")!=EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_frozen_model_sha256"]:return False,"Runtime frozen model mismatch"
        rr=self.redis.get_json(self.causal_diagnostic_replay_key("report"),{}) or {}
        if rr.get("replay_result_sha256")!=EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_causal_replay_result_sha256"]:return False,"Runtime replay result mismatch"
        return True,"allowed"

    @staticmethod
    def _ece_symbol_equal_rate(rows, cls, threshold):
        by={}
        for x in rows:
            if x.get("class")!=cls:continue
            sym=str(x.get("symbol") or "")
            by.setdefault(sym,[]).append(1.0 if isinstance(x.get("score"),(int,float)) and x["score"]>=threshold else 0.0)
        vals=[sum(v)/len(v) for v in by.values() if v]
        return (sum(vals)/len(vals) if vals else None),len(vals)

    def early_causal_entry_exec_loop(self):
        try:
            ok,why=self._early_causal_entry_exec_gate()
            if not ok:raise RuntimeError(why)
            model=self.redis.get_json(self.feature_scoring_freeze_key("report"),{}) or {}; roles=model.get("roles") or {}; early=roles.get("early_core") or []
            if not early:raise RuntimeError("Frozen Early Core definitions missing")
            sessions=sorted(str(x) for x in (self.redis.get_json(self.causal_diagnostic_replay_key("completed_sessions"),[]) or []) if "2019-01-02"<=str(x)<="2026-08-31")
            done=set(self.redis.get_json(self.early_causal_entry_exec_key("completed_sessions"),[]) or []);self.early_causal_entry_exec_stop_event.clear();calls=int(self.redis.get_json(self.early_causal_entry_exec_key("alpaca_bar_calls"),0) or 0)
            self._set_early_causal_entry_exec_state(status="RUNNING",phase="EARLY_CAUSAL_ENTRY_CHECKPOINT_REPLAY",message=f"Early causal checkpoint replay {len(done)}/{len(sessions)}",total_sessions=len(sessions),sessions_scanned=len(done),alpaca_bar_calls_made=calls,new_post_2026_08_31_data_read=False)
            cps=EARLY_CAUSAL_ENTRY_EXEC_SPEC["checkpoints_minutes_before_frozen_early_core"]
            for sess in sessions:
                if sess in done:continue
                if self.early_causal_entry_exec_stop_event.is_set():self._set_early_causal_entry_exec_state(status="PAUSED",phase="EARLY_CAUSAL_ENTRY_CHECKPOINT_REPLAY",message="Pause requested; resume-safe checkpoint preserved",total_sessions=len(sessions),sessions_scanned=len(done),alpaca_bar_calls_made=calls,new_post_2026_08_31_data_read=False);return
                target=date.fromisoformat(sess);src=self.redis.get_json(self.causal_diagnostic_replay_key(f"results:{sess}"),[]) or []
                src=[x for x in src if x.get("class") in {"positive","hard_negative"} and x.get("signal_ts") and x.get("symbol")]
                syms=sorted({str(x["symbol"]).upper() for x in src});start,end=self._probe_cycle_bounds(target)
                rows5=self._fd_fetch_session_rows(syms,target,start,end) if syms else {}
                # logical Alpaca bar-call count: SIP plus BOATS (when structurally available) per <=200-symbol batch
                batches=(len(syms)+199)//200;calls+=batches*(1 if target < date.fromisoformat(HISTORICAL_CENSUS_SPEC["boats_launch_date"]) else 2);self.redis.set_json(self.early_causal_entry_exec_key("alpaca_bar_calls"),calls)
                obs=[]
                for x in src:
                    try:base=datetime.fromisoformat(str(x["signal_ts"]).replace("Z","+00:00"))
                    except:continue
                    sym=str(x["symbol"]).upper();bars=rows5.get(sym,[])
                    for cp in cps:
                        ev=base-timedelta(minutes=int(cp));score,den=self._ctr_score_at(bars,ev,early)
                        price=None
                        for r in bars:
                            try:ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00"))+timedelta(minutes=5);c=float(r.get("c"))
                            except:continue
                            if ts<=ev and c>0:price=c
                        realized=None
                        b=x.get("event_baseline")
                        if isinstance(price,(int,float)) and isinstance(b,(int,float)) and float(b)>0:realized=(float(price)/float(b)-1)*100
                        obs.append({"class":x["class"],"symbol":sym,"year":sess[:4],"checkpoint_minutes":cp,"eval_ts":ev.isoformat().replace("+00:00","Z"),"score":score,"observed_weight":den,"scoreable":score is not None,"checkpoint_close":price,"event_t2":x.get("event_t2"),"percent_move_already_realized":realized})
                self.redis.set_json(self.early_causal_entry_exec_key(f"observations:{sess}"),obs);done.add(sess);self.redis.set_json(self.early_causal_entry_exec_key("completed_sessions"),sorted(done))
                if len(done)%10==0 or len(done)==len(sessions):self._set_early_causal_entry_exec_state(status="RUNNING",phase="EARLY_CAUSAL_ENTRY_CHECKPOINT_REPLAY",message=f"Early causal checkpoint replay {len(done)}/{len(sessions)}",current_session=sess,total_sessions=len(sessions),sessions_scanned=len(done),alpaca_bar_calls_made=calls,new_post_2026_08_31_data_read=False)
            # bounded finalization from persisted observations; no additional Alpaca calls
            allobs=[]
            for i,sess in enumerate(sessions,1):
                allobs.extend(self.redis.get_json(self.early_causal_entry_exec_key(f"observations:{sess}"),[]) or [])
                if i%100==0:self._set_early_causal_entry_exec_state(status="RUNNING",phase="EARLY_CAUSAL_ENTRY_FINALIZE",message=f"Finalizing persisted observations {i}/{len(sessions)}",total_sessions=len(sessions),sessions_scanned=len(sessions),finalize_sessions_scanned=i,alpaca_bar_calls_made=calls,new_post_2026_08_31_data_read=False)
            base_thr=float(EARLY_CAUSAL_ENTRY_RESEARCH_SPEC["baseline"]["threshold"]);mults=EARLY_CAUSAL_ENTRY_EXEC_SPEC["candidate_family"]["threshold_multipliers_of_frozen_early_core"];attempts=[];selected=None
            for cp in cps:
                cr=[x for x in allobs if int(x.get("checkpoint_minutes") or -1)==cp];pos=[x for x in cr if x.get("class")=="positive"];hn=[x for x in cr if x.get("class")=="hard_negative"]
                pos_score=[x for x in pos if x.get("scoreable")];hn_score=[x for x in hn if x.get("scoreable")]
                for mult in sorted(mults,reverse=True):
                    thr=base_thr*float(mult);pp=sum(1 for x in pos if isinstance(x.get("score"),(int,float)) and x["score"]>=thr);hp=sum(1 for x in hn if isinstance(x.get("score"),(int,float)) and x["score"]>=thr);pr=pp/len(pos) if pos else None;he=hp/len(hn) if hn else None;hs,hsn=self._ece_symbol_equal_rate(hn,"hard_negative",thr)
                    pys={x["symbol"] for x in pos if isinstance(x.get("score"),(int,float)) and x["score"]>=thr};year={}
                    same=0
                    for y in [str(z) for z in range(2019,2027)]:
                        py=[x for x in pos if x.get("year")==y];hy=[x for x in hn if x.get("year")==y];pyr=sum(1 for x in py if isinstance(x.get("score"),(int,float)) and x["score"]>=thr)/len(py) if py else None;hyr=sum(1 for x in hy if isinstance(x.get("score"),(int,float)) and x["score"]>=thr)/len(hy) if hy else None;sd=bool(pyr is not None and hyr is not None and pyr>hyr);same+=1 if sd else 0;year[y]={"positive_pass_rate":pyr,"hard_negative_event_pass_rate":hyr,"same_direction":sd}
                    support=pp>=EARLY_CAUSAL_ENTRY_EXEC_SPEC["gates"]["min_positive_events"] and len(pys)>=EARLY_CAUSAL_ENTRY_EXEC_SPEC["gates"]["min_positive_symbols"]
                    stable=same>=EARLY_CAUSAL_ENTRY_EXEC_SPEC["gates"]["min_same_direction_years"] and year.get("2025",{}).get("same_direction") and year.get("2026",{}).get("same_direction")
                    passed=bool(pr is not None and pr>=.20 and hs is not None and hs<=.10 and support and stable)
                    a={"checkpoint_minutes":cp,"threshold_multiplier":mult,"threshold":thr,"positive_total":len(pos),"positive_scoreable":len(pos_score),"positive_pass_events":pp,"positive_recall":pr,"positive_pass_symbols":len(pys),"hard_negative_total":len(hn),"hard_negative_scoreable":len(hn_score),"hard_negative_event_pass_rate":he,"hard_negative_equal_symbol_pass_rate":hs,"hard_negative_symbols":hsn,"same_direction_years":same,"by_year":year,"support_gate":support,"stability_gate":stable,"live_expressible":True,"classification":"PASS" if passed else "FAIL"};attempts.append(a)
                passing=[a for a in attempts if a["checkpoint_minutes"]==cp and a["classification"]=="PASS"]
                if passing and selected is None:selected=sorted(passing,key=lambda a:a["threshold"],reverse=True)[0]
            report={"version":VERSION,"build":BUILD,"execution_id":EARLY_CAUSAL_ENTRY_EXEC_SPEC["execution_id"],"execution_spec_sha256":EARLY_CAUSAL_ENTRY_EXEC_SHA256,"source_research_spec_sha256":EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_research_spec_sha256"],"source_protocol_artifact_sha256":EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_protocol_artifact_sha256"],"source_causal_replay_result_sha256":EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_causal_replay_result_sha256"],"source_frozen_model_sha256":EARLY_CAUSAL_ENTRY_EXEC_SPEC["required_frozen_model_sha256"],"status":"COMPLETED","phase":"EARLY_CAUSAL_ENTRY_RESEARCH_STOP_REVIEW","sessions":len(sessions),"first_session":sessions[0] if sessions else None,"last_session":sessions[-1] if sessions else None,"cohort":"persisted causal replay signals","attempted_rules":attempts,"selected_candidate":selected,"research_classification":"PASS" if selected else "FAIL","fresh_oos_preserved":True,"new_post_2026_08_31_data_read":False,"model_features_directions_weights_mutated":False,"entry_stop_exit_rules_selected":False,"profitability_used_for_selection":False,"alpaca_bar_calls_made":calls,"stop_and_review_required":True,"completed_at":iso()}
            canon=dict(report);canon.pop("completed_at");report["result_sha256"]=hashlib.sha256(json.dumps(canon,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest();self.redis.set_json(self.early_causal_entry_exec_key("report"),report);self._set_early_causal_entry_exec_state(status="COMPLETED",phase="EARLY_CAUSAL_ENTRY_RESEARCH_STOP_REVIEW",message="Early Causal Entry Development research completed; STOP REVIEW before any fresh OOS",total_sessions=len(sessions),sessions_scanned=len(sessions),alpaca_bar_calls_made=calls,new_post_2026_08_31_data_read=False,research_classification=report["research_classification"],stop_and_review_required=True)
        except Exception as e:
            logging.exception("Early causal entry execution failed");self._set_early_causal_entry_exec_state(status="ERROR",phase="EARLY_CAUSAL_ENTRY_RESEARCH_BLOCKED",message="Early causal entry research failed closed",last_error=f"{type(e).__name__}: {e}",new_post_2026_08_31_data_read=False)
        finally:
            with self.early_causal_entry_exec_lock:self.early_causal_entry_exec_thread=None

    def start_early_causal_entry_exec(self):
        ok,why=self._early_causal_entry_exec_gate()
        if not ok:return False,why
        old=self.redis.get_json(self.early_causal_entry_exec_key("report"),None)
        if isinstance(old,dict) and old.get("status")=="COMPLETED":return False,"already_completed"
        with self.early_causal_entry_exec_lock:
            if self.early_causal_entry_exec_thread and self.early_causal_entry_exec_thread.is_alive():return False,"already_running"
            self.early_causal_entry_exec_thread=threading.Thread(target=self.early_causal_entry_exec_loop,name="early-causal-entry-research",daemon=True);self.early_causal_entry_exec_thread.start()
        return True,"started"

    def phase0a_key(self, suffix: str) -> str:
        return self.key(f"phase0a:v1:{suffix}")

    def _set_phase0a_state(self, **updates: Any) -> None:
        with self.phase0a_lock:
            self.phase0a_state.update(updates); self.phase0a_state["updated_at"] = iso(); snapshot = dict(self.phase0a_state)
        if self.redis.configured:
            self.redis.set_json(self.phase0a_key("status"), snapshot)

    def _phase0a_gate(self) -> tuple[bool, str]:
        if not self.redis.configured: return False, "Redis is required for fail-closed Phase 0A"
        report = self.redis.get_json(self.phase0_probe_key("report"), None)
        if not report or not report.get("probe_passed") or not report.get("phase0a_allowed"):
            return False, "Phase 0 capability probe is not PASSED in Redis"
        if str(report.get("probe_sha256")) != PHASE0_PROBE_SHA256:
            return False, "Stored capability probe does not match the frozen v1.6.6/v1.6.7 probe"
        return True, "allowed"

    @staticmethod
    def _phase0a_coarse_event(rows: list[dict[str, Any]], threshold_pct: float = 20.0) -> dict[str, Any] | None:
        threshold = 1.0 + threshold_pct / 100.0; running_min = None; running_min_ts = None
        for row in sorted(rows, key=lambda x: str(x.get("t") or "")):
            try: lo, hi = float(row.get("l")), float(row.get("h"))
            except (TypeError, ValueError): continue
            if not (math.isfinite(lo) and math.isfinite(hi) and lo > 0 and hi > 0): continue
            ts = str(row.get("t") or "")
            if running_min is None or lo < running_min: running_min, running_min_ts = lo, ts
            if running_min and hi >= running_min * threshold:
                return {"t1_coarse": running_min_ts, "t1_low": running_min, "t2_coarse": ts, "t2_high": hi,
                        "coarse_gain_pct": (hi / running_min - 1.0) * 100.0, "same_bar_order_ambiguous": running_min_ts == ts}
        return None

    @staticmethod
    def _phase0a_split_suspect(rows: list[dict[str, Any]]) -> dict[str, Any]:
        # Conservative coarse screen only. A >3.5x adjacent-bar discontinuity is flagged; Phase 0B performs strict corporate-action exclusion.
        ordered = sorted(rows, key=lambda x: str(x.get("t") or "")); prev = None
        for row in ordered:
            try: close = float(row.get("c"))
            except (TypeError, ValueError): continue
            if close <= 0: continue
            if prev and max(close / prev, prev / close) >= 3.5:
                return {"suspect": True, "reason": "adjacent_coarse_close_ratio_ge_3.5", "ratio": max(close / prev, prev / close), "t": str(row.get("t") or "")}
            prev = close
        return {"suspect": False}

    def _phase0a_fetch_cycle(self, symbols: list[str], target: date) -> dict[str, list[dict[str, Any]]]:
        start, end = self._probe_cycle_bounds(target); out = {s: [] for s in symbols}
        for i in range(0, len(symbols), 200):
            batch = symbols[i:i+200]
            sip = self.alpaca.bars(batch, start, end, feed="sip", adjustment="raw", timeframe="30Min")
            boats = self.alpaca.bars(batch, start, end, feed="boats", adjustment="raw", timeframe="30Min")
            for symbol in batch:
                merged = {}
                for source, source_rows in (("sip", sip.get(symbol, [])), ("boats", boats.get(symbol, []))):
                    for row in source_rows:
                        ts = str(row.get("t") or "")
                        if not ts: continue
                        session = self._probe_session(ts, target)
                        if source == "boats" and session != "Overnight": continue
                        if ts not in merged or source == "sip": merged[ts] = {**row, "source": source}
                out[symbol] = [merged[k] for k in sorted(merged)]
        return out

    def phase0a_loop(self) -> None:
        try:
            allowed, reason = self._phase0a_gate()
            if not allowed: raise RuntimeError(reason)
            manifest = self.redis.get_json(str(PHASE0A_SPEC["source_manifest"]), {})
            symbols = sorted({str(x).upper() for x in manifest.get("symbols", []) if SYMBOL_RE.fullmatch(str(x).upper())})
            sessions = [str(x) for x in manifest.get("sessions", [])]
            if len(symbols) < 1000 or len(sessions) != 60: raise RuntimeError(f"Frozen full-universe manifest invalid: symbols={len(symbols)} sessions={len(sessions)}")
            completed = set(self.redis.get_json(self.phase0a_key("completed_sessions"), []) or [])
            total_candidates = int(self.redis.get_json(self.phase0a_key("candidate_count"), 0) or 0)
            self._set_phase0a_state(status="RUNNING", phase="CENSUS", message="Phase 0A coarse high-recall census", universe_count=len(symbols), session_count=len(sessions), completed_sessions=len(completed), candidate_count=total_candidates, phase0b_allowed=False)
            for idx, session in enumerate(sessions):
                if self.phase0a_stop_event.is_set():
                    self._set_phase0a_state(status="PAUSED", message="Phase 0A paused at a session boundary", phase0b_allowed=False); return
                if session in completed: continue
                target = date.fromisoformat(session); bars_by_symbol = self._phase0a_fetch_cycle(symbols, target); session_candidates = []
                quality = {"symbols_with_bars":0,"symbols_without_bars":0,"split_suspects":0,"same_bar_ambiguous":0}
                for symbol in symbols:
                    rows = bars_by_symbol.get(symbol, [])
                    if not rows: quality["symbols_without_bars"] += 1; continue
                    quality["symbols_with_bars"] += 1
                    event = self._phase0a_coarse_event(rows, float(PHASE0A_SPEC["primary_threshold_pct"]))
                    if not event: continue
                    split = self._phase0a_split_suspect(rows)
                    if split.get("suspect"): quality["split_suspects"] += 1
                    if event.get("same_bar_order_ambiguous"): quality["same_bar_ambiguous"] += 1
                    session_candidates.append({"symbol":symbol,"target_session":session,**event,"corporate_action_screen":split,"eligible_for_phase0b":not split.get("suspect"),"verified_ge20":False})
                self.redis.set_json(self.phase0a_key(f"candidates:{session}"), session_candidates)
                self.redis.set_json(self.phase0a_key(f"quality:{session}"), quality)
                completed.add(session); total_candidates += len(session_candidates)
                self.redis.set_json(self.phase0a_key("completed_sessions"), sorted(completed)); self.redis.set_json(self.phase0a_key("candidate_count"), total_candidates)
                self._set_phase0a_state(status="RUNNING", phase="CENSUS", message=f"Completed coarse census session {session}", current_session=session, completed_sessions=len(completed), remaining_sessions=len(sessions)-len(completed), candidate_count=total_candidates, last_session_candidates=len(session_candidates), last_session_quality=quality, phase0b_allowed=False)
            summary = {"version":VERSION,"build":BUILD,"census_id":PHASE0A_SPEC["census_id"],"phase0a_sha256":PHASE0A_SHA256,"probe_sha256":PHASE0_PROBE_SHA256,"status":"COMPLETED","universe_count":len(symbols),"sessions":len(sessions),"completed_sessions":len(completed),"coarse_candidates":total_candidates,"phase0a_is_candidate_census_only":True,"verified_ge20_count":0,"phase0b_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
            self.redis.set_json(self.phase0a_key("report"), summary)
            self._set_phase0a_state(status="COMPLETED", phase="STOP_REVIEW", message="Phase 0A completed; STOP and review counts before Phase 0B", candidate_count=total_candidates, completed_sessions=len(completed), phase0b_allowed=False, stop_and_review_required=True)
        except Exception as exc:
            logging.exception("Phase 0A census failed"); self._set_phase0a_state(status="ERROR", phase="BLOCKED", message="Phase 0A failed closed", phase0b_allowed=False, last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.phase0a_lock: self.phase0a_thread = None

    def start_phase0a(self) -> tuple[bool, str]:
        allowed, reason = self._phase0a_gate()
        if not allowed: return False, reason
        with self.phase0a_lock:
            if self.phase0a_thread and self.phase0a_thread.is_alive(): return False, "already_running"
            self.phase0a_stop_event.clear(); self.phase0a_thread = threading.Thread(target=self.phase0a_loop, name="independent-priority-phase0a", daemon=True); self.phase0a_thread.start()
        return True, "started"

    def phase0_reference_key(self, suffix: str) -> str:
        return self.key(f"phase0_reference_discovery:v1:{suffix}")

    def _set_phase0_reference_state(self, **updates: Any) -> None:
        with self.phase0_reference_lock:
            self.phase0_reference_state.update(updates)
            self.phase0_reference_state["updated_at"] = iso()
            snapshot = dict(self.phase0_reference_state)
        if self.redis.configured:
            self.redis.set_json(self.phase0_reference_key("status"), snapshot)

    @staticmethod
    def _reference_phase_name(value: Any) -> str:
        text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        if text in {"OVERNIGHT", "BOATS"}: return "Overnight"
        if text in {"PREMARKET", "PRE_MARKET", "PM"}: return "Premarket"
        return ""

    @staticmethod
    def _compact_raw_bar(row: dict[str, Any]) -> dict[str, Any]:
        return {k: row.get(k) for k in ("t", "o", "h", "l", "c", "v") if row.get(k) is not None}

    def phase0_reference_discovery_loop(self) -> None:
        try:
            self._set_phase0_reference_state(status="RUNNING", message="Reading frozen NDR explosion catalog; detector is not used for selection", phase0a_allowed=False)
            catalog = self.redis.get_json(f"{self.source_prefix}:explosions:catalog", None)
            if not isinstance(catalog, dict) or not isinstance(catalog.get("cases"), list):
                raise RuntimeError(f"Missing historical catalog: {self.source_prefix}:explosions:catalog")
            pools = {"Overnight": [], "Premarket": []}
            seen = set()
            for item in catalog.get("cases") or []:
                phase = self._reference_phase_name(item.get("phase"))
                if phase not in pools or float(item.get("mfe_pct") or 0.0) < 20.0:
                    continue
                key = (phase, str(item.get("session")), str(item.get("symbol")))
                if key in seen: continue
                seen.add(key)
                pools[phase].append(dict(item))
            for phase in pools:
                pools[phase].sort(key=lambda x: -float(x.get("mfe_pct") or 0.0))
                pools[phase] = pools[phase][:8]
            detailed = {"Overnight": [], "Premarket": []}
            for phase, candidates in pools.items():
                for item in candidates:
                    target = date.fromisoformat(str(item["session"]))
                    start, end = self._probe_cycle_bounds(target)
                    symbol = str(item["symbol"]).upper()
                    sip = self.alpaca.bars([symbol], start, end, feed="sip", adjustment="raw", timeframe="1Min").get(symbol, [])
                    boats = self.alpaca.bars([symbol], start, end, feed="boats", adjustment="raw", timeframe="1Min").get(symbol, [])
                    merged = {}
                    source = {}
                    for feed, rows in (("sip", sip), ("boats", boats)):
                        for row in rows:
                            ts = str(row.get("t") or "")
                            if not ts: continue
                            sess = self._probe_session(ts, target)
                            if feed == "boats" and sess != "Overnight": continue
                            if ts not in merged or feed == "sip":
                                merged[ts] = row; source[ts] = feed
                    session_rows = []
                    for ts in sorted(merged):
                        if self._probe_session(ts, target) == phase:
                            r = self._compact_raw_bar(merged[ts]); r["source"] = source.get(ts); session_rows.append(r)
                    detailed[phase].append({
                        "symbol": symbol, "target_session": target.isoformat(), "catalog_phase": phase,
                        "catalog_signal_ts": item.get("signal_ts"), "catalog_mfe_pct": item.get("mfe_pct"),
                        "catalog_signal_type": item.get("signal_type"), "raw_bar_count": len(session_rows),
                        "raw_session_bars": session_rows,
                    })
            report = {
                "version": VERSION, "build": BUILD, "source_prefix": self.source_prefix,
                "purpose": "Independent visual reference-candidate pack before freezing final Overnight/Premarket probe cases",
                "selection_rule": "Frozen NDR explosion catalog only: catalog phase is Overnight/Premarket and stored MFE >=20%; top 8 unique symbol-sessions by stored MFE per phase",
                "selection_uses_session_detector": False,
                "warning": "Do not freeze a reference from catalog metadata alone. Inspect raw_session_bars first; only after manual freeze may session_detector be run.",
                "phase0a_allowed": False, "candidate_counts": {k: len(v) for k,v in detailed.items()},
                "candidates": detailed, "completed_at": iso(),
            }
            self.phase0_reference_report = report
            self.redis.set_json(self.phase0_reference_key("report"), report)
            self._set_phase0_reference_state(status="COMPLETED", message="Candidate pack ready for independent raw-bar inspection", phase0a_allowed=False, candidate_counts=report["candidate_counts"])
        except Exception as exc:
            logging.exception("Phase 0 reference discovery failed")
            self._set_phase0_reference_state(status="ERROR", message="Reference discovery failed", phase0a_allowed=False, last_error=f"{type(exc).__name__}: {exc}")
        finally:
            with self.phase0_reference_lock: self.phase0_reference_thread = None

    def start_phase0_reference_discovery(self) -> tuple[bool, str]:
        if not (self.redis.configured and self.alpaca.configured): return False, "Redis and Alpaca credentials are required"
        with self.phase0_reference_lock:
            if self.phase0_reference_thread and self.phase0_reference_thread.is_alive(): return False, "already_running"
            self.phase0_reference_state = {"status":"STARTING","message":"Starting independent reference-candidate discovery","selection_uses_session_detector":False,"phase0a_allowed":False,"updated_at":iso()}
            self.phase0_reference_thread = threading.Thread(target=self.phase0_reference_discovery_loop, name="ipr-phase0-reference-discovery", daemon=True)
            self.phase0_reference_thread.start()
        return True, "started"

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
                for thread in (self.audit_thread, self.export_thread, self.early_thread, self.breakout_thread)
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
                for thread in (self.audit_thread, self.export_thread, self.early_thread, self.orb_thread)
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

    def causal_diagnostic_replay_key(self,suffix): return self.key(f"causal_diagnostic_replay:v1:{suffix}")
    def _set_causal_diagnostic_replay_state(self,**u):
        with self.causal_diagnostic_replay_lock:
            self.causal_diagnostic_replay_state.update(u);self.causal_diagnostic_replay_state["updated_at"]=iso();x=dict(self.causal_diagnostic_replay_state)
        if self.redis.configured:self.redis.set_json(self.causal_diagnostic_replay_key("status"),x)
    def _causal_diagnostic_replay_gate(self):
        if not (self.redis.configured and self.alpaca.configured):return False,"Redis and Alpaca are required"
        pr=self.redis.get_json(self.causal_translation_protocol_key("report"),None)
        if not isinstance(pr,dict) or pr.get("status")!="COMPLETED" or pr.get("phase")!="CAUSAL_TRANSLATION_PROTOCOL_FROZEN_STOP_REVIEW":return False,"Frozen v1.7.22 translation protocol required"
        if pr.get("translation_spec_sha256")!=CAUSAL_DIAGNOSTIC_REPLAY_SPEC["required_translation_spec_sha256"] or pr.get("translation_protocol_artifact_sha256")!=CAUSAL_DIAGNOSTIC_REPLAY_SPEC["required_translation_protocol_artifact_sha256"]:return False,"Translation protocol SHA mismatch"
        if pr.get("source_frozen_model_sha256")!=CAUSAL_DIAGNOSTIC_REPLAY_SPEC["required_frozen_model_sha256"]:return False,"Frozen model SHA mismatch"
        old=self.redis.get_json(self.causal_diagnostic_replay_key("report"),None)
        if isinstance(old,dict) and old.get("status")=="COMPLETED":return False,"completed_replay_exists_rerun_prohibited"
        return True,"allowed"
    def _ctr_fetch_1m(self,symbols,target,start,end):
        merged={sym:{} for sym in symbols}
        for off in range(0,len(symbols),200):
            batch=symbols[off:off+200];sip=self.alpaca.bars(batch,start,end,feed="sip",adjustment="raw",timeframe="1Min")
            boats={} if target < date.fromisoformat(HISTORICAL_CENSUS_SPEC["boats_launch_date"]) else self.alpaca.bars(batch,start,end,feed="boats",adjustment="raw",timeframe="1Min")
            for sym in batch:
                d=merged[sym]
                for row in boats.get(sym,[]) or []:
                    ts=str(row.get("t") or "")
                    if ts and self._probe_session(ts,target)=="Overnight":d[ts]={**row,"source":"boats","session":"Overnight"}
                for row in sip.get(sym,[]) or []:
                    ts=str(row.get("t") or "")
                    if ts:d[ts]={**row,"source":"sip","session":self._probe_session(ts,target)}
        return {sym:[d[k] for k in sorted(d)] for sym,d in merged.items()}
    @staticmethod
    def _ctr_score_at(rows,eval_dt,defs):
        num=den=0.0
        for d in defs:
            f=IndependentPriorityRadar._fd_features(rows,eval_dt-timedelta(minutes=int(d["anchor_minutes"])))
            v=(f or {}).get(d["feature"])
            if not isinstance(v,(int,float)) or not math.isfinite(v):continue
            z=d["direction"]*(float(v)-d["hard_negative_center"])/d["pooled_symbol_sd"];num+=d["weight"]*max(-3,min(3,z));den+=d["weight"]
        return (num/den,den) if den>=.5 else (None,den)
    def _ctr_first_signal(self,rows,early_defs,early_thr,confirm_defs,confirm_thr):
        parsed=[]
        for r in rows:
            try:ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00"));c=float(r.get("c"))
            except:continue
            if c>0:parsed.append((ts,c))
        parsed.sort(); first=None;confirm=None;early_scoreable=False;confirmation_scoreable=False
        for ts,c in parsed:
            ev=ts+timedelta(minutes=5);sc,den=self._ctr_score_at(rows,ev,early_defs)
            if sc is not None:early_scoreable=True
            if first is None and sc is not None and sc>=early_thr:first=(ev,c,sc,den)
            if first is not None and confirm is None and ev>=first[0]:
                cs,cd=self._ctr_score_at(rows,ev,confirm_defs)
                if cs is not None:confirmation_scoreable=True
                if cs is not None and cs>=confirm_thr:confirm=(ev,cs,cd)
            if first is not None and confirm is not None:break
        return first,confirm,early_scoreable,confirmation_scoreable
    @staticmethod
    def _ctr_forward(rows,signal_dt,signal_close,horizons):
        pts=[]
        for r in rows:
            try:ts=datetime.fromisoformat(str(r.get("t") or "").replace("Z","+00:00"));h=float(r.get("h"));l=float(r.get("l"));c=float(r.get("c"))
            except:continue
            if ts>=signal_dt and min(h,l,c)>0:pts.append((ts,h,l,c))
        pts.sort();out={}
        for hm in horizons:
            end=signal_dt+timedelta(minutes=hm);a=[x for x in pts if x[0]<end]
            if not a:out[str(hm)]=None;continue
            im=max(range(len(a)),key=lambda i:a[i][1]);out[str(hm)]={"MFE_pct_from_signal_close":(a[im][1]/signal_close-1)*100,"MAE_pct_from_signal_close":(min(x[2] for x in a)/signal_close-1)*100,"close_return_pct":(a[-1][3]/signal_close-1)*100,"time_to_MFE_minutes":max(0.0,(a[im][0]-signal_dt).total_seconds()/60.0)}
        if pts:
            im=max(range(len(pts)),key=lambda i:pts[i][1]);out["end_of_trading_cycle"]={"MFE_pct_from_signal_close":(pts[im][1]/signal_close-1)*100,"MAE_pct_from_signal_close":(min(x[2] for x in pts)/signal_close-1)*100,"close_return_pct":(pts[-1][3]/signal_close-1)*100,"time_to_MFE_minutes":max(0.0,(pts[im][0]-signal_dt).total_seconds()/60.0)}
        else:out["end_of_trading_cycle"]=None
        return out
    @staticmethod
    def _ctr_dist(vals):
        a=np.asarray([float(x) for x in vals if isinstance(x,(int,float)) and math.isfinite(x)],dtype=float)
        if not len(a):return {"n":0,"mean":None,"quantiles":{}}
        qs=[.1,.25,.5,.75,.9];return {"n":len(a),"mean":float(a.mean()),"quantiles":{str(q):float(np.quantile(a,q)) for q in qs}}
    def _ctr_rescue_gate(self):
        """Read-only gate for finalization rescue. Never requires or calls Alpaca."""
        if not self.redis.configured:return False,"Redis is required"
        old=self.redis.get_json(self.causal_diagnostic_replay_key("report"),None)
        if isinstance(old,dict) and old.get("status")=="COMPLETED":return False,"completed_replay_exists_rescue_not_needed"
        sessions=sorted(str(x) for x in (self.redis.get_json(self.phase0b_full_key("completed_sessions"),[]) or []) if "2019-"<=str(x)<="2026-08-31")
        done=set(str(x) for x in (self.redis.get_json(self.causal_diagnostic_replay_key("completed_sessions"),[]) or []))
        missing=[x for x in sessions if x not in done]
        if not sessions:return False,"no_source_sessions"
        if missing:return False,f"replay_incomplete_missing_{len(missing)}_sessions"
        pr=self.redis.get_json(self.causal_translation_protocol_key("report"),{}) or {};m=self.redis.get_json(self.feature_scoring_freeze_key("report"),{}) or {}
        if pr.get("translation_spec_sha256")!=CAUSAL_DIAGNOSTIC_REPLAY_SPEC["required_translation_spec_sha256"]:return False,"translation_spec_sha_mismatch"
        if pr.get("translation_protocol_artifact_sha256")!=CAUSAL_DIAGNOSTIC_REPLAY_SPEC["required_translation_protocol_artifact_sha256"]:return False,"translation_artifact_sha_mismatch"
        if m.get("frozen_model_sha256")!=CAUSAL_DIAGNOSTIC_REPLAY_SPEC["required_frozen_model_sha256"]:return False,"frozen_model_sha_mismatch"
        return True,"allowed"
    @staticmethod
    def _ctr_rescue_cols():
        hs=[str(x) for x in CAUSAL_DIAGNOSTIC_REPLAY_SPEC["horizons_minutes"]]+["end_of_trading_cycle"]
        mets=list(CAUSAL_DIAGNOSTIC_REPLAY_SPEC["forward_metrics"])
        def safe(x):return re.sub(r"[^A-Za-z0-9_]","_",str(x))
        return hs,mets,{(h,m):f"f_{safe(h)}_{safe(m)}" for h in hs for m in mets}
    def _ctr_rescue_dist_sql(self,con,where,params,col):
        vals=[]
        cur=con.execute(f'SELECT "{col}" FROM signals WHERE {where} AND "{col}" IS NOT NULL',params)
        while True:
            batch=cur.fetchmany(20000)
            if not batch:break
            vals.extend(float(x[0]) for x in batch if x and x[0] is not None and math.isfinite(float(x[0])))
        return self._ctr_dist(vals)
    def _ctr_rescue_summarize_sql(self,con,where,params,cls):
        hs,mets,cols=self._ctr_rescue_cols()
        signals=int(con.execute(f"SELECT COUNT(*) FROM signals WHERE {where}",params).fetchone()[0])
        symbols=int(con.execute(f"SELECT COUNT(DISTINCT symbol) FROM signals WHERE {where}",params).fetchone()[0])
        z={"signals":signals,"symbols":symbols,
           "percent_move_already_realized":self._ctr_rescue_dist_sql(con,where,params,"percent_move_already_realized"),
           "minutes_signal_to_plus20_confirmation":self._ctr_rescue_dist_sql(con,where,params,"minutes_signal_to_plus20_confirmation")}
        if cls=="positive":
            before=int(con.execute(f"SELECT COUNT(*) FROM signals WHERE {where} AND signal_before_plus20_confirmation=1",params).fetchone()[0])
            z["fraction_signal_before_plus20_confirmation"]=before/signals if signals else None
        z["forward"]={}
        for h in hs:
            mfe=cols[(h,"MFE_pct_from_signal_close")]
            n=int(con.execute(f'SELECT COUNT(*) FROM signals WHERE {where} AND "{mfe}" IS NOT NULL',params).fetchone()[0])
            block={m:self._ctr_rescue_dist_sql(con,where,params,cols[(h,m)]) for m in mets}
            block["fraction_MFE_ge"]={str(t):(int(con.execute(f'SELECT COUNT(*) FROM signals WHERE {where} AND "{mfe}">=?',params+(t*100,)).fetchone()[0])/n if n else None) for t in [.05,.1,.2]}
            z["forward"][h]=block
        return z
    def causal_diagnostic_replay_finalize_rescue_loop(self):
        dbpath=None
        try:
            ok,why=self._ctr_rescue_gate()
            if not ok:raise RuntimeError(why)
            pr=self.redis.get_json(self.causal_translation_protocol_key("report"),{}) or {};m=self.redis.get_json(self.feature_scoring_freeze_key("report"),{}) or {}
            sessions=sorted(str(x) for x in (self.redis.get_json(self.phase0b_full_key("completed_sessions"),[]) or []) if "2019-"<=str(x)<="2026-08-31")
            self._set_causal_diagnostic_replay_state(status="RUNNING",phase="CAUSAL_DIAGNOSTIC_FINALIZATION_RESCUE",message="Read-only finalization rescue: spooling persisted replay results; Alpaca disabled",post_signal_paths_read=True,total_sessions=len(sessions),sessions_scanned=len(sessions),finalize_sessions_scanned=0,alpaca_requests_made=0)
            fd,dbpath=tempfile.mkstemp(prefix="ipr_ctr_finalize_",suffix=".sqlite3");os.close(fd);con=sqlite3.connect(dbpath)
            hs,mets,cols=self._ctr_rescue_cols();flat=list(cols.values())
            con.execute("PRAGMA journal_mode=OFF");con.execute("PRAGMA synchronous=OFF");con.execute("PRAGMA temp_store=FILE")
            defs=["class TEXT","symbol TEXT","signal_ts TEXT","year TEXT","phase TEXT","percent_move_already_realized REAL","minutes_signal_to_plus20_confirmation REAL","signal_before_plus20_confirmation INTEGER"]+[f'"{c}" REAL' for c in flat]
            con.execute("CREATE TABLE signals ("+",".join(defs)+")")
            names=["class","symbol","signal_ts","year","phase","percent_move_already_realized","minutes_signal_to_plus20_confirmation","signal_before_plus20_confirmation"]+flat
            q="INSERT INTO signals ("+",".join('"'+x+'"' for x in names)+") VALUES ("+",".join("?" for _ in names)+")"
            for i,sess in enumerate(sessions,1):
                rows=self.redis.get_json(self.causal_diagnostic_replay_key(f"results:{sess}"),[]) or []
                batch=[]
                for x in rows:
                    f=x.get("forward") or {};base=[x.get("class"),x.get("symbol"),x.get("signal_ts"),str(x.get("signal_ts") or "")[:4],x.get("phase"),x.get("percent_move_already_realized"),x.get("minutes_signal_to_plus20_confirmation"),1 if x.get("signal_before_plus20_confirmation") else 0]
                    vals=[]
                    for h in hs:
                        a=f.get(h) or {}
                        for mm in mets:vals.append(a.get(mm))
                    batch.append(tuple(base+vals))
                if batch:con.executemany(q,batch)
                if i%25==0 or i==len(sessions):
                    con.commit();self._set_causal_diagnostic_replay_state(status="RUNNING",phase="CAUSAL_DIAGNOSTIC_FINALIZATION_RESCUE",message=f"Finalization rescue spool {i}/{len(sessions)} sessions",post_signal_paths_read=True,total_sessions=len(sessions),sessions_scanned=len(sessions),finalize_sessions_scanned=i,finalize_stage="SPOOL",alpaca_requests_made=0)
            con.commit();con.execute("CREATE INDEX idx_cls ON signals(class)");con.execute("CREATE INDEX idx_year_cls ON signals(year,class)");con.execute("CREATE INDEX idx_phase_cls ON signals(phase,class)");con.commit()
            byclass={}
            for j,c in enumerate(["positive","hard_negative"],1):
                self._set_causal_diagnostic_replay_state(status="RUNNING",phase="CAUSAL_DIAGNOSTIC_FINALIZATION_RESCUE",message=f"Finalization rescue summarize class {c}",finalize_stage="SUMMARY_CLASS",finalize_group=c,alpaca_requests_made=0)
                byclass[c]=self._ctr_rescue_summarize_sql(con,"class=?",(c,),c)
            byyear={}
            for y in range(2019,2027):
                self._set_causal_diagnostic_replay_state(status="RUNNING",phase="CAUSAL_DIAGNOSTIC_FINALIZATION_RESCUE",message=f"Finalization rescue summarize year {y}",finalize_stage="SUMMARY_YEAR",finalize_group=str(y),alpaca_requests_made=0)
                byyear[str(y)]={c:self._ctr_rescue_summarize_sql(con,"class=? AND year=?",(c,str(y)),c) for c in ["positive","hard_negative"]}
            byphase={}
            for ph in ["AH","Overnight","Premarket","Regular","Other"]:
                self._set_causal_diagnostic_replay_state(status="RUNNING",phase="CAUSAL_DIAGNOSTIC_FINALIZATION_RESCUE",message=f"Finalization rescue summarize phase {ph}",finalize_stage="SUMMARY_PHASE",finalize_group=ph,alpaca_requests_made=0)
                byphase[ph]={c:self._ctr_rescue_summarize_sql(con,"class=? AND phase=?",(c,ph),c) for c in ["positive","hard_negative"]}
            rawpos=rawhard=scorepos=scorehard=0
            for i,sess in enumerate(sessions,1):
                x=self.redis.get_json(self.causal_diagnostic_replay_key(f"stats:{sess}"),{}) or {};rawpos+=int(x.get("positive_selected") or 0);rawhard+=int(x.get("hard_negative_selected") or 0);scorepos+=int(x.get("positive_scoreable") or 0);scorehard+=int(x.get("hard_negative_scoreable") or 0)
                if i%100==0:self._set_causal_diagnostic_replay_state(status="RUNNING",phase="CAUSAL_DIAGNOSTIC_FINALIZATION_RESCUE",message=f"Finalization rescue stats {i}/{len(sessions)}",finalize_stage="STATS",finalize_sessions_scanned=i,alpaca_requests_made=0)
            report={"version":VERSION,"build":BUILD,"replay_id":CAUSAL_DIAGNOSTIC_REPLAY_SPEC["replay_id"],"replay_spec_sha256":CAUSAL_DIAGNOSTIC_REPLAY_SHA256,"translation_spec_sha256":pr.get("translation_spec_sha256"),"translation_protocol_artifact_sha256":pr.get("translation_protocol_artifact_sha256"),"source_frozen_model_sha256":m.get("frozen_model_sha256"),"status":"COMPLETED","phase":"CAUSAL_DIAGNOSTIC_REPLAY_STOP_REVIEW","sessions":len(sessions),"first_session":sessions[0] if sessions else None,"last_session":sessions[-1] if sessions else None,"positive_verified_events":rawpos,"hard_negative_events_selected":rawhard,"positive_events_ever_scoreable":scorepos,"hard_negative_events_ever_scoreable":scorehard,"positive_scoreability_rate":scorepos/rawpos if rawpos else None,"hard_negative_scoreability_rate":scorehard/rawhard if rawhard else None,"positive_signal_events":byclass["positive"]["signals"],"hard_negative_signal_events":byclass["hard_negative"]["signals"],"positive_signal_coverage":byclass["positive"]["signals"]/rawpos if rawpos else None,"hard_negative_signal_coverage":byclass["hard_negative"]["signals"]/rawhard if rawhard else None,"summary_by_class":byclass,"summary_by_year":byyear,"summary_by_signal_phase":byphase,"post_signal_paths_read":True,"model_mutated":False,"thresholds_recalibrated":False,"entry_stop_exit_rules_selected":False,"expectancy_or_profit_factor_claim_allowed":False,"stop_and_review_required":True,"completed_at":iso()}
            canon=dict(report);canon.pop("completed_at");report["replay_result_sha256"]=hashlib.sha256(json.dumps(canon,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
            self.redis.set_json(self.causal_diagnostic_replay_key("report"),report);self.redis.set_json(self.causal_diagnostic_replay_key("finalization_rescue_provenance"),{"rescue_build":"IPR-1.7.23-R3-FINALIZATION-RESCUE","source_runtime_version":VERSION,"source_runtime_build":BUILD,"source_sessions":len(sessions),"alpaca_requests_made":0,"method":"read-only Redis -> disk-backed SQLite spool -> exact original summary equations","report_shape_preserved":True,"completed_at":iso()})
            self._set_causal_diagnostic_replay_state(status="COMPLETED",phase="CAUSAL_DIAGNOSTIC_REPLAY_STOP_REVIEW",message="Causal Diagnostic Replay finalized by read-only rescue; STOP REVIEW",post_signal_paths_read=True,sessions_scanned=len(sessions),total_sessions=len(sessions),finalize_sessions_scanned=len(sessions),finalize_stage="COMPLETED",alpaca_requests_made=0,stop_and_review_required=True)
        except Exception as e:
            logging.exception("Causal diagnostic finalization rescue failed");self._set_causal_diagnostic_replay_state(status="ERROR",phase="CAUSAL_DIAGNOSTIC_FINALIZATION_RESCUE_BLOCKED",message="Finalization rescue failed closed",last_error=f"{type(e).__name__}: {e}",post_signal_paths_read=True,alpaca_requests_made=0)
        finally:
            try:
                if 'con' in locals():con.close()
            except:pass
            if dbpath:
                try:os.remove(dbpath)
                except:pass
            with self.causal_diagnostic_replay_lock:self.causal_diagnostic_replay_thread=None
    def start_causal_diagnostic_replay_finalize_rescue(self):
        ok,why=self._ctr_rescue_gate()
        if not ok:return False,why
        with self.causal_diagnostic_replay_lock:
            if self.causal_diagnostic_replay_thread and self.causal_diagnostic_replay_thread.is_alive():return False,"already_running"
            self.causal_diagnostic_replay_thread=threading.Thread(target=self.causal_diagnostic_replay_finalize_rescue_loop,name="causal-diagnostic-finalization-rescue",daemon=True);self.causal_diagnostic_replay_thread.start()
        return True,"started"

    def causal_diagnostic_replay_loop(self):
        try:
            pr=self.redis.get_json(self.causal_translation_protocol_key("report"),{}) or {};m=self.redis.get_json(self.feature_scoring_freeze_key("report"),{}) or {}
            roles=m.get("roles") or {};cal=m.get("calibration") or {};early=roles.get("early_core") or [];conf=roles.get("confirmation") or []
            if m.get("frozen_model_sha256")!=CAUSAL_DIAGNOSTIC_REPLAY_SPEC["required_frozen_model_sha256"]:raise RuntimeError("Frozen model mismatch at runtime")
            sessions=sorted(str(x) for x in (self.redis.get_json(self.phase0b_full_key("completed_sessions"),[]) or []) if "2019-"<=str(x)<="2026-08-31")
            done=set(self.redis.get_json(self.causal_diagnostic_replay_key("completed_sessions"),[]) or []);self.causal_diagnostic_replay_stop_event.clear()
            self._set_causal_diagnostic_replay_state(status="RUNNING",phase="CAUSAL_DIAGNOSTIC_REPLAY",message="Frozen causal diagnostic replay running",post_signal_paths_read=True,total_sessions=len(sessions),sessions_scanned=len(done))
            for si,sess in enumerate(sessions,1):
                if sess in done:continue
                if self.causal_diagnostic_replay_stop_event.is_set():self._set_causal_diagnostic_replay_state(status="PAUSED",phase="CAUSAL_DIAGNOSTIC_REPLAY",post_signal_paths_read=True,total_sessions=len(sessions),sessions_scanned=len(done));return
                target=date.fromisoformat(sess);p0=self.redis.get_json(self.phase0b_full_key(f"results:{sess}"),None)
                if p0 is None:raise RuntimeError(f"Missing Phase0B {sess}")
                coarse={str(x.get("symbol") or "").upper():x for x in (self.redis.get_json(self.historical_census_key(f"candidates:{sess}"),[]) or [])}
                pos=[x for x in p0 if x.get("classification")=="verified"];fail=[x for x in p0 if x.get("classification")=="failed"];fctx=[]
                for r in fail:
                    sym=str(r.get("symbol") or "").upper();cut=self._fd_coarse_cutoff(coarse.get(sym,{}))
                    if cut:fctx.append((r,sym,cut,self._fd_phase(cut,target),self._fd_price_band(r.get("t1_low"))))
                sel=[]
                for pi,r in enumerate(pos):
                    sym=str(r.get("symbol") or "").upper();phase=self._fd_phase(str(r.get("t2") or ""),target);pb=self._fd_price_band(r.get("t1_low"));exact=[x for x in fctx if x[3]==phase and x[4]==pb];pool=exact or [x for x in fctx if x[3]==phase] or fctx;base=int(hashlib.sha256(f"{sess}|{sym}|{r.get('t2')}".encode()).hexdigest()[:12],16);match=hashlib.sha256(f"{sess}|{sym}|{r.get('t2')}|{pi}".encode()).hexdigest()[:20];sel.append(("positive",r,sym,match))
                    for j in range(min(3,len(pool))):x=pool[(base+j*7919)%len(pool)];sel.append(("hard_negative",x[0],x[1],match))
                syms=sorted({x[2] for x in sel});start,end=self._probe_cycle_bounds(target);rows5=self._fd_fetch_session_rows(syms,target,start,end) if syms else {};signals=[];session_stats={"positive_selected":sum(1 for x in sel if x[0]=="positive"),"hard_negative_selected":sum(1 for x in sel if x[0]=="hard_negative"),"positive_scoreable":0,"hard_negative_scoreable":0}
                for cls,r,sym,match in sel:
                    first,cf,esc,csc=self._ctr_first_signal(rows5.get(sym,[]),early,float(cal["early_core"]["frozen_threshold"]),conf,float(cal["confirmation"]["frozen_threshold"]))
                    if esc:session_stats[f"{cls}_scoreable"]+=1
                    if not first:continue
                    edt,price,score,den=first;signals.append({"class":cls,"symbol":sym,"match_id":match,"signal_ts":edt.isoformat().replace("+00:00","Z"),"signal_close":price,"early_core_score":score,"early_core_weight_observed":den,"confirmation_ts":cf[0].isoformat().replace("+00:00","Z") if cf else None,"confirmation_score":cf[1] if cf else None,"phase":self._probe_session(edt.isoformat(),target),"event_t2":r.get("t2"),"event_baseline":r.get("t1_low")})
                sigsyms=sorted({x["symbol"] for x in signals});rows1=self._ctr_fetch_1m(sigsyms,target,start,end) if sigsyms else {}
                for x in signals:
                    sd=datetime.fromisoformat(x["signal_ts"].replace("Z","+00:00"));x["forward"]=self._ctr_forward(rows1.get(x["symbol"],[]),sd,float(x["signal_close"]),CAUSAL_DIAGNOSTIC_REPLAY_SPEC["horizons_minutes"])
                    b=x.get("event_baseline");x["percent_move_already_realized"]=(float(x["signal_close"])/float(b)-1)*100 if isinstance(b,(int,float)) and float(b)>0 else None
                    try:t2=datetime.fromisoformat(str(x.get("event_t2") or "").replace("Z","+00:00"));x["minutes_signal_to_plus20_confirmation"]=(t2-sd).total_seconds()/60.0
                    except:x["minutes_signal_to_plus20_confirmation"]=None
                    x["signal_before_plus20_confirmation"]=bool(isinstance(x["minutes_signal_to_plus20_confirmation"],(int,float)) and x["minutes_signal_to_plus20_confirmation"]>=0) if x["class"]=="positive" else None
                self.redis.set_json(self.causal_diagnostic_replay_key(f"results:{sess}"),signals);self.redis.set_json(self.causal_diagnostic_replay_key(f"stats:{sess}"),session_stats);done.add(sess);self.redis.set_json(self.causal_diagnostic_replay_key("completed_sessions"),sorted(done))
                if si==1 or si%20==0 or si==len(sessions):self._set_causal_diagnostic_replay_state(status="RUNNING",phase="CAUSAL_DIAGNOSTIC_REPLAY",message=f"Causal diagnostic replay {len(done)}/{len(sessions)}",post_signal_paths_read=True,total_sessions=len(sessions),sessions_scanned=len(done),current_session=sess)
            allr=[]
            for sess in sessions:allr.extend(self.redis.get_json(self.causal_diagnostic_replay_key(f"results:{sess}"),[]) or [])
            def summarize(rows):
                z={"signals":len(rows),"symbols":len({x["symbol"] for x in rows}),"percent_move_already_realized":self._ctr_dist([x.get("percent_move_already_realized") for x in rows]),"minutes_signal_to_plus20_confirmation":self._ctr_dist([x.get("minutes_signal_to_plus20_confirmation") for x in rows])}
                if rows and rows[0].get("class")=="positive":z["fraction_signal_before_plus20_confirmation"]=sum(1 for x in rows if x.get("signal_before_plus20_confirmation"))/len(rows)
                z["forward"]={}
                for h in [str(x) for x in CAUSAL_DIAGNOSTIC_REPLAY_SPEC["horizons_minutes"]]+["end_of_trading_cycle"]:
                    q=[x["forward"].get(h) for x in rows if (x.get("forward") or {}).get(h)];z["forward"][h]={m:self._ctr_dist([a.get(m) for a in q]) for m in CAUSAL_DIAGNOSTIC_REPLAY_SPEC["forward_metrics"]};z["forward"][h]["fraction_MFE_ge"]={str(t):sum(1 for a in q if a.get("MFE_pct_from_signal_close") is not None and a["MFE_pct_from_signal_close"]>=t*100)/len(q) if q else None for t in [.05,.1,.2]}
                return z
            byclass={c:summarize([x for x in allr if x["class"]==c]) for c in ["positive","hard_negative"]};byyear={};byphase={}
            for y in range(2019,2027):byyear[str(y)]={c:summarize([x for x in allr if x["class"]==c and x["signal_ts"].startswith(str(y))]) for c in ["positive","hard_negative"]}
            for ph in ["AH","Overnight","Premarket","Regular","Other"]:byphase[ph]={c:summarize([x for x in allr if x["class"]==c and x.get("phase")==ph]) for c in ["positive","hard_negative"]}
            statrows=[self.redis.get_json(self.causal_diagnostic_replay_key(f"stats:{sess}"),{}) or {} for sess in sessions];rawpos=sum(int(x.get("positive_selected") or 0) for x in statrows);rawhard=sum(int(x.get("hard_negative_selected") or 0) for x in statrows);scorepos=sum(int(x.get("positive_scoreable") or 0) for x in statrows);scorehard=sum(int(x.get("hard_negative_scoreable") or 0) for x in statrows)
            report={"version":VERSION,"build":BUILD,"replay_id":CAUSAL_DIAGNOSTIC_REPLAY_SPEC["replay_id"],"replay_spec_sha256":CAUSAL_DIAGNOSTIC_REPLAY_SHA256,"translation_spec_sha256":pr.get("translation_spec_sha256"),"translation_protocol_artifact_sha256":pr.get("translation_protocol_artifact_sha256"),"source_frozen_model_sha256":m.get("frozen_model_sha256"),"status":"COMPLETED","phase":"CAUSAL_DIAGNOSTIC_REPLAY_STOP_REVIEW","sessions":len(sessions),"first_session":sessions[0] if sessions else None,"last_session":sessions[-1] if sessions else None,"positive_verified_events":rawpos,"hard_negative_events_selected":rawhard,"positive_events_ever_scoreable":scorepos,"hard_negative_events_ever_scoreable":scorehard,"positive_scoreability_rate":scorepos/rawpos if rawpos else None,"hard_negative_scoreability_rate":scorehard/rawhard if rawhard else None,"positive_signal_events":byclass["positive"]["signals"],"hard_negative_signal_events":byclass["hard_negative"]["signals"],"positive_signal_coverage":byclass["positive"]["signals"]/rawpos if rawpos else None,"hard_negative_signal_coverage":byclass["hard_negative"]["signals"]/rawhard if rawhard else None,"summary_by_class":byclass,"summary_by_year":byyear,"summary_by_signal_phase":byphase,"post_signal_paths_read":True,"model_mutated":False,"thresholds_recalibrated":False,"entry_stop_exit_rules_selected":False,"expectancy_or_profit_factor_claim_allowed":False,"stop_and_review_required":True,"completed_at":iso()};canon=dict(report);canon.pop("completed_at");report["replay_result_sha256"]=hashlib.sha256(json.dumps(canon,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest();self.redis.set_json(self.causal_diagnostic_replay_key("report"),report);self._set_causal_diagnostic_replay_state(status="COMPLETED",phase="CAUSAL_DIAGNOSTIC_REPLAY_STOP_REVIEW",message="Causal Diagnostic Replay completed; STOP REVIEW",post_signal_paths_read=True,sessions_scanned=len(sessions),total_sessions=len(sessions),stop_and_review_required=True)
        except Exception as e:
            logging.exception("Causal diagnostic replay failed");self._set_causal_diagnostic_replay_state(status="ERROR",phase="BLOCKED",message="Causal diagnostic replay failed closed",last_error=f"{type(e).__name__}: {e}",post_signal_paths_read=True)
        finally:
            with self.causal_diagnostic_replay_lock:self.causal_diagnostic_replay_thread=None
    def start_causal_diagnostic_replay(self):
        ok,why=self._causal_diagnostic_replay_gate()
        if not ok:return False,why
        with self.causal_diagnostic_replay_lock:
            if self.causal_diagnostic_replay_thread and self.causal_diagnostic_replay_thread.is_alive():return False,"already_running"
            self.causal_diagnostic_replay_thread=threading.Thread(target=self.causal_diagnostic_replay_loop,name="causal-diagnostic-replay",daemon=True);self.causal_diagnostic_replay_thread.start()
        return True,"started"



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
            "phase0_capability_probe": "/phase0/probe",
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



@app.get("/phase0/reference-candidates")
def phase0_reference_candidates_home():
    return jsonify({"purpose":"Find candidates only; no detector-based selection and no Phase 0A start","start_url":"/phase0/reference-candidates/start","status_url":"/phase0/reference-candidates/status","result_url":"/phase0/reference-candidates/result","phase0a_allowed":False})

@app.get("/phase0/reference-candidates/start")
@app.post("/phase0/reference-candidates/start")
def phase0_reference_candidates_start():
    if request.method == "POST" and not export_authorized(): return jsonify({"ok":False,"error":"unauthorized"}),401
    started,message=radar.start_phase0_reference_discovery()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0/reference-candidates/status","result_url":"/phase0/reference-candidates/result","phase0a_allowed":False}), (202 if started else 409)

@app.get("/phase0/reference-candidates/status")
def phase0_reference_candidates_status():
    stored=radar.redis.get_json(radar.phase0_reference_key("status"),None) if radar.redis.configured else None
    with radar.phase0_reference_lock:
        payload=dict(stored or radar.phase0_reference_state); payload["worker_alive"]=bool(radar.phase0_reference_thread and radar.phase0_reference_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/reference-candidates/result")
def phase0_reference_candidates_result():
    report=radar.redis.get_json(radar.phase0_reference_key("report"),None) if radar.redis.configured else radar.phase0_reference_report
    if not report: return jsonify({"result_ready":False,"phase0a_allowed":False,"status_url":"/phase0/reference-candidates/status"}),202
    return jsonify(report)

@app.get("/phase0/universe-probe")
def historical_universe_probe_home():
    return jsonify({"purpose":"Test historical universe coverage before any 2019-2026 census","start_url":"/phase0/universe-probe/start","status_url":"/phase0/universe-probe/status","result_url":"/phase0/universe-probe/result","historical_census_allowed":False})

@app.get("/phase0/universe-probe/start")
@app.post("/phase0/universe-probe/start")
def historical_universe_probe_start():
    started,message=radar.start_historical_universe_probe()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0/universe-probe/status","result_url":"/phase0/universe-probe/result","historical_census_allowed":False}), (202 if started else 409)

@app.get("/phase0/universe-probe/status")
def historical_universe_probe_status():
    stored=radar.redis.get_json(radar.universe_probe_key("status"),None) if radar.redis.configured else None
    with radar.universe_probe_lock:
        payload=dict(stored or radar.universe_probe_state); payload["worker_alive"]=bool(radar.universe_probe_thread and radar.universe_probe_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/universe-probe/result")
def historical_universe_probe_result():
    report=radar.redis.get_json(radar.universe_probe_key("report"),None) if radar.redis.configured else radar.universe_probe_report
    if not report:return jsonify({"result_ready":False,"status_url":"/phase0/universe-probe/status","historical_census_allowed":False}),202
    return jsonify(report)

@app.get("/phase0/universe-reconstruction")
def universe_reconstruction_home():
    return jsonify({"purpose":"Reconstruct 2019-2026 symbol-time universe without assuming ticker identity continuity","start_url":"/phase0/universe-reconstruction/start","status_url":"/phase0/universe-reconstruction/status","result_url":"/phase0/universe-reconstruction/result","pause_url":"/phase0/universe-reconstruction/pause","historical_census_allowed":False})

@app.get("/phase0/universe-reconstruction/start")
@app.post("/phase0/universe-reconstruction/start")
def universe_reconstruction_start():
    started,message=radar.start_historical_universe_reconstruction(); return jsonify({"ok":started,"status":message,"status_url":"/phase0/universe-reconstruction/status","result_url":"/phase0/universe-reconstruction/result","historical_census_allowed":False}), (202 if started else 409)

@app.get("/phase0/universe-reconstruction/pause")
@app.post("/phase0/universe-reconstruction/pause")
def universe_reconstruction_pause():
    radar.universe_reconstruction_stop_event.set(); return jsonify({"ok":True,"message":"pause_requested","historical_census_allowed":False})

@app.get("/phase0/universe-reconstruction/status")
def universe_reconstruction_status():
    stored=radar.redis.get_json(radar.universe_reconstruction_key("status"),None) if radar.redis.configured else None
    with radar.universe_reconstruction_lock:
        payload=dict(stored or radar.universe_reconstruction_state); payload["worker_alive"]=bool(radar.universe_reconstruction_thread and radar.universe_reconstruction_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/universe-reconstruction/result")
def universe_reconstruction_result():
    report=radar.redis.get_json(radar.universe_reconstruction_key("report"),None) if radar.redis.configured else None
    if not report: return jsonify({"result_ready":False,"status_url":"/phase0/universe-reconstruction/status","historical_census_allowed":False}),202
    return jsonify(report)

@app.get("/phase0/historical-census/audit")
def historical_census_audit_home():
    allowed,reason=radar._historical_census_audit_gate()
    return jsonify({"purpose":"Read-only audit of stored 2019-2026 Historical Census; no Alpaca requests and no census rescan","gate_allowed":allowed,"gate_reason":reason,"start_url":"/phase0/historical-census/audit/start","status_url":"/phase0/historical-census/audit/status","result_url":"/phase0/historical-census/audit/result","phase0b_allowed":False})

@app.get("/phase0/historical-census/audit/start")
@app.post("/phase0/historical-census/audit/start")
def historical_census_audit_start():
    started,message=radar.start_historical_census_audit()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0/historical-census/audit/status","result_url":"/phase0/historical-census/audit/result","phase0b_allowed":False}), (202 if started else 409)

@app.get("/phase0/historical-census/audit/status")
def historical_census_audit_status():
    stored=radar.redis.get_json(radar.historical_census_audit_key("status"),None) if radar.redis.configured else None
    with radar.historical_census_audit_lock:
        payload=dict(stored or radar.historical_census_audit_state); payload["worker_alive"]=bool(radar.historical_census_audit_thread and radar.historical_census_audit_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/historical-census/audit/result")
def historical_census_audit_result():
    report=radar.redis.get_json(radar.historical_census_audit_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/phase0/historical-census/audit/status","phase0b_allowed":False}),202
    return jsonify(report)

@app.get("/phase0/historical-census/instrument-cleanup")
def instrument_cleanup_home():
    allowed,reason=radar._instrument_cleanup_gate()
    return jsonify({"purpose":"Instrument-type cleanup audit over stored 2019-2026 Census candidates; no Census rescan and no market-bar requests","gate_allowed":allowed,"gate_reason":reason,"start_url":"/phase0/historical-census/instrument-cleanup/start","status_url":"/phase0/historical-census/instrument-cleanup/status","result_url":"/phase0/historical-census/instrument-cleanup/result","phase0b_allowed":False})

@app.get("/phase0/historical-census/instrument-cleanup/start")
@app.post("/phase0/historical-census/instrument-cleanup/start")
def instrument_cleanup_start():
    started,message=radar.start_instrument_cleanup_audit()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0/historical-census/instrument-cleanup/status","result_url":"/phase0/historical-census/instrument-cleanup/result","phase0b_allowed":False}), (202 if started else 409)

@app.get("/phase0/historical-census/instrument-cleanup/status")
def instrument_cleanup_status():
    stored=radar.redis.get_json(radar.instrument_cleanup_key("status"),None) if radar.redis.configured else None
    with radar.instrument_cleanup_lock:
        payload=dict(stored or radar.instrument_cleanup_state); payload["worker_alive"]=bool(radar.instrument_cleanup_thread and radar.instrument_cleanup_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/historical-census/instrument-cleanup/result")
def instrument_cleanup_result():
    report=radar.redis.get_json(radar.instrument_cleanup_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/phase0/historical-census/instrument-cleanup/status","phase0b_allowed":False}),202
    return jsonify(report)


@app.get("/phase0/historical-census/pre0b-audit")
def pre0b_audit_home():
    allowed,reason=radar._pre0b_audit_gate()
    return jsonify({"purpose":"Final cheap audit before Phase 0B: unresolved-name resolution, clean/same-bar intersection, and annual coverage normalization","gate_allowed":allowed,"gate_reason":reason,"start_url":"/phase0/historical-census/pre0b-audit/start","status_url":"/phase0/historical-census/pre0b-audit/status","result_url":"/phase0/historical-census/pre0b-audit/result","phase0b_allowed":False})

@app.get("/phase0/historical-census/pre0b-audit/start")
@app.post("/phase0/historical-census/pre0b-audit/start")
def pre0b_audit_start():
    started,message=radar.start_pre0b_resolution_normalization_audit()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0/historical-census/pre0b-audit/status","result_url":"/phase0/historical-census/pre0b-audit/result","phase0b_allowed":False}), (202 if started else 409)

@app.get("/phase0/historical-census/pre0b-audit/status")
def pre0b_audit_status():
    stored=radar.redis.get_json(radar.pre0b_audit_key("status"),None) if radar.redis.configured else None
    with radar.pre0b_audit_lock:
        payload=dict(stored or radar.pre0b_audit_state); payload["worker_alive"]=bool(radar.pre0b_audit_thread and radar.pre0b_audit_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/historical-census/pre0b-audit/result")
def pre0b_audit_result():
    report=radar.redis.get_json(radar.pre0b_audit_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/phase0/historical-census/pre0b-audit/status","phase0b_allowed":False}),202
    return jsonify(report)

@app.get("/phase0/phase0b-dataset-audit")
def phase0b_dataset_audit_home():
    allowed,reason=radar._phase0b_dataset_audit_gate()
    return jsonify({"purpose":"Read-only audit of completed Phase 0B dataset before Feature Discovery","audit_id":PHASE0B_DATASET_AUDIT_SPEC["audit_id"],"audit_sha256":PHASE0B_DATASET_AUDIT_SHA256,"gate_allowed":allowed,"gate_reason":reason,"protocol_url":"/phase0/phase0b-dataset-audit/protocol","start_url":"/phase0/phase0b-dataset-audit/start","status_url":"/phase0/phase0b-dataset-audit/status","result_url":"/phase0/phase0b-dataset-audit/result","feature_discovery_allowed":False})

@app.get("/phase0/phase0b-dataset-audit/protocol")
def phase0b_dataset_audit_protocol(): return jsonify({"version":VERSION,"build":BUILD,"spec":PHASE0B_DATASET_AUDIT_SPEC,"audit_sha256":PHASE0B_DATASET_AUDIT_SHA256})

@app.get("/phase0/phase0b-dataset-audit/start")
@app.post("/phase0/phase0b-dataset-audit/start")
def phase0b_dataset_audit_start():
    started,message=radar.start_phase0b_dataset_audit()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0/phase0b-dataset-audit/status","result_url":"/phase0/phase0b-dataset-audit/result","feature_discovery_allowed":False}), (202 if started else 409)

@app.get("/phase0/phase0b-dataset-audit/status")
def phase0b_dataset_audit_status():
    stored=radar.redis.get_json(radar.phase0b_dataset_audit_key("status"),None) if radar.redis.configured else None
    with radar.phase0b_dataset_audit_lock:
        payload=dict(stored or radar.phase0b_dataset_audit_state); payload["worker_alive"]=bool(radar.phase0b_dataset_audit_thread and radar.phase0b_dataset_audit_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/phase0b-dataset-audit/result")
def phase0b_dataset_audit_result():
    report=radar.redis.get_json(radar.phase0b_dataset_audit_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/phase0/phase0b-dataset-audit/status","feature_discovery_allowed":False}),202
    return jsonify(report)



@app.get("/research/feature-discovery/protocol")
def feature_discovery_protocol():
    allowed,reason=radar._feature_discovery_gate()
    return jsonify({"version":VERSION,"build":BUILD,"protocol":FEATURE_DISCOVERY_PROTOCOL_SPEC,"protocol_sha256":FEATURE_DISCOVERY_PROTOCOL_SHA256,"execution_spec":FEATURE_DISCOVERY_EXEC_SPEC,"execution_sha256":FEATURE_DISCOVERY_EXEC_SHA256,"source_audit_gate_passed":allowed,"gate_reason":reason,"feature_discovery_allowed":allowed,"validation_2025_opened":False,"holdout_2026_opened":False})

@app.get("/research/feature-discovery")
def feature_discovery_home():
    allowed,reason=radar._feature_discovery_gate()
    return jsonify({"purpose":"Causal Feature Discovery on frozen 2019-2024 Discovery split only","gate_allowed":allowed,"gate_reason":reason,"protocol_url":"/research/feature-discovery/protocol","start_url":"/research/feature-discovery/start","status_url":"/research/feature-discovery/status","result_url":"/research/feature-discovery/result","pause_url":"/research/feature-discovery/pause","validation_2025_opened":False,"holdout_2026_opened":False})

@app.get("/research/feature-discovery/start")
def feature_discovery_start():
    started,message=radar.start_feature_discovery(); return jsonify({"ok":started,"status":"started" if started else message,"status_url":"/research/feature-discovery/status","result_url":"/research/feature-discovery/result","validation_2025_opened":False,"holdout_2026_opened":False}), (200 if started else 409)

@app.get("/research/feature-discovery/pause")
def feature_discovery_pause():
    radar.feature_discovery_stop_event.set(); return jsonify({"ok":True,"message":"pause_requested","resume_url":"/research/feature-discovery/start","validation_2025_opened":False,"holdout_2026_opened":False})

@app.get("/research/feature-discovery/status")
def feature_discovery_status():
    stored=radar.redis.get_json(radar.feature_discovery_key("status"),None) if radar.redis.configured else None
    with radar.feature_discovery_lock:
        payload=dict(stored or radar.feature_discovery_state); payload["worker_alive"]=bool(radar.feature_discovery_thread and radar.feature_discovery_thread.is_alive())
    return jsonify(payload)

@app.get("/research/feature-discovery/result")
def feature_discovery_result():
    report=radar.redis.get_json(radar.feature_discovery_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/research/feature-discovery/status","validation_2025_opened":False,"holdout_2026_opened":False}),202
    return jsonify(report)

@app.get("/research/feature-discovery-audit/protocol")
def feature_discovery_audit_protocol():
    allowed,reason=radar._feature_discovery_audit_gate()
    return jsonify({"version":VERSION,"build":BUILD,"audit_spec":FEATURE_DISCOVERY_AUDIT_SPEC,"audit_sha256":FEATURE_DISCOVERY_AUDIT_SHA256,"gate_allowed":allowed,"gate_reason":reason,"validation_2025_opened":False,"holdout_2026_opened":False})

@app.get("/research/feature-discovery-audit")
def feature_discovery_audit_home():
    allowed,reason=radar._feature_discovery_audit_gate()
    return jsonify({"purpose":"Read-only 2019-2024 Discovery stability/ladder audit before Validation","gate_allowed":allowed,"gate_reason":reason,"protocol_url":"/research/feature-discovery-audit/protocol","start_url":"/research/feature-discovery-audit/start","status_url":"/research/feature-discovery-audit/status","result_url":"/research/feature-discovery-audit/result","validation_2025_opened":False,"holdout_2026_opened":False})

@app.get("/research/feature-discovery-audit/start")
def feature_discovery_audit_start():
    started,message=radar.start_feature_discovery_audit(); return jsonify({"ok":started,"status":"started" if started else message,"status_url":"/research/feature-discovery-audit/status","result_url":"/research/feature-discovery-audit/result","validation_2025_opened":False,"holdout_2026_opened":False}), (200 if started else 409)

@app.get("/research/feature-discovery-audit/status")
def feature_discovery_audit_status():
    stored=radar.redis.get_json(radar.feature_discovery_audit_key("status"),None) if radar.redis.configured else None
    with radar.feature_discovery_audit_lock:
        payload=dict(stored or radar.feature_discovery_audit_state); payload["worker_alive"]=bool(radar.feature_discovery_audit_thread and radar.feature_discovery_audit_thread.is_alive())
    return jsonify(payload)

@app.get("/research/feature-discovery-audit/result")
def feature_discovery_audit_result():
    report=radar.redis.get_json(radar.feature_discovery_audit_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/research/feature-discovery-audit/status","validation_2025_opened":False,"holdout_2026_opened":False}),202
    return jsonify(report)

@app.get("/research/feature-scoring-freeze/protocol")
def feature_scoring_freeze_protocol():
    allowed,reason=radar._feature_scoring_freeze_gate(); return jsonify({"version":VERSION,"build":BUILD,"freeze_spec":FEATURE_SCORING_FREEZE_SPEC,"freeze_spec_sha256":FEATURE_SCORING_FREEZE_SHA256,"gate_allowed":allowed,"gate_reason":reason,"validation_2025_opened":False,"holdout_2026_opened":False})

@app.get("/research/feature-scoring-freeze/start")
def feature_scoring_freeze_start():
    started,message=radar.start_feature_scoring_freeze(); return jsonify({"ok":started,"status":"started" if started else message,"status_url":"/research/feature-scoring-freeze/status","result_url":"/research/feature-scoring-freeze/result","validation_2025_opened":False,"holdout_2026_opened":False}), (200 if started else 409)

@app.get("/research/feature-scoring-freeze/status")
def feature_scoring_freeze_status():
    stored=radar.redis.get_json(radar.feature_scoring_freeze_key("status"),None) if radar.redis.configured else None
    with radar.feature_scoring_freeze_lock:
        payload=dict(stored or radar.feature_scoring_freeze_state); payload["worker_alive"]=bool(radar.feature_scoring_freeze_thread and radar.feature_scoring_freeze_thread.is_alive())
    return jsonify(payload)

@app.get("/research/feature-scoring-freeze/result")
def feature_scoring_freeze_result():
    report=radar.redis.get_json(radar.feature_scoring_freeze_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/research/feature-scoring-freeze/status","validation_2025_opened":False,"holdout_2026_opened":False}),202
    return jsonify(report)

@app.get("/research/validation-success-criteria/protocol")
def validation_success_criteria_protocol():
    allowed,reason=radar._validation_criteria_gate(); return jsonify({"version":VERSION,"build":BUILD,"criteria_spec":VALIDATION_SUCCESS_CRITERIA_SPEC,"criteria_sha256":VALIDATION_SUCCESS_CRITERIA_SHA256,"gate_allowed":allowed,"gate_reason":reason,"validation_2025_opened":False,"holdout_2026_opened":False})

@app.get("/research/validation-success-criteria/start")
def validation_success_criteria_start():
    ok,message=radar.freeze_validation_success_criteria(); return jsonify({"ok":ok,"status":message,"status_url":"/research/validation-success-criteria/status","result_url":"/research/validation-success-criteria/result","validation_2025_opened":False,"holdout_2026_opened":False}), (200 if ok else 409)

@app.get("/research/validation-success-criteria/status")
def validation_success_criteria_status():
    stored=radar.redis.get_json(radar.validation_criteria_key("status"),None) if radar.redis.configured else None
    return jsonify(dict(stored or radar.validation_criteria_state))

@app.get("/research/validation-success-criteria/result")
def validation_success_criteria_result():
    report=radar.redis.get_json(radar.validation_criteria_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/research/validation-success-criteria/status","validation_2025_opened":False,"holdout_2026_opened":False}),202
    return jsonify(report)




@app.get("/research/early-causal-entry/protocol")
def early_causal_entry_protocol():
    allowed, reason = radar._early_causal_entry_research_protocol_gate()
    return jsonify({"version": VERSION, "build": BUILD, "research_spec": EARLY_CAUSAL_ENTRY_RESEARCH_SPEC, "research_spec_sha256": EARLY_CAUSAL_ENTRY_RESEARCH_SHA256, "gate_allowed": allowed, "gate_reason": reason, "alpaca_requests_made": 0, "new_post_2026_08_31_data_read": False})

@app.get("/research/early-causal-entry/start")
def research_early_causal_entry_protocol_start():
    ok, why = radar.freeze_early_causal_entry_research_protocol()
    return jsonify({"ok": ok, "status": why, "status_url": "/research/early-causal-entry/status", "result_url": "/research/early-causal-entry/result", "research_execution_started": False}), (200 if ok else 409)

@app.get("/research/early-causal-entry/status")
def research_early_causal_entry_protocol_status():
    x = radar.redis.get_json(radar.early_causal_entry_research_key("status"), None) if radar.redis.configured else None
    return jsonify(x or {"status": "IDLE", "phase": "NOT_STARTED", "research_id": EARLY_CAUSAL_ENTRY_RESEARCH_SPEC["research_id"], "alpaca_requests_made": 0})

@app.get("/research/early-causal-entry/result")
def research_early_causal_entry_protocol_result():
    x = radar.redis.get_json(radar.early_causal_entry_research_key("report"), None) if radar.redis.configured else None
    if not x:
        return jsonify({"result_ready": False, "status_url": "/research/early-causal-entry/status"}), 202
    return jsonify(x)

@app.get("/research/early-causal-entry/execution/amendment/protocol")
def research_early_causal_entry_threshold_amendment_protocol():
    allowed,reason=radar._early_causal_entry_threshold_amendment_gate();return jsonify({"version":VERSION,"build":BUILD,"amendment_spec":EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SPEC,"amendment_spec_sha256":EARLY_CAUSAL_ENTRY_THRESHOLD_AMENDMENT_SHA256,"gate_allowed":allowed,"gate_reason":reason,"research_execution_started":False,"alpaca_requests_made":0,"new_post_2026_08_31_data_read":False})

@app.get("/research/early-causal-entry/execution/amendment/start")
@app.post("/research/early-causal-entry/execution/amendment/start")
def research_early_causal_entry_threshold_amendment_start():
    ok,why=radar.freeze_early_causal_entry_threshold_amendment();return jsonify({"ok":ok,"status":why,"research_execution_started":False,"result_url":"/research/early-causal-entry/execution/amendment/result"}),(200 if ok else 409)

@app.get("/research/early-causal-entry/execution/amendment/result")
def research_early_causal_entry_threshold_amendment_result():
    x=radar.redis.get_json(radar.early_causal_entry_threshold_amendment_key("report"),None) if radar.redis.configured else None
    if not x:return jsonify({"result_ready":False,"protocol_url":"/research/early-causal-entry/execution/amendment/protocol"}),202
    return jsonify(x)

@app.get("/research/early-causal-entry/execution/protocol")
def research_early_causal_entry_execution_protocol():
    allowed,reason=radar._early_causal_entry_exec_gate();return jsonify({"version":VERSION,"build":BUILD,"execution_spec":EARLY_CAUSAL_ENTRY_EXEC_SPEC,"execution_spec_sha256":EARLY_CAUSAL_ENTRY_EXEC_SHA256,"gate_allowed":allowed,"gate_reason":reason,"new_post_2026_08_31_data_read":False})

@app.get("/research/early-causal-entry/execution/start")
@app.post("/research/early-causal-entry/execution/start")
def research_early_causal_entry_execution_start():
    ok,why=radar.start_early_causal_entry_exec();return jsonify({"ok":ok,"status":"started" if ok else why,"status_url":"/research/early-causal-entry/execution/status","result_url":"/research/early-causal-entry/execution/result"}),(202 if ok else 409)

@app.get("/research/early-causal-entry/execution/pause")
@app.post("/research/early-causal-entry/execution/pause")
def research_early_causal_entry_execution_pause():
    radar.early_causal_entry_exec_stop_event.set();return jsonify({"ok":True,"message":"pause_requested","resume_url":"/research/early-causal-entry/execution/start"})

@app.get("/research/early-causal-entry/execution/status")
def research_early_causal_entry_execution_status():
    x=radar.redis.get_json(radar.early_causal_entry_exec_key("status"),None) if radar.redis.configured else None
    with radar.early_causal_entry_exec_lock:
        out=dict(x or radar.early_causal_entry_exec_state);out["worker_alive"]=bool(radar.early_causal_entry_exec_thread and radar.early_causal_entry_exec_thread.is_alive())
    return jsonify(out)

@app.get("/research/early-causal-entry/execution/result")
def research_early_causal_entry_execution_result():
    x=radar.redis.get_json(radar.early_causal_entry_exec_key("report"),None) if radar.redis.configured else None
    if not x:return jsonify({"result_ready":False,"status_url":"/research/early-causal-entry/execution/status"}),202
    return jsonify(x)

@app.get("/research/causal-diagnostic-replay/finalize-rescue/protocol")
def causal_diagnostic_replay_finalize_rescue_protocol():
    allowed,reason=radar._ctr_rescue_gate();return jsonify({"rescue_build":"IPR-1.7.23-R3-FINALIZATION-RESCUE","gate_allowed":allowed,"gate_reason":reason,"read_only_source":True,"alpaca_requests_made":0,"normal_result_url":"/research/causal-diagnostic-replay/result"})
@app.get("/research/causal-diagnostic-replay/finalize-rescue/start")
def causal_diagnostic_replay_finalize_rescue_start():
    ok,why=radar.start_causal_diagnostic_replay_finalize_rescue();return jsonify({"ok":ok,"status":"started" if ok else why,"status_url":"/research/causal-diagnostic-replay/status","result_url":"/research/causal-diagnostic-replay/result"}),(200 if ok else 409)

@app.get("/research/causal-diagnostic-replay/protocol")
def causal_diagnostic_replay_protocol():
    allowed,reason=radar._causal_diagnostic_replay_gate();return jsonify({"version":VERSION,"build":BUILD,"replay_spec":CAUSAL_DIAGNOSTIC_REPLAY_SPEC,"replay_spec_sha256":CAUSAL_DIAGNOSTIC_REPLAY_SHA256,"gate_allowed":allowed,"gate_reason":reason,"post_signal_paths_read":False})
@app.get("/research/causal-diagnostic-replay/start")
def causal_diagnostic_replay_start():
    ok,why=radar.start_causal_diagnostic_replay();return jsonify({"ok":ok,"status":"started" if ok else why,"status_url":"/research/causal-diagnostic-replay/status","result_url":"/research/causal-diagnostic-replay/result"}),(200 if ok else 409)
@app.get("/research/causal-diagnostic-replay/pause")
def causal_diagnostic_replay_pause():
    radar.causal_diagnostic_replay_stop_event.set();return jsonify({"ok":True,"message":"pause_requested","resume_url":"/research/causal-diagnostic-replay/start"})
@app.get("/research/causal-diagnostic-replay/status")
def causal_diagnostic_replay_status():
    x=radar.redis.get_json(radar.causal_diagnostic_replay_key("status"),None) if radar.redis.configured else None;return jsonify(x or radar.causal_diagnostic_replay_state)
@app.get("/research/causal-diagnostic-replay/result")
def causal_diagnostic_replay_result():
    x=radar.redis.get_json(radar.causal_diagnostic_replay_key("report"),None) if radar.redis.configured else None
    if not x:return jsonify({"result_ready":False,"status_url":"/research/causal-diagnostic-replay/status"}),202
    return jsonify(x)

@app.get("/research/causal-trading-translation/protocol")
def causal_trading_translation_protocol():
    allowed,reason=radar._causal_translation_protocol_gate();return jsonify({"version":VERSION,"build":BUILD,"translation_spec":CAUSAL_TRADING_TRANSLATION_SPEC,"translation_spec_sha256":CAUSAL_TRADING_TRANSLATION_SHA256,"gate_allowed":allowed,"gate_reason":reason,"post_signal_paths_read":False})
@app.get("/research/causal-trading-translation/start")
def causal_trading_translation_start():
    ok,why=radar.freeze_causal_translation_protocol();return jsonify({"ok":ok,"status":why,"status_url":"/research/causal-trading-translation/status","result_url":"/research/causal-trading-translation/result","post_signal_paths_read":False}),(200 if ok else 409)
@app.get("/research/causal-trading-translation/status")
def causal_trading_translation_status():
    x=radar.redis.get_json(radar.causal_translation_protocol_key("status"),None) if radar.redis.configured else None;return jsonify(x or {"status":"IDLE","phase":"NOT_STARTED","translation_id":CAUSAL_TRADING_TRANSLATION_SPEC["translation_id"],"post_signal_paths_read":False})
@app.get("/research/causal-trading-translation/result")
def causal_trading_translation_result():
    x=radar.redis.get_json(radar.causal_translation_protocol_key("report"),None) if radar.redis.configured else None
    if not x:return jsonify({"result_ready":False,"status_url":"/research/causal-trading-translation/status"}),202
    return jsonify(x)

@app.get("/research/final-frozen-holdout-2026/protocol")
def final_frozen_holdout_2026_protocol():
    allowed,reason=radar._final_holdout_2026_gate();return jsonify({"version":VERSION,"build":BUILD,"holdout_spec":FINAL_HOLDOUT_2026_SPEC,"holdout_spec_sha256":FINAL_HOLDOUT_2026_SHA256,"gate_allowed":allowed,"gate_reason":reason,"holdout_2026_opened":False,"holdout_2026_read":False})
@app.get("/research/final-frozen-holdout-2026/start")
def final_frozen_holdout_2026_start():
    ok,why=radar.start_final_frozen_holdout_2026();return jsonify({"ok":ok,"status":"started" if ok else why,"status_url":"/research/final-frozen-holdout-2026/status","result_url":"/research/final-frozen-holdout-2026/result"}),(200 if ok else 409)
@app.get("/research/final-frozen-holdout-2026/pause")
def final_frozen_holdout_2026_pause():
    radar.final_holdout_2026_stop_event.set();return jsonify({"ok":True,"status":"pause_requested"})
@app.get("/research/final-frozen-holdout-2026/status")
def final_frozen_holdout_2026_status():
    x=radar.redis.get_json(radar.final_holdout_2026_key("status"),None) if radar.redis.configured else None;out=x if isinstance(x,dict) else dict(radar.final_holdout_2026_state);out["worker_alive"]=bool(radar.final_holdout_2026_thread and radar.final_holdout_2026_thread.is_alive());return jsonify(out)
@app.get("/research/final-frozen-holdout-2026/result")
def final_frozen_holdout_2026_result():
    x=radar.redis.get_json(radar.final_holdout_2026_key("report"),None) if radar.redis.configured else None
    if not x:return jsonify({"result_ready":False,"status_url":"/research/final-frozen-holdout-2026/status"}),202
    return jsonify(x)

@app.get("/research/holdout-success-criteria/protocol")
def holdout_success_criteria_protocol():
    allowed,reason=radar._holdout_criteria_gate();return jsonify({"version":VERSION,"build":BUILD,"criteria_spec":HOLDOUT_SUCCESS_CRITERIA_SPEC,"criteria_sha256":HOLDOUT_SUCCESS_CRITERIA_SHA256,"gate_allowed":allowed,"gate_reason":reason,"holdout_2026_opened":False,"holdout_2026_read":False})

@app.get("/research/holdout-success-criteria/start")
def holdout_success_criteria_start():
    ok,message=radar.freeze_holdout_success_criteria();return jsonify({"ok":ok,"status":message,"status_url":"/research/holdout-success-criteria/status","result_url":"/research/holdout-success-criteria/result","holdout_2026_opened":False,"holdout_2026_read":False}),(200 if ok else 409)

@app.get("/research/holdout-success-criteria/status")
def holdout_success_criteria_status():
    stored=radar.redis.get_json(radar.holdout_criteria_key("status"),None) if radar.redis.configured else None;return jsonify(dict(stored or radar.holdout_criteria_state))

@app.get("/research/holdout-success-criteria/result")
def holdout_success_criteria_result():
    report=radar.redis.get_json(radar.holdout_criteria_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/research/holdout-success-criteria/status","holdout_2026_opened":False,"holdout_2026_read":False}),202
    return jsonify(report)

@app.get("/phase0/phase0b-full")
def phase0b_full_home():
    allowed,reason=radar._phase0b_full_gate()
    return jsonify({"purpose":"Full Trading Cycle 1-minute verification of all 205,028 frozen clean candidates; no window optimization","verification_id":PHASE0B_FULL_SPEC["verification_id"],"phase0b_sha256":PHASE0B_FULL_SHA256,"gate_allowed":allowed,"gate_reason":reason,"protocol_url":"/phase0/phase0b-full/protocol","start_url":"/phase0/phase0b-full/start","status_url":"/phase0/phase0b-full/status","result_url":"/phase0/phase0b-full/result","pause_url":"/phase0/phase0b-full/pause","feature_discovery_allowed":False})

@app.get("/research/frozen-model-validation-2025/protocol")
def v25_protocol():
    ok,why=radar._validation_2025_gate();return jsonify({"version":VERSION,"build":BUILD,"validation_spec":VALIDATION_2025_SPEC,"validation_spec_sha256":VALIDATION_2025_SHA256,"gate_allowed":ok,"gate_reason":why,"validation_2025_opened":False,"holdout_2026_opened":False})
@app.get("/research/frozen-model-validation-2025/start")
def v25_start():
    ok,why=radar.start_frozen_model_validation_2025();return jsonify({"ok":ok,"status":"started" if ok else why,"status_url":"/research/frozen-model-validation-2025/status","result_url":"/research/frozen-model-validation-2025/result","holdout_2026_opened":False}),(200 if ok else 409)
@app.get("/research/frozen-model-validation-2025/pause")
def v25_pause():radar.validation_2025_stop_event.set();return jsonify({"ok":True,"message":"pause_requested","holdout_2026_opened":False})
@app.get("/research/frozen-model-validation-2025/status")
def v25_status():
    x=radar.redis.get_json(radar.validation_2025_key("status"),None) if radar.redis.configured else None;out=x if isinstance(x,dict) else dict(radar.validation_2025_state);out["worker_alive"]=bool(radar.validation_2025_thread and radar.validation_2025_thread.is_alive());return jsonify(out)
@app.get("/research/frozen-model-validation-2025/result")
def v25_result():
    x=radar.redis.get_json(radar.validation_2025_key("report"),None) if radar.redis.configured else None
    if not x:return jsonify({"result_ready":False,"status_url":"/research/frozen-model-validation-2025/status","holdout_2026_opened":False}),202
    return jsonify(x)

@app.get("/phase0/phase0b-full/protocol")
def phase0b_full_protocol(): return jsonify({"version":VERSION,"build":BUILD,"spec":PHASE0B_FULL_SPEC,"phase0b_sha256":PHASE0B_FULL_SHA256})

@app.get("/phase0/phase0b-full/start")
@app.post("/phase0/phase0b-full/start")
def phase0b_full_start():
    started,message=radar.start_phase0b_full()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0/phase0b-full/status","result_url":"/phase0/phase0b-full/result","feature_discovery_allowed":False}), (202 if started else 409)

@app.get("/phase0/phase0b-full/pause")
@app.post("/phase0/phase0b-full/pause")
def phase0b_full_pause():
    radar.phase0b_full_stop_event.set(); return jsonify({"ok":True,"message":"pause_requested","resume_url":"/phase0/phase0b-full/start","feature_discovery_allowed":False})

@app.get("/phase0/phase0b-full/status")
def phase0b_full_status():
    stored=radar.redis.get_json(radar.phase0b_full_key("status"),None) if radar.redis.configured else None
    with radar.phase0b_full_lock:
        payload=dict(stored or radar.phase0b_full_state); payload["worker_alive"]=bool(radar.phase0b_full_thread and radar.phase0b_full_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/phase0b-full/result")
def phase0b_full_result():
    report=radar.redis.get_json(radar.phase0b_full_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/phase0/phase0b-full/status","feature_discovery_allowed":False}),202
    return jsonify(report)

@app.get("/phase0/phase0b-window-probe")
def phase0b_window_probe_home():
    allowed,reason=radar._phase0b_window_probe_gate()
    return jsonify({"purpose":"Validate buffered-window 1-minute optimization against full-cycle 1-minute ground truth before Phase 0B","gate_allowed":allowed,"gate_reason":reason,"start_url":"/phase0/phase0b-window-probe/start","status_url":"/phase0/phase0b-window-probe/status","result_url":"/phase0/phase0b-window-probe/result","phase0b_allowed":False})

@app.get("/phase0/phase0b-window-probe/start")
@app.post("/phase0/phase0b-window-probe/start")
def phase0b_window_probe_start():
    started,message=radar.start_phase0b_window_probe()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0/phase0b-window-probe/status","result_url":"/phase0/phase0b-window-probe/result","phase0b_allowed":False}), (202 if started else 409)

@app.get("/phase0/phase0b-window-probe/status")
def phase0b_window_probe_status():
    stored=radar.redis.get_json(radar.phase0b_window_probe_key("status"),None) if radar.redis.configured else None
    with radar.phase0b_window_probe_lock:
        payload=dict(stored or radar.phase0b_window_probe_state); payload["worker_alive"]=bool(radar.phase0b_window_probe_thread and radar.phase0b_window_probe_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/phase0b-window-probe/result")
def phase0b_window_probe_result():
    report=radar.redis.get_json(radar.phase0b_window_probe_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/phase0/phase0b-window-probe/status","phase0b_allowed":False}),202
    return jsonify(report)

@app.get("/phase0/historical-census")
def historical_census_home():
    allowed,reason=radar._historical_census_gate()
    return jsonify({"purpose":"2019-2026 reconstructed-universe high-recall Historical Census","census_id":HISTORICAL_CENSUS_SPEC["census_id"],"historical_census_sha256":HISTORICAL_CENSUS_SHA256,"gate_allowed":allowed,"gate_reason":reason,"protocol_url":"/phase0/historical-census/protocol","start_url":"/phase0/historical-census/start","status_url":"/phase0/historical-census/status","result_url":"/phase0/historical-census/result","pause_url":"/phase0/historical-census/pause","phase0b_allowed":False})

@app.get("/phase0/historical-census/protocol")
def historical_census_protocol():
    return jsonify({"version":VERSION,"build":BUILD,"spec":HISTORICAL_CENSUS_SPEC,"historical_census_sha256":HISTORICAL_CENSUS_SHA256})

@app.get("/phase0/historical-census/start")
@app.post("/phase0/historical-census/start")
def historical_census_start():
    started,message=radar.start_historical_census()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0/historical-census/status","result_url":"/phase0/historical-census/result","phase0b_allowed":False}), (202 if started else 409)

@app.get("/phase0/historical-census/pause")
@app.post("/phase0/historical-census/pause")
def historical_census_pause():
    radar.historical_census_stop_event.set(); return jsonify({"ok":True,"message":"pause_requested","phase0b_allowed":False})

@app.get("/phase0/historical-census/status")
def historical_census_status():
    stored=radar.redis.get_json(radar.historical_census_key("status"),None) if radar.redis.configured else None
    with radar.historical_census_lock:
        payload=dict(stored or radar.historical_census_state); payload["worker_alive"]=bool(radar.historical_census_thread and radar.historical_census_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/historical-census/result")
def historical_census_result():
    report=radar.redis.get_json(radar.historical_census_key("report"),None) if radar.redis.configured else None
    if not report:return jsonify({"result_ready":False,"status_url":"/phase0/historical-census/status","phase0b_allowed":False}),202
    return jsonify(report)

@app.get("/phase0a")
def phase0a_home():
    allowed, reason = radar._phase0a_gate()
    return jsonify({"census_id":PHASE0A_SPEC["census_id"],"phase0a_sha256":PHASE0A_SHA256,"gate_allowed":allowed,"gate_reason":reason,"protocol_url":"/phase0a/protocol","start_url":"/phase0a/start","status_url":"/phase0a/status","result_url":"/phase0a/result","pause_url":"/phase0a/pause","phase0b_allowed":False})

@app.get("/phase0a/protocol")
def phase0a_protocol(): return jsonify({"version":VERSION,"build":BUILD,"spec":PHASE0A_SPEC,"phase0a_sha256":PHASE0A_SHA256})

@app.get("/phase0a/start")
@app.post("/phase0a/start")
def phase0a_start():
    started, message = radar.start_phase0a()
    return jsonify({"ok":started,"status":message,"status_url":"/phase0a/status","result_url":"/phase0a/result","phase0b_allowed":False}), (202 if started else 409)

@app.get("/phase0a/pause")
@app.post("/phase0a/pause")
def phase0a_pause():
    radar.phase0a_stop_event.set(); return jsonify({"ok":True,"message":"pause_requested","phase0b_allowed":False})

@app.get("/phase0a/status")
def phase0a_status():
    stored = radar.redis.get_json(radar.phase0a_key("status"), None) if radar.redis.configured else None
    with radar.phase0a_lock:
        payload = dict(stored or radar.phase0a_state); payload["worker_alive"] = bool(radar.phase0a_thread and radar.phase0a_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0a/result")
def phase0a_result():
    report = radar.redis.get_json(radar.phase0a_key("report"), None) if radar.redis.configured else None
    if not report: return jsonify({"result_ready":False,"status_url":"/phase0a/status","phase0b_allowed":False}), 202
    return jsonify(report)

@app.get("/phase0/probe")
def phase0_probe_home():
    return jsonify({
        "probe_id": PHASE0_PROBE_SPEC["probe_id"], "probe_sha256": PHASE0_PROBE_SHA256,
        "protocol_url": "/phase0/probe/protocol", "status_url": "/phase0/probe/status",
        "start_url": "/phase0/probe/start", "result_url": "/phase0/probe/result",
        "phase0a_implemented": False, "fail_closed": True,
    })

@app.get("/phase0/probe/protocol")
def phase0_probe_protocol():
    return jsonify({"version": VERSION, "build": BUILD, "probe_spec": PHASE0_PROBE_SPEC, "probe_sha256": PHASE0_PROBE_SHA256})

@app.get("/phase0/probe/start")
@app.post("/phase0/probe/start")
def phase0_probe_start():
    # Browser-friendly GET is intentionally allowed for this diagnostic probe only.
    # POST retains the admin-token guard used by the service's mutation endpoints.
    if request.method == "POST" and not export_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    started, message = radar.start_phase0_probe()
    return jsonify({"ok": started, "status": message, "status_url": "/phase0/probe/status", "result_url": "/phase0/probe/result", "phase0a_allowed": False}), (202 if started else 409)

@app.get("/phase0/probe/status")
def phase0_probe_status():
    stored = radar.redis.get_json(radar.phase0_probe_key("status"), None) if radar.redis.configured else None
    with radar.phase0_probe_lock:
        payload = dict(stored or radar.phase0_probe_state)
        payload["worker_alive"] = bool(radar.phase0_probe_thread and radar.phase0_probe_thread.is_alive())
    return jsonify(payload)

@app.get("/phase0/probe/result")
def phase0_probe_result():
    report = radar.redis.get_json(radar.phase0_probe_key("report"), None) if radar.redis.configured else radar.phase0_probe_report
    if not report:
        return jsonify({"result_ready": False, "phase0a_allowed": False, "status_url": "/phase0/probe/status"}), 202
    return jsonify(report)

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
        "phase0_probe_alive": bool(radar.phase0_probe_thread and radar.phase0_probe_thread.is_alive()),
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
