"""Generation-fenced E/B recovery commit with no retroactive side effects."""
from __future__ import annotations

from recovery_commit_permit import validate_recovery_commit_permit
from state_store import validate_record


class RecoveryEBCommitUnsafe(RuntimeError):
    pass


def _identity(token):
    worker = getattr(token, "worker_instance_id", None)
    generation = getattr(token, "leader_generation", None)
    if (not isinstance(worker, str) or not worker
            or isinstance(generation, bool)
            or not isinstance(generation, int) or generation < 1):
        raise RecoveryEBCommitUnsafe("RECOVERY_EB_LEADERSHIP_INVALID")
    return worker, generation


class FencedEBRecoveryCommitter:
    def __init__(self, lua, leadership):
        if not callable(getattr(leadership, "require_current", None)):
            raise ValueError("leadership.require_current required")
        if not callable(getattr(lua, "atomic_recovery_eb", None)):
            raise ValueError("lua.atomic_recovery_eb required")
        self.lua = lua
        self.leadership = leadership

    def commit(self, records, permit, *, coverage_scope_symbols=None):
        if (not isinstance(records, (list, tuple)) or not records
                or len(records) > 80):
            raise RecoveryEBCommitUnsafe("RECOVERY_EB_BATCH_INVALID")
        before = self.leadership.require_current()
        worker, generation = _identity(before)
        sessions = set(); symbols = set()
        for record in records:
            if not isinstance(record, dict) or record.get("record_type") not in {
                    "early_core", "base_ready"}:
                raise RecoveryEBCommitUnsafe("RECOVERY_EB_RECORD_INVALID")
            validate_record(record, record["record_type"])
            if (record.get("worker_instance_id") != worker
                    or record.get("leader_generation") != generation):
                raise RecoveryEBCommitUnsafe("RECOVERY_EB_RECORD_LEADERSHIP_MISMATCH")
            sessions.add(record["session"]); symbols.add(record["symbol"])
        if len(sessions) != 1:
            raise RecoveryEBCommitUnsafe("RECOVERY_EB_SESSION_MISMATCH")
        scope=(sorted(coverage_scope_symbols)
               if coverage_scope_symbols is not None else sorted(symbols))
        if (not scope or len(set(scope))!=len(scope)
                or any(not isinstance(s,str) or not s for s in scope)
                or not symbols.issubset(scope)):
            raise RecoveryEBCommitUnsafe("RECOVERY_EB_COVERAGE_SCOPE_INVALID")
        proof = validate_recovery_commit_permit(
            permit, worker_instance_id=worker, leader_generation=generation,
            session=next(iter(sessions)), symbols=scope)
        result = self.lua.atomic_recovery_eb(
            worker, records, generation, coverage_permit=permit,
            coverage_scope_symbols=scope)
        after = self.leadership.require_current()
        if _identity(after) != (worker, generation):
            raise RecoveryEBCommitUnsafe("RECOVERY_EB_LEADERSHIP_CHANGED_AFTER_COMMIT")
        if not isinstance(result, dict) or set(result) != {
                "inserted", "already_identical"}:
            raise RecoveryEBCommitUnsafe("RECOVERY_EB_RESULT_INVALID")
        return {
            **result,
            "permit_sha256": proof["permit_sha256"],
            "full_session_coverage_proven": True,
            "retroactive_entries_created": 0,
            "retroactive_entries_allowed": False,
            "direct_handoff_authorized": False,
            "shadow_deploy_authorized": False,
        }
