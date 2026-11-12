# OPR Operational Policy v1.1 — REST Bar Truth + Bounded Dynamic Trades

Freeze ID: `OPR-OPERATIONAL-POLICY-1.1-2026-09-26-A`

Status: engineering/shadow only. This policy does not authorize DIRECT, actionable alerts, deployment, or a merge to main.

## Canonical signal inputs

- Early Core is evaluated only from native Alpaca SIP REST `5Min` bars.
- BASE_READY is evaluated only from completed Alpaca SIP REST `1Min` bars.
- A REST bar is decision-eligible only after its close plus `90` seconds.
- WebSocket minute bars are transport hints and active-trade observations; they cannot persist E or B.
- No historical pagination result is called finality or complete-market proof.

## Dynamic trade scope

- The broad universe subscribes to `bars` and `statuses`; it does not subscribe to trades.
- A symbol may enter the bounded trade scope only while it is `CONFLUENCE_VALID` waiting for an entry price, or while it has an active OPR trade.
- The maximum dynamic trade scope is `256` symbols. Exceeding it fails closed.
- A SIP trade cannot authorize entry or monitoring until a subscription ACK for the current epoch contains that symbol.
- Trade scope is removed after entry expiry, terminal halt, or terminal trade state.
- Corrections/cancels for the bounded trade scope remain subject to the existing fail-closed revision path.

## Disconnect and recovery

- Disconnect immediately blocks new decisions.
- REST reconstructs mature 1Min and native 5Min bars chronologically.
- E/B completed during an untrusted gap is `OPPORTUNITY_MISSED_DURING_DATA_GAP`; it never creates a retroactive entry.
- Trust resumes only for future mature bars after recovery and current-epoch ACKs.
- Unknown halt state blocks entry. A halt/status event missed during a gap is not inferred from missing bars.
- If stop and target ordering cannot be proven during a gap, the trade result is ambiguous.

## Prospective shadow audit

- Every persisted E/B decision stores its bounded causal input snapshot.
- The same interval is fetched again no earlier than 18 hours later.
- The audit reports bar mutations and E/B decision flips separately.
- Observed zero flips is evidence for the observed sample only, never an Alpaca finality guarantee.
