# Step 3BA — consolidated durable revision/recovery/commit safety

## Delivered together
- Fenced, bounded Redis revision evidence (128 records, each <=16 KiB diagnostic). Exact repeat is idempotent. Multiple distinct revisions are retained; overflow leaves a persistent blocking marker rather than silently dropping earlier evidence. No TTL and no clearing API. Global unresolved key intentionally blocks subsequent sessions too.
- Actual production disconnect composition offloads persistence outside its decision lock. Journal writes check lease owner and generation atomically. Persistence errors mark the recovery failed and cancel it.
- On process restart, startup reads the durable journal before ordinary recovery and refuses unresolved revisions. Single pending native replay can be reconstructed from the journal without the old in-memory slot. A changed journal during REST rejects that candidate.
- E/B Lua batch checks the unresolved journal key inside the same atomic operation as leadership checks and writes. This covers a revision journal append between permit preflight and Lua commit. The check is a veto, not a completeness proof. Already committed prior batches are not rolled back.
- Real Redis tests cover actual composed disconnect, restart, multiple revisions, exact retries, overflow, corrupt storage, leadership loss, write-race veto, pending native replay and full synthetic native-session replay through the existing E/B commit gate.

## Four requested workstreams — honest status
1. Correction/cancel settlement: matched original and native replay candidates exist. **Provider correction incorporation and corrected-bar equivalence are NOT proven.** No arithmetic OHLCV patch or invented native5 is introduced.
2. Automatic recovery: automatic evidence persistence/restart barrier now integrated. **Automatic scheduling/reconciliation of multiple pending revisions and complete gaps is unfinished.** A memory-to-Redis crash window or Redis write failure is not claimed as durable receipt. Reconnect alone remains untrusted.
3. E/B persistence / live trust: atomic durable-revision veto now added to existing owner/generation/permit gates. **No DIRECT/Shadow authorization**, active/halted-trade historical coverage remains required. Journal absence does not prove complete revision coverage.
4. Integrated validation: synthetic failure paths and actual Redis gates exercised. **No new live session**, no assertion that injected tests prove historical SIP completeness.

## Remaining evidence/implementation boundary
A successful or repeated stable REST response cannot itself acknowledge a specific correction. Current artifacts lack independently complete gap/revision coverage. Without that evidence, this stage must not clear the journal, authorize revised E/B persistence, overwrite canonical states, or set LIVE_TRUSTED. A future resolver needs a separately validated correction/coverage contract; merely passing a boolean is insufficient.

All behavior is code in the Draft PR only. No main merge, deployment, Render service change or live alert activation. Redis used for tests is isolated localhost DB15, not production.
