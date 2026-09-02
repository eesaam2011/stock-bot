# Evidence-First Explosion Bot — Frozen Phase 1 Protocol

Protocol: `EFB-PREREG-2026-09-02-A`

This phase is research and shadow collection only. It cannot send trading alerts,
place orders, or authorize a live strategy.

## Why this is a new architecture

The previous NDR 93/35 strategy, earlier READY entry, wider stops, scaled exits,
Market Radar approximation, raw 45-minute price change, ER45, distance to
resistance, and simple volume/upper-wick filters did not establish executable
profitability. Their scores may not be reused as entry gates here.

Only two pre-registered event-driven paths are tested:

1. `ORB_5M`: a causal breakout after the completed 09:30–09:34 New York opening
   range.
2. `BREAKOUT_RETEST_RECLAIM`: the same initial breakout, followed by a controlled
   retest and a later reclaim.

The exact numeric rules live in `evidence_first_protocol.json`. Its SHA-256
fingerprint is stored with every run. Changing any rule creates a new protocol;
old cases cannot silently be mixed with it.

## Anti-leakage rules

- Development sessions only during Phase 1.
- The previously viewed 15-session holdout is locked during development and may
  later be used only as a secondary sanity check, not as a fresh final holdout.
- Final validation requires 20 new unseen sessions collected with point-in-time
  quotes, float, and news.
- Signal decisions use only completed bars at or before the signal.
- Entry is the next one-minute bar open.
- If stop and target occur in the same minute, the stop is assumed first.
- One trade per symbol/session/path.
- A result from an outcome-selected or non-causal universe is exploratory and is
  never eligible for live adoption, regardless of profitability.

## Adoption boundary

The engine reports metrics at 0%, 0.25%, and 0.50% round-trip costs. A path must
meet every frozen development gate in the JSON protocol. Even then it is only a
candidate for the new forward holdout; it is not approved for live trading.

