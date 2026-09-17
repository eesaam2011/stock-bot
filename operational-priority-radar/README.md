# Operational Radar v1 — Step 4 Provenance & Equivalence Gate

This gate recovered the actual frozen model artifact and the exact historical source implementation.

Frozen model declared SHA-256: `c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3`
Canonical recomputation SHA-256: `18294ae8f53e308086d0b93b5e6aa9157aa40ac20547f3d0de97c6d884c076c0`
Frozen Early Core threshold: `0.5205528990060366`
Frozen Early Core definitions: `12`

This release intentionally implements only the frozen scoring equation against already-computed feature values.
It does NOT yet claim live feature-extraction equivalence. The next sub-gate must port `_fd_features` literally against native Alpaca 5Min bars and verify fixture equivalence before the operational E engine is accepted.

## Threshold comparator investigation
The exact historical `_ctr_first_signal` path uses `_ctr_score_at` with ordinary Python float arithmetic in frozen definition order and tests `sc >= early_thr` directly. No rounding, Decimal, epsilon, string comparison, or NumPy conversion occurs in the first-crossing decision. The earlier synthetic boundary fixture failed because inverse floating-point construction does not guarantee exact equality after rescoring. No model semantics were changed.
