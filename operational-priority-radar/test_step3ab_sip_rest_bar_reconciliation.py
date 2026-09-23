"""Step3AB: exact native REST/SIP bar multiset reconciliation."""
import copy
import unittest

from native_session_recovery_batches import (
    combine_native_session_batches, recover_native_session_batch,
)
from production_sip_semantic_journal import ProductionSIPSemanticJournal
from sip_drain_coordinator import BoundedSIPDrainCoordinator
from sip_epoch_capture import BoundedEpochCapture
from sip_rest_bar_reconciliation import (
    SIPRESTBarReconciliationUnsafe, reconcile_sip_rest_bars,
)
from test_step3aa_sip_semantic_journal import FakeLua, Leadership
from test_step3z_native_session_recovery_batches import (
    AS_OF, END, REST, START, bar,
)


def evidence():
    batches = []
    for index, symbols in enumerate((["A"], ["B"])):
        batches.append(recover_native_session_batch(
            REST(), symbols, session="2026-09-24", session_start=START,
            session_end=END, as_of=AS_OF, batch_index=index))
    _, native = combine_native_session_batches(
        batches, expected_symbols=["A", "B"])
    lua = FakeLua()
    journal = ProductionSIPSemanticJournal(
        lua, "2026-09-24", ["A", "B"],
        bar_window_start=START.isoformat(), bar_window_end=END.isoformat())
    capture = BoundedEpochCapture(128, 1024 * 1024)
    capture.start(7); capture.begin_drain(7)
    for symbol in ("A", "B"):
        for minute in range(30):
            capture.ingest(7, {**bar(symbol, START.replace(
                minute=START.minute + minute), "1m"), "T": "b"},
                received_at=AS_OF)
    drain = BoundedSIPDrainCoordinator(
        capture, Leadership(), journal.reconcile_batch, batch_size=16)
    drain.drain_available(7, max_batches=16)
    return native, journal.snapshot("W", 3, 7)


class TestSIPRESTBarReconciliation(unittest.TestCase):
    def test_exact_multiset_issues_narrow_bar_proof(self):
        native, semantic = evidence()
        proof = reconcile_sip_rest_bars(native, semantic)
        self.assertEqual(proof["bar_count"], 60)
        self.assertTrue(proof["sip_bars_reconciled"])
        self.assertEqual(proof["bar_reconciliation_gaps"], 0)
        self.assertEqual(proof["bar_reconciliation_conflicts"], 0)
        self.assertFalse(proof["sip_trades_reconciled"])
        self.assertFalse(proof["sip_statuses_reconciled"])
        self.assertFalse(proof["full_session_coverage_proven"])

    def test_count_digest_scope_and_window_mismatch_fail_closed(self):
        native, semantic = evidence()
        mutations = (
            ("bar_window_count", semantic["bar_window_count"] - 1),
            ("bar_multiset_sha256", "f" * 64),
            ("symbols_sha256", "e" * 64),
            ("bar_window_end_utc", AS_OF.isoformat()),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                bad = copy.deepcopy(semantic); bad[field] = value
                bad.pop("snapshot_sha256")
                from sip_semantic_digest import canonical_sha256
                bad["snapshot_sha256"] = canonical_sha256(bad)
                with self.assertRaises(SIPRESTBarReconciliationUnsafe):
                    reconcile_sip_rest_bars(native, bad)

    def test_tampered_unsigned_inputs_are_rejected(self):
        native, semantic = evidence()
        bad_native = copy.deepcopy(native); bad_native["native_1m_rows"] += 1
        with self.assertRaisesRegex(SIPRESTBarReconciliationUnsafe, "DIGEST"):
            reconcile_sip_rest_bars(bad_native, semantic)
        bad_semantic = copy.deepcopy(semantic); bad_semantic["item_count"] += 1
        with self.assertRaisesRegex(SIPRESTBarReconciliationUnsafe, "DIGEST"):
            reconcile_sip_rest_bars(native, bad_semantic)


if __name__ == "__main__":
    unittest.main()
