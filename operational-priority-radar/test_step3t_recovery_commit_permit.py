"""Step 3T: full-session permit and double-fenced E/B recovery commit."""
import copy
import hashlib
import json
import unittest

from recovery_commit_permit import (
    SCHEMA, RecoveryCommitPermitUnsafe, symbols_sha256,
    validate_recovery_commit_permit,
)
from recovery_eb_committer import FencedEBRecoveryCommitter, RecoveryEBCommitUnsafe
from redis_lua_production import AtomicConflict, ProductionRedisLua
from test_step3h_canonical_audit import observed, record


def permit(*, worker="W", generation=1, symbols=("A",), received=550546):
    body = {
        "schema": SCHEMA,
        "worker_instance_id": worker,
        "leader_generation": generation,
        "session": "2026-09-22",
        "symbol_count": len(symbols),
        "symbols_sha256": symbols_sha256(list(symbols)),
        "session_start_utc": "2026-09-22T13:30:00+00:00",
        "session_end_utc": "2026-09-22T20:00:00+00:00",
        "as_of_utc": "2026-09-22T20:01:00+00:00",
        "epoch": 7,
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch": True,
        "received": received,
        "handled": received,
        "metrics_acked": received,
        "first_sequence": 1,
        "last_sequence": received,
        "capture_acked_upto": received,
        "capture_buffered": 0,
        "capture_overflow_observed": False,
        "dispatch_overflow_observed": False,
        "dispatch_overflows": 0,
        "rest_pagination_proven": True,
        "native_session_reconciled": True,
        "sip_trades_reconciled": True,
        "sip_statuses_reconciled": True,
        "full_session_coverage_proven": True,
        "market_session_evidence_sha256": "a" * 64,
        "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    body["permit_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    return body


class Token:
    worker_instance_id = "W"
    leader_generation = 1


class Leadership:
    def __init__(self): self.calls = 0; self.changed_after = False
    def require_current(self):
        self.calls += 1
        if self.changed_after and self.calls > 1:
            return type("Other", (), {"worker_instance_id": "X",
                                       "leader_generation": 2})()
        return Token()


class FakeRedis:
    def __init__(self, result=2): self.calls = []; self.result = result
    def eval(self, *args): self.calls.append(args); return self.result


class TestRecoveryCommitPermit(unittest.TestCase):
    def setUp(self):
        signals, _ = observed()
        self.records = [record(s) for s in signals]

    def test_valid_exact_permit_is_narrow(self):
        proof = validate_recovery_commit_permit(
            permit(), worker_instance_id="W", leader_generation=1,
            session="2026-09-22", symbols=["A"])
        self.assertTrue(proof["full_session_coverage_proven"])
        self.assertFalse(proof["retroactive_entries_allowed"])
        self.assertFalse(proof["direct_handoff_authorized"])
        self.assertFalse(proof["shadow_deploy_authorized"])

    def test_every_safety_claim_and_digest_are_fail_closed(self):
        for key, value in (("subscription_ack_verified", False),
                           ("single_uninterrupted_epoch", False),
                           ("rest_pagination_proven", False),
                           ("sip_trades_reconciled", False),
                           ("sip_statuses_reconciled", False),
                           ("capture_overflow_observed", True),
                           ("dispatch_overflow_observed", True),
                           ("retroactive_entries_allowed", True),
                           ("direct_handoff_authorized", True),
                           ("shadow_deploy_authorized", True)):
            bad = permit(); bad[key] = value
            with self.assertRaises(RecoveryCommitPermitUnsafe):
                validate_recovery_commit_permit(
                    bad, worker_instance_id="W", leader_generation=1,
                    session="2026-09-22", symbols=["A"])

    def test_production_lua_rejects_missing_or_wrong_scope_before_redis(self):
        redis = FakeRedis(); lua = ProductionRedisLua(redis)
        with self.assertRaisesRegex(AtomicConflict, "PERMIT_REJECTED"):
            lua.atomic_recovery_eb("W", self.records, 1)
        wrong = permit(symbols=("B",))
        with self.assertRaisesRegex(AtomicConflict, "PERMIT_REJECTED"):
            lua.atomic_recovery_eb("W", self.records, 1,
                                   coverage_permit=wrong)
        self.assertEqual(redis.calls, [])

    def test_valid_commit_is_one_lua_call_and_creates_no_entry(self):
        redis = FakeRedis(); lua = ProductionRedisLua(redis)
        leadership = Leadership()
        result = FencedEBRecoveryCommitter(lua, leadership).commit(
            self.records, permit())
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["retroactive_entries_created"], 0)
        self.assertFalse(result["retroactive_entries_allowed"])
        self.assertFalse(result["direct_handoff_authorized"])
        self.assertEqual(len(redis.calls), 1)
        self.assertEqual(leadership.calls, 2)

    def test_leadership_change_after_atomic_commit_never_declares_success(self):
        redis = FakeRedis(); lua = ProductionRedisLua(redis)
        leadership = Leadership(); leadership.changed_after = True
        with self.assertRaisesRegex(RecoveryEBCommitUnsafe,
                                    "LEADERSHIP_CHANGED_AFTER_COMMIT"):
            FencedEBRecoveryCommitter(lua, leadership).commit(
                self.records, permit())
        self.assertEqual(len(redis.calls), 1)

    def test_record_generation_mismatch_rejected_before_redis(self):
        rows = copy.deepcopy(self.records)
        rows[0]["leader_generation"] = 2
        redis = FakeRedis(); lua = ProductionRedisLua(redis)
        with self.assertRaisesRegex(RecoveryEBCommitUnsafe,
                                    "RECORD_LEADERSHIP_MISMATCH"):
            FencedEBRecoveryCommitter(lua, Leadership()).commit(rows, permit())
        self.assertEqual(redis.calls, [])


if __name__ == "__main__":
    unittest.main()
