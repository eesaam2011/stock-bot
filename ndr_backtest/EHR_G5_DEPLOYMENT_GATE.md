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
