"""Operational Radar v1 constants derived from the frozen specification."""
SPEC_ID = "OPERATIONAL_V1_FINAL_SPEC_FROZEN"
SPEC_SHA256 = "e013eab05b0f926ec5761e2dac23be6ae41a7ddd4a4a1f28bbd65bbc523b72ea"

EARLY_CORE_THRESHOLD = 0.5205528990060366
CONFLUENCE_WINDOW_SECONDS = 900
MAX_ENTRY_TRADE_AGE_SECONDS = 5
ENTRY_PRICE_WAIT_SECONDS = 10
STRUCTURAL_STOP_BUFFER = 0.005
MAX_INITIAL_RISK_PCT = 6.0
T1_R_MULTIPLE = 1.0
T2_R_MULTIPLE = 2.0
MONITORING_HORIZON_SECONDS = 7200

LEASE_TTL_SECONDS = 30
LEASE_RENEW_SECONDS = 10
ROLLING_1M_BARS = 60
MEMORY_PRESSURE_PCT = 70
MEMORY_CRITICAL_PCT = 80
MEMORY_EMERGENCY_PCT = 90

# Intentionally unresolved by the frozen spec until Shadow benchmark.
SLOW_CONSUMER_THRESHOLD = None
SLOW_CONSUMER_STATUS = "PENDING_SHADOW_BENCHMARK"

BASE_READY_THRESHOLDS = {
    "opportunity_min": 88.0,
    "failure_max": 35.0,
    "demand_min": 65.0,
    "acceptance_min": 62.0,
    "acceleration_min": 1.0,
}

def actionable_alerts_configured() -> bool:
    return SLOW_CONSUMER_THRESHOLD is not None

def require_actionable_alerts_configured() -> None:
    if not actionable_alerts_configured():
        raise RuntimeError(
            "Actionable alerts are blocked until SLOW_CONSUMER_THRESHOLD "
            "is frozen by the post-Shadow Engineering Parameter Manifest."
        )

# Canonical state-store contract (engineering schema, not trading policy).
REDIS_NAMESPACE = "operational_priority_radar:v1"
CANONICAL_SCHEMA_VERSION = 1
