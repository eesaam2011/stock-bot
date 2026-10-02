"""Step3AE: trade/status reconciliation is admissible only for empty scope."""
import copy
import threading
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone

from empty_trade_status_disposition import (
    EmptyTradeStatusUnsafe, issue_empty_trade_status_disposition,
    snapshot_empty_active_trade_scope,
)
from sip_semantic_digest import canonical_sha256


SESSION = "2026-09-24"
START = datetime(2026, 9, 24, 13, 30, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Token:
    worker_instance_id: str = "W"
    leader_generation: int = 4


class Leadership:
    def __init__(self): self.calls = 0
    def require_current(self): self.calls += 1; return Token()


class Reader:
    def __init__(self, values=((), ()), redis=None):
        self.values = list(values); self.r = redis or object()
    def active_trades(self): return list(self.values.pop(0))


class Pipeline:
    def __init__(self, redis, gate=False):
        self.r = redis; self.decision_lock = threading.RLock(); self.gate = gate
    def _decision_gate_open(self): return self.gate


def validation():
    body = {
        "schema": "OPR_FULL_SESSION_SEMANTIC_VALIDATION_V1",
        "session": SESSION,
        "session_start_utc": START.isoformat(),
        "session_end_utc": "2026-09-24T20:00:00+00:00",
        "market_session_evidence_sha256": "a" * 64,
        "semantic_ledger_snapshot_sha256": "b" * 64,
        "epoch": 1, "received": 100, "handled": 100,
        "metrics_acked": 100, "first_sequence": 1, "last_sequence": 100,
        "symbol_count": 2, "symbols_sha256": "c" * 64,
        "bar_count": 10, "trade_count": 89, "status_count": 1,
        "bar_window_count": 10, "semantic_chain_sha256": "d" * 64,
        "bar_multiset_sha256": "e" * 64,
        "subscription_ack_verified": True,
        "single_uninterrupted_epoch_observed": True,
        "all_received_messages_semantically_validated": True,
        "payloads_retained": 0, "pipeline_writes": 0,
        "sip_bars_reconciled": False, "sip_trades_reconciled": False,
        "sip_statuses_reconciled": False,
        "full_session_coverage_proven": False,
        "direct_handoff_authorized": False,
        "shadow_deploy_authorized": False,
        "retroactive_entries_allowed": False,
    }
    body["semantic_validation_sha256"] = canonical_sha256(body)
    return body


def resign(document, field):
    document = copy.deepcopy(document); document.pop(field, None)
    document[field] = canonical_sha256(document); return document


class TestEmptyTradeStatusDisposition(unittest.TestCase):
    def snapshot(self, reader=None, leadership=None):
        reader = reader or Reader()
        return snapshot_empty_active_trade_scope(
            reader, leadership or Leadership(), SESSION,
            pipeline=Pipeline(reader.r),
            captured_at="2026-09-24T13:20:00+00:00")

    def test_empty_stable_scope_issues_narrow_disposition(self):
        leadership = Leadership(); snap = self.snapshot(leadership=leadership)
        self.assertEqual(leadership.calls, 3)
        proof = issue_empty_trade_status_disposition(validation(), snap)
        self.assertTrue(proof["sip_trades_reconciled"])
        self.assertTrue(proof["sip_statuses_reconciled"])
        self.assertEqual(proof["active_trade_scope_count"], 0)
        self.assertFalse(proof["nonempty_scope_reconciliation_supported"])
        self.assertFalse(proof["full_session_coverage_proven"])
        self.assertFalse(proof["direct_handoff_authorized"])

    def test_any_active_or_halted_trade_blocks_specialized_path(self):
        active = {"trade_id": "T", "state": "ACTIVE_PRE_T1"}
        halted = {"trade_id": "H", "state": "HALTED_ACTIVE"}
        for values in (((active,), ()), ((), (active,)),
                       ((halted,), (halted,))):
            with self.subTest(values=values):
                with self.assertRaisesRegex(EmptyTradeStatusUnsafe, "NOT_EMPTY"):
                    self.snapshot(reader=Reader(values))

    def test_leadership_change_between_reads_fails_closed(self):
        class Changed:
            def __init__(self): self.generation = 3
            def require_current(self):
                self.generation += 1
                return Token(leader_generation=self.generation)
        with self.assertRaisesRegex(EmptyTradeStatusUnsafe, "CHANGED"):
            self.snapshot(leadership=Changed())

    def test_pipeline_identity_and_closed_gate_are_mandatory(self):
        reader = Reader()
        with self.assertRaisesRegex(EmptyTradeStatusUnsafe, "ARGUMENT"):
            snapshot_empty_active_trade_scope(
                reader, Leadership(), SESSION, pipeline=Pipeline(object()),
                captured_at="2026-09-24T13:20:00+00:00")
        with self.assertRaisesRegex(EmptyTradeStatusUnsafe, "GATE_OPEN"):
            snapshot_empty_active_trade_scope(
                reader, Leadership(), SESSION, pipeline=Pipeline(reader.r, True),
                captured_at="2026-09-24T13:20:00+00:00")

    def test_snapshot_must_precede_session_and_match_session(self):
        snap = self.snapshot()
        late = copy.deepcopy(snap)
        late["captured_at_utc"] = "2026-09-24T13:31:00+00:00"
        late = resign(late, "scope_snapshot_sha256")
        with self.assertRaisesRegex(EmptyTradeStatusUnsafe, "BEFORE_SESSION"):
            issue_empty_trade_status_disposition(validation(), late)
        wrong = copy.deepcopy(snap); wrong["session"] = "2026-09-23"
        wrong = resign(wrong, "scope_snapshot_sha256")
        with self.assertRaisesRegex(EmptyTradeStatusUnsafe, "SESSION"):
            issue_empty_trade_status_disposition(validation(), wrong)

    def test_tampering_and_scope_escalation_fail(self):
        snap = self.snapshot(); bad = copy.deepcopy(snap)
        bad["active_trade_count"] = 1
        with self.assertRaisesRegex(EmptyTradeStatusUnsafe, "DIGEST"):
            issue_empty_trade_status_disposition(validation(), bad)
        escalated = copy.deepcopy(snap)
        escalated["direct_handoff_authorized"] = True
        escalated = resign(escalated, "scope_snapshot_sha256")
        with self.assertRaisesRegex(EmptyTradeStatusUnsafe, "REQUIREMENT"):
            issue_empty_trade_status_disposition(validation(), escalated)


if __name__ == "__main__":
    unittest.main()
