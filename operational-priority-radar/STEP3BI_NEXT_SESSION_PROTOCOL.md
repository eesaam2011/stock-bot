# Step 3BI — next REST-only measurement protocol (prepared, not executed)

## Baseline and preserved evidence

Pinned original runner: b28f42315d18b445eca2da314e7767d9371cc9e7.
Original session: OPR_STEP3BI_FULL_SESSION_20261001.
Original audit: 222 records (74 at each of 30/60/90 seconds), 11 represented symbols out of 16 selected, no 120/180 records. Zero bar mutations and decision flips do not prove finality.
Secured Redis export: 223888 bytes; SHA256 e223422b080484e900b9bff26558e6ce10ab30d306ef68a5350ed4405c98b4aa.
The post-session REST symbol re-fetch exactly matches those 222 checkpoint slots; it is not a recording of the original responses and cannot categorically rule out every runtime failure.

## New measurement, kept separate

- Use a new session identifier and the exact CI-verified source commit. Do not overwrite the original session or re-run its preflight.
- REST only, read-only with respect to canonical E/B and trade state; only audit records and evidence files may be written. No executable Shadow, DIRECT, WebSocket or actionable alerts.
- Keep deterministic selection at 16 symbols and sample_every_minutes=30; no replacement based on future results. Keep 30/60/90/120/180 checkpoints, 15-second polling, 45-second allowed lateness, max_observations=1500 and comparison delay >=18 hours.
- Fetch overlap during an active study: max(180, 60 + max(checkpoints) + lateness_seconds), currently 285 seconds. Initial bootstrap remains 65 minutes; normal non-study overlap remains 3 minutes.
- Run before the regular opening and through at least market close +180 seconds +45 seconds. Record explicit timezone-aware start/end and the independent shell exit code. The current runner's poll_at reference predates the REST response; do not call it a response-receipt timestamp or use it as a precise request-latency measure.
- Retain actual-run coverage for every selected symbol: successful fetch calls, missing-symbol/empty responses, unique returned bars, returned row appearances (overlaps included), sampled slots, eligible slots per checkpoint, successful audit records per checkpoint, eligible-but-unrecorded slots, complete five-checkpoint slots and min/max causal input length.
- Bootstrap sampled slots can precede the eligible due-time window. Do not treat every sampled_slots_seen item as an expected recorded observation.
- Coverage memory is bounded: 100000 unique symbol/bar timestamps and 5000 sample slots; exhaustion aborts with partial evidence. No silent eviction.

## Validation and decision gates

1. Verify observations == coverage.total_records == len(audit_keys); include all five checkpoint labels even if zero.
2. Report selected symbols separately from represented symbols, and report zero reasons without inferring no trading in the market.
3. Check eligible_without_record_by_checkpoint is zero and errors is empty; any missing point requires investigation. Demonstrate actual records at all five checkpoints and compare matched symbol/bar slots, rather than only equal aggregate counts.
4. Count complete five-point slots and report boundary/late/missing-data exceptions explicitly. Preserve differences in causal input length; five-point availability does not establish decision-input equivalence or full-session coverage.
5. Export Redis audit values before expiration, attach them, and verify bytes/SHA256. Compare every retained observation only after >=18 hours; report bars_changed and decision_flipped separately by checkpoint and symbol.
6. finality_proven=false and recommended_grace_seconds=null remain enforced. A single complete session is not automatic approval of a smaller grace, live alerts or profitability.

## Current implementation validation

Local full OPR pytest: 603 passed, 61 skipped, 86 subtests passed. Skips include unavailable local real Redis and existing Render tests; no Redis simulator is substituted. Remote CI must supply the existing mandatory real Redis 7 integration checks before the source is considered verified for a new measurement.

No session has been scheduled or launched by this protocol. No merge, deployment, Render setting change or service restart is authorized by this document.
