# Dynamic WebSocket validation — preparation only

This document does not launch a session or authorize merge, deployment, Render
changes, DIRECT, executable Shadow or actionable alerts. REST Step 3BI remains
separate and must reach its evidence decision gate first. No fixed number of
future sessions is promised.

## Evidence that may be reused

| Evidence | Valid use | Insufficient claim |
| --- | --- | --- |
| Step 3BD full-universe failure | Regression fixture for unknown action and prior transport limits | Current dynamic composition live success |
| AAOI / Step 3BF scoped probe | Original-trade index, prior scoped transport and late-revision failure evidence | Post-3BH symbol isolation or current end-to-end success |
| Step 3BH tests | Documented revision isolation policy under tested inputs | Full market session, durable restart recovery, affected-trade monitoring |
| Step 3BI | REST maturity and per-symbol coverage | WebSocket evidence |
| Current composition binding tests | Factory binds real isolation callback; synthetic ACK fences and fail-closed outcomes | Provider ACK, live continuity, durable journal or active-trade recovery |

Reuse requires a pinned source SHA, unchanged or demonstrated-equivalent code
on the claimed path, retained raw evidence and explicit coverage of that claim.
Historical failures remain useful fixtures. The reviewed evidence does not
contain a full live dynamic session after all Step 3BE–3BH changes.

## Findings in current composition

`compose_shadow_runtime` wires the capture revision callback to the real
`ProductionDecisionPipeline.quarantine_symbol_revision`, gated by current-epoch
trade ACK authorization. Documented cancels C/E and corrections can block the
affected symbol. Unknown actions, unauthorized symbols, callback refusal and
isolation-capacity exhaustion keep the whole-epoch fail-closed behavior.
Isolation is not correction settlement or continuity proof.

For an active symbol, quarantine removes `pipeline.trades[symbol]` and entry
opportunities while retaining `_active_by_symbol[symbol]` and desired trade
subscription. The offline test records this exact behavior. It does not prove
correct T1/T2/STOP decisions after uncertain corrections, canonical disposition,
durable isolation or recovery after restart. `pipeline.trades` is an entry-price
buffer, not the active-trade monitor. Real `on_trade` still calls `_monitor_trade`
after symbol isolation; an offline routing test verifies that call only, not its
canonical outcomes. A green test is not resolution of this limitation.

The default `ProductionStartupRecovery.run` reports
`CANONICAL_REPLAY_NOT_IMPLEMENTED` on its fetch-audit completion path, and
`continuity_verified(epoch)` explicitly returns false. The current factory
therefore cannot certify executable end-to-end readiness by merely running a
socket session. This is an existing implementation gate, not a live-session
failure or a newly introduced regression.

The injected offline factory excludes the production durable revision journal
branch. Its tests deliberately do not run recovery, semantic draining, the
supervisor, REST, Redis or socket transport. Synthetic ACK assignments never
authorize DIRECT or decisions.

## Gates before writing an executable live-session protocol

1. Finish the independent Step 3BI measurement and >=18-hour comparison, or
   record its outstanding deficiency without presenting it as closed.
2. Define and verify the isolated active trade's explicit terminal/held state,
   canonical persistence, monitoring policy and restart/reconnect recovery.
   Entry-buffer removal is not trade closure or correction settlement. Verify
   whether active monitoring should continue or hold under uncertain revisions.
3. Review actual production startup recovery, leadership fences, durable
   semantic receipts and bounded capture draining together. Demonstrate an
   authoritative continuity gate; never force LIVE_TRUSTED or DIRECT to obtain
   a test result. Missing proof is a blocker, not permission to bypass it.
4. Supply a read-only observational harness using current composition policy
   with explicit audit-only persistence. The previous SIP soak probe alone
   lacks the production isolation binding and cannot certify this path.
   Account for every frame, ACK, revision outcome, queue bound and disconnect;
   preserve partial evidence and independent exit status on failure.
5. Pin source and CI results, record actual provider subscription requests and
   ACKs, enforce narrow scope and session boundaries, and specify safe stop.
   Credentials are entered by the user. No canonical E/B/trade writes or
   actionable alert delivery are permitted by this preparation.

## Required future evidence and decisions

- Prove current-epoch subscribe/unsubscribe behavior, pre-ACK exclusion,
  reconnect fencing and bounded dispatch/drain on actual transport.
- Separate documented revision isolation, unknown-action epoch failure,
  expired-original evidence and affected active-trade disposition. An absent
  live revision remains an uncovered case; offline fixtures must be labelled.
- If no natural CONFLUENCE_VALID opportunity occurs, do not fabricate one or
  count a manually scoped observer as proof of opportunity-driven subscription.
- Record transport observations separately from decisions and continuity.
  A read-only session cannot alone establish executable monitoring readiness.
- Review any Alpaca response about replay/corrections before relying on it;
  an unanswered ticket supplies no provider guarantee.
- Outcomes are PASS for the specifically observed claim, BLOCKED for an unmet
  gate, or INCOMPLETE for missing coverage. Full radar readiness requires both
  REST and dynamic paths plus their integration; alert enablement is a later
  explicit decision.
