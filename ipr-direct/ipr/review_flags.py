"""R10.8 independent review switches; no environment or Redis configuration."""
NATIVE_TIMING_ENABLED = True
OUTBOX_MGET_ENABLED = True
NATIVE_FAILURE_NONFATAL_ENABLED = True
RECOVERY_IO_OUTSIDE_LOCK_ENABLED = True
IPR_RECOVERY_DIAG_ENABLED = True  # Review-only sidecar; no Environment/Redis setting.
