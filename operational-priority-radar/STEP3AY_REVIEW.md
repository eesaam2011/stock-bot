# Step 3AY — bind revision recovery to actual receipt time

Found a concrete temporal gap in Step 3AX: checking the provider frame timestamp did not establish that a revision had actually been received before the rebuild cutoff. Capture now records an aware UTC received_at timestamp for revisions, and binds the entire allowlisted diagnostic (epoch, watermark, frame, original-pair evidence, receipt) with a canonical digest. Invalid receipt times invalidate capture.

Native revision rebuild verifies the diagnostic digest and refuses a receipt after its cutoff before issuing REST. Its candidate binds the diagnostic digest, receipt and cutoff. Old artifacts without these fields are intentionally rejected; no historical receipt time is invented. These hashes are local integrity bindings, NOT signatures or independent source authentication.

Tests: old event timestamp with late arrival; receipt/epoch tampering; legacy evidence; naive receipt; candidate binding. Existing AX fixtures now provide explicit synthetic receipt times. This is synthetic test evidence, not a new live session.

This closes a necessary temporal condition, not provider correction incorporation. Repeated equal REST results or a later REST request are not alone proof that corrections are applied. Official stream documentation describes corrections and late-trade updated bars, but the reviewed page does not supply a per-correction REST incorporation acknowledgment:
https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data

Production automatic recovery, corrected native-bar reconciliation and complete multi-epoch/live-session evidence remain unfinished. No E/B writes are authorized by this candidate. No Render service change, deployment, merge, DIRECT or Shadow activation.
