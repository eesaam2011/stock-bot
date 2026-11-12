R10.8 review only. Base: R10.4 exact approved ZIP.
Read REPORT_R108_AR.txt first.
Verify: sha256sum -c SHA256SUMS.txt
Tests: python3 -I run_tests.py
Result captured: 423 tests, OK, 125.963 seconds.
Both independent source switches are in ipr/review_flags.py, lines 2 and 3.
No R10.5 clock dispatcher or R10.6 fast transport is included.
The inherited full-day test rewrites DAY_FULL_SNAPSHOT_RESULT_R9_CORE.json; its R10.4 bytes were restored after this run.
Legacy reports/results in the base are historical; REPORT_R108_AR.txt, VALIDATION_R108.json and TESTS_R108_COMPLETE_RAW.txt describe this review.
