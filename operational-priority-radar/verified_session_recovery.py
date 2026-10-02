"""Explicit empty-active-trade recovery; no startup trust or entry activation."""
from recovery_eb_committer import FencedEBRecoveryCommitter
from recovery_session_finalizer import finalize_empty_scope_session_recovery
from recovery_session_committer import commit_finalized_eb_session


def recover_and_commit_verified_empty_session(
        evidence, rest, symbols, scope_snapshot, leadership, lua, *, as_of,
        batch_size=80, max_events_per_batch=100000):
    """Finalize all existing proof gates before constructing any E/B writes.

    Both stages share one leadership provider. Lua still checks owner and
    generation atomically at every batch write. Partial batches on failure
    are not rolled back; exact retries are idempotent. This explicit API is
    intentionally not wired to startup readiness or DIRECT processing.
    """
    committer = FencedEBRecoveryCommitter(lua, leadership)
    finalized = finalize_empty_scope_session_recovery(
        evidence, rest, symbols, scope_snapshot, leadership, as_of=as_of,
        batch_size=batch_size, max_events_per_batch=max_events_per_batch)
    committed = commit_finalized_eb_session(
        finalized, committer, leadership, symbols)
    return {'finalization': finalized, 'commit': committed,
            'direct_handoff_authorized': False,
            'shadow_deploy_authorized': False,
            'retroactive_entries_allowed': False}
