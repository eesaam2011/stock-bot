"""Step3AG: exact write-free production session finalization."""
import copy
import unittest
from dataclasses import dataclass
from datetime import timedelta

from empty_trade_status_disposition import snapshot_empty_active_trade_scope
from recovery_session_finalizer import (
    RecoverySessionFinalizationUnsafe,
    finalize_empty_scope_session_recovery,
    recover_native_full_scope,
)
from test_step3ad_full_session_semantic_validation import (
    END, START, SYMBOLS, valid_evidence,
)


@dataclass(frozen=True)
class Token:
    worker_instance_id: str = "W"
    leader_generation: int = 3


class Leadership:
    def __init__(self, change_after=None):
        self.calls = 0
        self.change_after = change_after

    def require_current(self):
        self.calls += 1
        if self.change_after is not None and self.calls > self.change_after:
            return Token("successor", 4)
        return Token()


class Reader:
    def __init__(self):
        self.r = object()

    def active_trades(self):
        return []


class Lock:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def acquire(self): return True
    def release(self): return None


class Pipeline:
    def __init__(self, redis):
        self.r = redis
        self.decision_lock = Lock()

    def _decision_gate_open(self):
        return False


class REST:
    def __init__(self):
        self.calls = []

    def native_recovery_batch_audited(self, symbols, start, end, **kwargs):
        self.calls.append(tuple(symbols))
        one = {symbol: [] for symbol in symbols}
        if "A" in one:
            one["A"] = [{
                "S": "A", "t": START.isoformat(), "o": 10.0,
                "h": 10.2, "l": 9.9, "c": 10.1, "v": 1000,
            }]
        five = {symbol: [] for symbol in symbols}
        lane = {"api_pagination_exhausted": True, "pages": 1}
        return one, five, {
            "native_1m": dict(lane), "native_5m": dict(lane),
            "both_api_page_chains_exhausted": True,
        }


def scope_snapshot(leadership=None):
    reader = Reader()
    return snapshot_empty_active_trade_scope(
        reader, leadership or Leadership(), "2026-09-24",
        pipeline=Pipeline(reader.r),
        captured_at=(START - timedelta(minutes=10)).isoformat())


class TestRecoverySessionFinalizer(unittest.TestCase):
    def test_exact_path_produces_permit_and_records_without_writes(self):
        leadership = Leadership()
        result = finalize_empty_scope_session_recovery(
            valid_evidence(), REST(), SYMBOLS, scope_snapshot(), leadership,
            as_of=END + timedelta(minutes=5))
        audit = result["finalization_audit"]
        self.assertTrue(audit["full_session_coverage_proven"])
        self.assertEqual(audit["redis_writes"], 0)
        self.assertEqual(audit["opportunities_created"], 0)
        self.assertFalse(audit["retroactive_entries_allowed"])
        self.assertFalse(audit["direct_handoff_authorized"])
        self.assertFalse(audit["shadow_deploy_authorized"])
        self.assertEqual(result["permit"]["leader_generation"], 3)

    def test_full_scope_fetch_is_partitioned_into_at_most_eighty(self):
        symbols = [f"S{i:03d}" for i in range(81)]
        rest = REST()
        results = recover_native_full_scope(
            rest, symbols, session="2026-09-24", session_start=START,
            session_end=END, as_of=END + timedelta(minutes=5))
        self.assertEqual([len(call) for call in rest.calls], [80, 1])
        self.assertEqual(len(results), 2)

    def test_stale_snapshot_leadership_fails_before_rest(self):
        rest = REST()
        with self.assertRaisesRegex(
                RecoverySessionFinalizationUnsafe, "SCOPE_LEADERSHIP_STALE"):
            finalize_empty_scope_session_recovery(
                valid_evidence(), rest, SYMBOLS,
                scope_snapshot(Leadership()),
                Leadership(change_after=0),
                as_of=END + timedelta(minutes=5))
        self.assertEqual(rest.calls, [])

    def test_leadership_change_during_rest_blocks_permit(self):
        # Calls: initial token, then one check after the first REST batch.
        leadership = Leadership(change_after=1)
        with self.assertRaisesRegex(
                RecoverySessionFinalizationUnsafe, "CHANGED_DURING_REST"):
            finalize_empty_scope_session_recovery(
                valid_evidence(), REST(), SYMBOLS, scope_snapshot(),
                leadership, as_of=END + timedelta(minutes=5))

    def test_tampered_live_evidence_fails_before_rest(self):
        evidence = copy.deepcopy(valid_evidence())
        evidence["terminal"]["received"] += 1
        rest = REST()
        with self.assertRaises(Exception):
            finalize_empty_scope_session_recovery(
                evidence, rest, SYMBOLS, scope_snapshot(), Leadership(),
                as_of=END + timedelta(minutes=5))
        self.assertEqual(rest.calls, [])


if __name__ == "__main__":
    unittest.main()
