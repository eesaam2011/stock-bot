# Step 3AP: full-session message-kind gate

Step 3AN observed one real Alpaca `T=x` trade cancel/error after a verified
reconnect. Alpaca documents `x` and `c` as automatically delivered with a
trade subscription; neither is a harmless WebSocket control response.
Source: https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data

`adjudicate_full_session_transport` now requires type-checked per-epoch
`market_data_received == received`, `known_control_received == 0`,
`unknown_nonmarket_received == 0`, and a BAR/TRADE/STATUS ledger whose three
counts sum to `received`. An old report missing these fields cannot be
grandfathered into transport attestation. A forged total that masks one
unknown frame still fails. This strengthens the transport gate even when
`received == handled == metrics_acked` is otherwise true.

This gate remains transport-only. It does not implement revision reconciliation
or establish an uninterrupted live market session, upstream completeness,
canonical E/B replay, persistence, DIRECT or Shadow. Step 3AO still stops an
actual revision frame before capture ACK. Reconciliation of original trade IDs
and revised bars remains an independent prerequisite.
