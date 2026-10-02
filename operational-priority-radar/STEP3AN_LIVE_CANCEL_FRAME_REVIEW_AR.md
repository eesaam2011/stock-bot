# Step 3AN: live SIP trade cancel frame after reconnect

Date: 2026-09-24. Source: an isolated, read-only Render probe from commit
`154b1e03e57e87381ee4344d39936b9c20780cc4`, not a deployed bot.
The exact payload-free report is `evidence/STEP3AN_LIVE_RECONNECT_20260924.json`;
its canonical SHA256 is
`37aadb24a9fdd3f77b514bd8aaf76e28bef1bc0cc805436e9d7e42bd796a798d`.

The 90-second probe requested 12,000 symbols and deliberately closed its own
socket after the first ACK. The actual reconnect loop obtained a second ACK
2,046.806 ms after the first disconnect. The first epoch recorded 35,866
received, handled, captured and acknowledged market messages. The second
recorded 263,160 received and handled, but only 263,159 captured and
acknowledged. There was no dispatch or capture overflow, and no worker error.
The sole extra message had type `T=x`. Only the type, never its content, was
retained. Alpaca documents `x` as a trade cancel/error, automatically delivered
with trade subscriptions:
https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data

This is a real market-data semantics gap, not a known control frame. The
existing capture and semantic journal count `b`, `t`, and `s` but do not
reconcile `x`. Accordingly `each_recorded_epoch_exact=false`. Transport
receipt and processing alone do not prove the corrected trade stream or E/B
state. Do not permit a full-session or continuity proof, E/B persistence,
DIRECT, Shadow or retroactive entries on the strength of this probe.

Next: define bounded, ordered cancel/error handling and exact reconciliation
against the associated trade ID, including duplicate, missing original,
pre-session original, and reconnect/REST overlap. Until proved, fail closed
on an `x` frame in the recovery scope. A synthetic correction `T=c` needs the
same treatment; its absence in this short sample is no coverage proof.
