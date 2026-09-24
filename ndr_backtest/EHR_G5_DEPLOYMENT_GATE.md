# Same-service pilot option (selected 2026-09-25)

The existing Flask service now registers **disabled-by-default** EHR G5 endpoints. This option does not add a Render service, but it cannot guarantee CPU/memory isolation from the main backtest. For this reason **only a three-symbol pilot is exposed**; a 376-symbol full run remains blocked pending actual CPU/memory measurements, durable storage and cross-process locking.

Deployment gate:
1. Review/merge this PR only after CI succeeds. Verify the current Render service auto-deploy policy before merging.
2. Set `EHR_G5_ENABLED=1` on the existing backtest service **after deployment**, and do not change the existing Start Command. The existing `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` and `NDR_BT_ADMIN_TOKEN` are reused.
3. Open `https://next-day-radar-backtest.onrender.com/ehr-g5` on mobile. Enter the existing admin token locally, choose the frozen JSON, and follow the four buttons. Alternatively POST the frozen JSON as raw `application/json` to `/ehr-g5/requirements`, with the existing admin token in `X-Admin-Token`. This private file is never committed to public GitHub.
4. Only after **17:30 New York time** (00:30 Saudi during US daylight saving), POST `/ehr-g5/start`. GET `/ehr-g5/status` to check progress; download the result with GET `/ehr-g5/download` after completion. All endpoints require admin authorization.
5. The default checkpoint directory `/tmp/ehr_g5_pilot` is **ephemeral**; download the ZIP immediately. To preserve across deploys, configure `EHR_G5_OUTPUT_DIR` to an existing mounted persistent directory, if available. This does not alter Redis.
6. No full-cohort execution, no returns, no corporate-action verification and no trading are authorized by this pilot.

# EHR G5 isolated collection — deployment gate (NOT ACTIVE)

This branch adds only a standalone, read-only SIP daily collector. It is deliberately **not imported** by `ndr_backtest_service.py`, not merged to `main`, and not running on Render.

## Why not run in the existing web process?
The backtest web service shares CPU/memory with requests and existing background threads. Running 376-symbol HTTP collection there without observed resource limits would risk its primary job. Use a **separate Render background worker or one-off job in the same Render project** with the same GitHub repository and separately scoped environment variables. Do not alter the existing web service's Start Command or restart its current jobs.

## Required setup before any run
1. Attach the frozen file `EHR-G5-PRICE-COVERAGE-REQUIREMENTS-2026-09-25.json` to the worker at an explicit private path (e.g. `/opt/render/project/src/ndr_backtest/ehr_g5_requirements.json`). It contains 583 cases and 376 distinct symbols; do not substitute a fresh symbol list. Record SHA256 of the provisioned file.
2. Provision `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` to the **worker only**, through Render secret environment settings; never commit or paste them.
3. Mount a persistent disk to the **worker**, e.g. `/var/data/ehr_g5`. The collector requires an absolute checkpoint directory. Verify disk and plan support before deployment.
4. Run credential-free CI tests on the PR. Confirm no edits to existing service and no auto-deploy from the PR branch.
5. Run a three-symbol pilot with `--max-symbols 3 --end 2026-09-22`; inspect `collection_manifest.json` and individual per-symbol JSON. **Note:** the pilot uses the first three symbols in alphabetical order, not necessarily LUCY/ACTU/SVRE.
6. Only then run all 376 with `--max-symbols 0` and a conservative pause. Run outside the regular session if both services share account rate limits.
7. On retry, completed symbols are skipped if source/start/end match. Any failed symbols are retried. Do not consider the task complete merely because the manifest exists.

## Worker command (from repository root)
```bash
python ndr_backtest/ehr_g5_isolated_collector.py \
  --requirements /opt/render/project/src/ndr_backtest/ehr_g5_requirements.json \
  --output /var/data/ehr_g5 \
  --end 2026-09-22 --max-symbols 3 --pause 1
```
After pilot validation, set `--max-symbols 0`.

## Safety and scientific limits
- Raw daily SIP bars are **coverage data**, not certified official primary-exchange open/close. Compare with an official consolidated or exchange reference before treating entry/exit prices as executable.
- Alpaca raw bars are not corporate-action-adjusted; verify splits, reverse splits, symbol changes, mergers, delistings, and halts separately.
- Historical delisted symbols may need permanent security IDs and an additional data provider. Missing prices must remain unresolved, never silently drop failed symbols.
- H20 for late-August signals requires prices through **2026-09-28**; do not request future bars or infer incomplete outcomes.
- No trading, no Telegram, no Redis writes, no return calculations, no changes to the live bot.
- If Render cannot isolate worker CPU/memory from the existing web service, **do not start** collection there; use an independent instance.
