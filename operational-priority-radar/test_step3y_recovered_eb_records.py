"""Step3Y: deterministic recovered signal -> canonical E/B conversion."""
import copy
import math
import unittest
from dataclasses import replace
from datetime import timedelta

from recovered_eb_records import (
    RecoveredEBRecordUnsafe, recovered_signals_to_record_batches,
)
from state_store import validate_record
from test_step3h_canonical_audit import observed, START, NOW


class TestRecoveredEBRecords(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.signals, _ = observed()

    def build(self, signals=None, scope=("A",), **kwargs):
        return recovered_signals_to_record_batches(
            self.signals if signals is None else signals,
            session="2026-09-22", scope_symbols=list(scope),
            worker_instance_id="W", leader_generation=7, as_of=NOW,
            **kwargs)

    def test_exact_e_and_b_records_are_canonical_and_non_actionable(self):
        batches, audit = self.build()
        records = [r for batch in batches for r in batch]
        self.assertEqual(len(records), 2)
        for record in records:
            validate_record(record, record["record_type"])
            self.assertEqual(record["leader_generation"], 7)
            self.assertEqual(record["worker_instance_id"], "W")
            self.assertEqual(record["decision_available_ts"], NOW.isoformat())
        self.assertEqual(audit["opportunities_created"], 0)
        self.assertEqual(audit["trades_created"], 0)
        self.assertEqual(audit["outboxes_created"], 0)
        self.assertFalse(audit["retroactive_entries_allowed"])
        self.assertFalse(audit["direct_handoff_authorized"])

    def test_records_are_deterministic_independent_of_input_order(self):
        a, aa = self.build(self.signals)
        b, ba = self.build(tuple(reversed(self.signals)))
        self.assertEqual(a, b)
        self.assertEqual(aa["records_sha256"], ba["records_sha256"])

    def test_duplicate_scope_and_chronology_fail_closed(self):
        with self.assertRaisesRegex(RecoveredEBRecordUnsafe, "DUPLICATE"):
            self.build(self.signals + self.signals[:1])
        with self.assertRaises(RecoveredEBRecordUnsafe):
            self.build(scope=("A", "A"))
        bad = replace(self.signals[0], decision_available_ts=START)
        with self.assertRaisesRegex(RecoveredEBRecordUnsafe, "CHRONOLOGY"):
            self.build((bad,))

    def test_wrong_session_symbol_or_generation_rejected(self):
        wrong_session = replace(self.signals[0], session="2026-09-21")
        outside = replace(self.signals[0], symbol="OUTSIDE")
        for signals in ((wrong_session,), (outside,)):
            with self.assertRaisesRegex(RecoveredEBRecordUnsafe, "SCOPE"):
                self.build(signals)
        with self.assertRaises(RecoveredEBRecordUnsafe):
            recovered_signals_to_record_batches(
                self.signals, session="2026-09-22", scope_symbols=["A"],
                worker_instance_id="W", leader_generation=True, as_of=NOW)

    def test_invalid_e_and_b_metrics_rejected(self):
        e = next(s for s in self.signals if s.kind == "E")
        b = next(s for s in self.signals if s.kind == "B")
        with self.assertRaisesRegex(RecoveredEBRecordUnsafe, "E_METRICS"):
            self.build((replace(e, score=math.nan),))
        bad_features = copy.deepcopy(b.features); bad_features.pop("opportunity")
        with self.assertRaisesRegex(RecoveredEBRecordUnsafe, "B_METRICS"):
            self.build((replace(b, features=bad_features),))

    def test_large_reconstruction_is_split_into_80_record_batches(self):
        templates = self.signals
        signals = []
        scope = []
        for index in range(41):
            symbol = f"S{index:03d}"; scope.append(symbol)
            for signal in templates:
                signals.append(replace(signal, symbol=symbol))
        batches, audit = self.build(tuple(signals), scope=tuple(scope))
        self.assertEqual([len(batch) for batch in batches], [80, 2])
        self.assertEqual(audit["record_count"], 82)
        self.assertEqual(audit["batch_count"], 2)


if __name__ == "__main__":
    unittest.main()
