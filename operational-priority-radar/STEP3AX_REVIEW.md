# Step 3AX — revision-triggered native replay candidate

ProductionStartupRecovery exposes an explicit write-free preview_revision_rebuild method. It validates configured symbol scope/session start and cancellation, revalidates the retained raw original/revision pair and its digest, then invokes existing bounded native REST session replay for that symbol, including native5 warmup. A shared current leadership identity must remain unchanged across the REST/replay operation. All reconstructed decisions remain available no earlier than recovery time.

This does NOT arithmetically patch OHLCV or synthesize native5 from minute bars. Native REST responses can lag corrections: successful pagination and replay do NOT prove the revision was incorporated. The output explicitly reports rest_revision_applied_proven=false, no ACK, E/B persistence, continuity, DIRECT, Shadow or retroactive-entry authority.

The method is available on the production recovery component but is NOT automatically invoked by the reconnect supervisor. Startup readiness remains fail-closed. Existing bars/Redis records are not overwritten; the output is a candidate for further reconciliation. Tests use synthetic REST fixtures, not a live market feed.

Tests cover native replay, no retrospective decision time, raw-pair digest tampering, missing original, leadership change during REST, cancellation during REST, configured symbol scope, and the production adapter. Existing suite/Redis gates run unchanged.

Remaining: provider revision incorporation proof, corrected SIP/native bar reconciliation, safe integration into multi-epoch recovery and full live-session evidence. No Render changes, deployment, main merge or alerts enabled.
