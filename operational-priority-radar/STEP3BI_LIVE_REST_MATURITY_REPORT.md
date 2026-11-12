# OPR Step 3BI — live REST maturity evidence

Pinned source: `b28f42315d18b445eca2da314e7767d9371cc9e7`  
Successful CI: https://github.com/eesaam2011/stock-bot/actions/runs/36738063215

## Live session

- Window: `2026-10-01T13:10:24Z` through `2026-10-01T20:05:09Z`.
- Deterministic sample: 16 symbols.
- `observations=222`, `polls=1591`, `skipped_slots=6104`, `errors=[]`.
- An observation is a REST maturity/checkpoint audit record, not a raw SIP/WebSocket message.
- `canonical_writes=0`, `alerts_sent=0`, `actionable_alerts_authorized=false`.
- The separate EXIT file is absent. The result JSON is complete, but an independent shell exit code is not proven.

## Deferred comparison (+18h)

| checkpoint | compared | bars_changed | BASE_READY flips |
|---:|---:|---:|---:|
| 30s | 74 | 0 | 0 |
| 60s | 74 | 0 | 0 |
| 90s | 74 | 0 | 0 |
| 120s | 0 | unavailable | unavailable |
| 180s | 0 | unavailable | unavailable |

Totals: `checked=222`, `compared=222`, `bars_changed=0`, `decision_flipped=0`. Session and commit matched.

## Decision

`finality_proven=false` and `recommended_grace_seconds=null`. No grace checkpoint is selected: this is one session, and 120/180-second evidence is missing. The supported conclusion is **insufficient evidence**, not 30-second finality.

Post-compare local test run: `574 passed, 61 skipped, 86 subtests passed`. The pinned CI run contains successful mandatory Redis 7 output (57/57). No deployment, Render configuration change/restart, main merge, DIRECT, executable Shadow, or actionable alerts occurred.

## Render artifact hashes

- Observe JSON (32434 bytes): `5b16bea159b87f53047d0c6485bac40d45a271424fba90417aa3f54f9faec614`
- Observe runtime log (32456 bytes): `03d8e5c54cdfe27966e8fcc7e109955cb6543509ba0ea7e41d6d48ba8d8576e9`
- Compare JSON (562 bytes): `6614bfa8b83e7c03a10b14b22d76f99f512024b0339fcac1ffa407010da924d6`
- Compare runtime log (562 bytes): `6614bfa8b83e7c03a10b14b22d76f99f512024b0339fcac1ffa407010da924d6`
