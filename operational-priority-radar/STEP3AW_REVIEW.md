# Step 3AW — original trade evidence survives bounded capture drain

The actual BoundedEpochCapture now maintains a separate same-epoch, allowlisted original-trade window (4096 identities, 2 MiB serialized payload ceiling; Python object overhead is additional). ACK removes capture items but not retained originals. On the first correction/cancel, the terminal diagnostic includes the retained original and the existing strict pair audit result before invalidation.

Eviction, malformed originals and repeated retained IDs are explicit. Matching is scoped to the retained window, NOT proof of global ID uniqueness, complete tape, timestamp meaning, corrected bars, continuity or E/B coverage. New epochs and invalidation clear the index. Extra input fields are not retained. Full market-scale performance of this added work has not been measured.

The existing rejection of revisions remains: even a matched pair fails closed. This change repairs evidence loss for future observations; it does not recover Step 3AN's lost original or implement bar repair. Cancellation/correction timestamps are not assumed to be original trade timestamps.

Regression coverage: original survives ACK; cancel/correction matching; count/byte eviction; duplicate identity; conflicting price; wrong symbol; new epoch; malformed fields; defensive copies. The existing runtime terminal tests verify diagnostics survive teardown and do not leak into a new epoch.

Remaining before live authorization: provider-correct bar revision semantics and native5 consistency; production chronological gap recovery/entry blocking; independent multi-epoch completeness and full live-session proof; valid pre-open snapshot and current leadership. Step 3AV's synthetic replay-to-Redis coverage is preserved, not relabeled as live.

No Render change, deployment, merge, DIRECT or Shadow activation.
