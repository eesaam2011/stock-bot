# Step 3AV — explicit verified empty-scope recovery integration

This stage composes the existing transport/semantic/REST finalizer and the fenced E/B session committer into one explicit API using one leadership provider. It does not change startup readiness, Render, DIRECT, Shadow or entry decisions.

New tests traverse bounded capture/drain, semantic evidence, native REST replay, permit validation, record construction and actual Redis Lua persistence. Fixtures are synthetic: one symbol, thirty minutes, with a nonempty Early record. They are NOT a six-and-a-half-hour market run and do not establish live Breakout coverage. Exact retry and a generation change immediately before the Lua write are tested. Existing broader B and recovery tests remain separate.

The API supports empty-active-trade recovery only. Individual batches are atomically fenced; a multi-batch failure may leave earlier committed batches. It must not be described as whole-session transactional rollback. Exact retry is idempotent.

## Remaining hard blockers
- No newly verified complete live market session in this package.
- The recorded Step 3AN x frame lacks original trade payload; later code cannot reconstruct missing historical evidence.
- Current revision diagnostics fail closed. The standalone revision auditor is not a production bar-revision/rebuild engine.
- ProductionStartupRecovery still reports CANONICAL_REPLAY_NOT_IMPLEMENTED. This explicit coordinator is not wired into automatic startup or DIRECT transitions.
- Reconnection alone does not prove recovery of missing messages or multi-epoch semantic continuity.
- A real pre-open pipeline snapshot and valid leadership are required; test fixtures cannot authorize production E/B writes.

All previously committed market evidence remains in source/evidence with its original scope. No live credentials or new live-session results are introduced.
