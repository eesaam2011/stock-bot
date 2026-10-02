"""Step3AH: finalized E/B sessions alone may reach the fenced committer."""
import copy
import unittest

from recovered_eb_records import recovered_records_sha256
from recovery_session_committer import (
    RecoverySessionCommitUnsafe, commit_finalized_eb_session,
)
from sip_semantic_digest import canonical_sha256
from test_step3h_canonical_audit import observed, record
from test_step3t_recovery_commit_permit import Token, permit


SYMBOLS = ["A"]


class Leadership:
    def __init__(self, changed=False): self.calls = 0; self.changed = changed
    def require_current(self):
        self.calls += 1
        if self.changed and self.calls > 1:
            return type("Other", (), {"worker_instance_id": "X",
                                       "leader_generation": 2})()
        return Token()


class Committer:
    def __init__(self): self.calls = []
    def commit(self, rows, proof, *, coverage_scope_symbols):
        self.calls.append((rows, proof, coverage_scope_symbols))
        return {"inserted": len(rows), "already_identical": 0}


def finalized():
    signals, _ = observed()
    records = [record(signal) for signal in signals]
    proof = permit()
    records_sha = recovered_records_sha256(
        records, session="2026-09-22", scope_symbols=SYMBOLS)
    record_audit = {
        "schema": "OPR_RECOVERED_EB_RECORD_BATCHES_V1",
        "session": "2026-09-22", "scope_symbol_count": 1,
        "record_count": 2, "batch_count": 1,
        "max_records_per_batch": 80, "records_sha256": records_sha,
        "canonical_record_types": ["early_core", "base_ready"],
        "opportunities_created": 0, "trades_created": 0,
        "outboxes_created": 0, "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    audit = {
        "schema": "OPR_EMPTY_SCOPE_SESSION_FINALIZATION_V1",
        "session": "2026-09-22", "worker_instance_id": "W",
        "leader_generation": 1, "symbol_count": 1,
        "symbols_sha256": proof["symbols_sha256"],
        "market_session_evidence_sha256": "a" * 64,
        "semantic_evidence_sha256": "b" * 64,
        "permit_sha256": proof["permit_sha256"],
        "records_sha256": records_sha, "record_count": 2,
        "record_batch_count": 1, "native_batch_count": 1,
        "rest_pagination_proven": True, "sip_bars_reconciled": True,
        "sip_trades_reconciled": True, "sip_statuses_reconciled": True,
        "full_session_coverage_proven": True, "redis_writes": 0,
        "opportunities_created": 0, "trades_created": 0,
        "outboxes_created": 0, "retroactive_entries_allowed": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
    }
    audit["finalization_sha256"] = canonical_sha256(audit)
    return {"permit": proof, "record_batches": (tuple(records),),
            "record_audit": record_audit, "finalization_audit": audit}


class TestRecoverySessionCommitter(unittest.TestCase):
    def test_valid_finalization_commits_only_eb_records(self):
        committer = Committer()
        result = commit_finalized_eb_session(
            finalized(), committer, Leadership(), SYMBOLS)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(len(committer.calls), 1)
        self.assertEqual(result["opportunities_created"], 0)
        self.assertFalse(result["retroactive_entries_allowed"])

    def test_record_or_finalization_tamper_blocks_before_commit(self):
        for mutate in (
            lambda value: value["record_batches"][0][0].__setitem__(
                "score", 999),
            lambda value: value["finalization_audit"].__setitem__(
                "direct_handoff_authorized", True),
        ):
            value = copy.deepcopy(finalized()); mutate(value)
            committer = Committer()
            with self.assertRaises(Exception):
                commit_finalized_eb_session(
                    value, committer, Leadership(), SYMBOLS)
            self.assertEqual(committer.calls, [])

    def test_oversized_batch_and_wrong_scope_fail_before_commit(self):
        value = finalized()
        value["record_batches"] = (tuple(
            copy.deepcopy(value["record_batches"][0][0])
            for _ in range(81)),)
        for candidate, scope in ((value, SYMBOLS), (finalized(), ["B"])):
            committer = Committer()
            with self.assertRaises(Exception):
                commit_finalized_eb_session(
                    candidate, committer, Leadership(), scope)
            self.assertEqual(committer.calls, [])

    def test_resigned_cross_batch_identity_tamper_is_preflighted(self):
        value = finalized()
        rows = [copy.deepcopy(row) for row in value["record_batches"][0]]
        rows[1]["worker_instance_id"] = "other"
        value["record_batches"] = ((rows[0],), (rows[1],))
        sha = recovered_records_sha256(
            rows, session="2026-09-22", scope_symbols=SYMBOLS)
        value["record_audit"].update({
            "records_sha256": sha, "batch_count": 2,
        })
        value["finalization_audit"].update({
            "records_sha256": sha, "record_batch_count": 2,
        })
        value["finalization_audit"].pop("finalization_sha256")
        value["finalization_audit"]["finalization_sha256"] = canonical_sha256(
            value["finalization_audit"])
        committer = Committer()
        with self.assertRaisesRegex(
                RecoverySessionCommitUnsafe, "SCOPE_OR_LEADERSHIP"):
            commit_finalized_eb_session(
                value, committer, Leadership(), SYMBOLS)
        self.assertEqual(committer.calls, [])

    def test_leadership_change_after_batches_never_declares_success(self):
        with self.assertRaisesRegex(
                RecoverySessionCommitUnsafe, "CHANGED_AFTER_BATCHES"):
            commit_finalized_eb_session(
                finalized(), Committer(), Leadership(changed=True), SYMBOLS)


if __name__ == "__main__":
    unittest.main()
