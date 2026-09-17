# Operational Priority Radar v1 — Step 16A

This release fixes the Step 16 short-lived-main gap.

`shadow_worker_main.py` now owns a long-running asyncio supervisor:
leadership -> startup recovery -> WebSocket reconnect loop + durable outbox loop +
10-second leadership renewal -> graceful drain on SIGTERM/SIGINT.

Important deployment gate:
`production_composition.py` intentionally refuses to silently fabricate missing
production components. A real Render deployment still requires concrete Redis
client, WebSocket connector, recovery, Frozen Early Core and BASE_READY instances
to be passed by the production composition/bootstrap layer.

Therefore this package proves the long-running supervisor contract, but is not
yet honestly classified as "live-connected Shadow" until the concrete composition
and real connectivity probes pass.
