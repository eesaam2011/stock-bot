"""Step3Z: bounded native session replay over an exact universe partition."""
import copy
import unittest
from datetime import datetime, timedelta, timezone

from native_session_recovery_batches import (
    NativeSessionRecoveryUnsafe, combine_native_session_batches,
    recover_native_session_batch,
)
from recovery_session_replay import EARLY_WARMUP_MINUTES


UTC = timezone.utc
START = datetime(2026, 9, 24, 13, 30, tzinfo=UTC)
END = START + timedelta(minutes=30)
AS_OF = END + timedelta(minutes=2)


def bar(symbol, when, timeframe):
    step = int((when - (START - timedelta(minutes=EARLY_WARMUP_MINUTES))).total_seconds() // 60)
    price = 10 + step / 1000
    row = {"S": symbol, "t": when.isoformat(), "o": price, "h": price + .2,
           "l": price - .1, "c": price + .1, "v": 1000 + step,
           "vw": price, "n": 10}
    if timeframe == "5m":
        row["_timeframe"] = "native_5Min"
    return row


class REST:
    def __init__(self, exhausted=True):
        self.exhausted = exhausted

    def native_recovery_batch_audited(self, symbols, start, end, **kwargs):
        one = {symbol: [bar(symbol, START + timedelta(minutes=i), "1m")
                        for i in range(30)] for symbol in symbols}
        five = {symbol: [bar(symbol, start + timedelta(minutes=i), "5m")
                         for i in range(0, EARLY_WARMUP_MINUTES + 30, 5)]
                for symbol in symbols}
        lane = {"api_pagination_exhausted": self.exhausted, "pages": 2}
        return one, five, {"native_1m": dict(lane), "native_5m": dict(lane),
                           "both_api_page_chains_exhausted": self.exhausted}


def recovered(symbols, index):
    return recover_native_session_batch(
        REST(), symbols, session="2026-09-24", session_start=START,
        session_end=END, as_of=AS_OF, batch_index=index)


class TestNativeSessionRecoveryBatches(unittest.TestCase):
    def test_exact_partition_replays_without_escalating_sip_claims(self):
        first = recovered(["B", "A"], 0)
        second = recovered(["C"], 1)
        signals, audit = combine_native_session_batches(
            [first, second], expected_symbols=["A", "B", "C"])
        self.assertEqual(audit["batch_count"], 2)
        self.assertEqual(audit["symbol_count"], 3)
        self.assertTrue(audit["exact_disjoint_symbol_partition"])
        self.assertTrue(audit["all_rest_page_chains_exhausted"])
        self.assertTrue(audit["native_session_batches_replayed"])
        self.assertEqual(audit["signal_count"], len(signals))
        self.assertFalse(audit["sip_semantics_reconciled"])
        self.assertFalse(audit["full_session_coverage_proven"])
        self.assertFalse(audit["retroactive_entries_allowed"])
        self.assertFalse(audit["direct_handoff_authorized"])
        self.assertFalse(audit["shadow_deploy_authorized"])

    def test_requires_both_terminal_page_chains(self):
        with self.assertRaisesRegex(NativeSessionRecoveryUnsafe, "PAGINATION"):
            recover_native_session_batch(
                REST(False), ["A"], session="2026-09-24",
                session_start=START, session_end=END, as_of=AS_OF,
                batch_index=0)

    def test_overlap_missing_and_noncontiguous_indices_rejected(self):
        a = recovered(["A", "B"], 0)
        b = recovered(["B", "C"], 1)
        with self.assertRaisesRegex(NativeSessionRecoveryUnsafe, "PARTITION"):
            combine_native_session_batches([a, b], expected_symbols=["A", "B", "C"])
        with self.assertRaisesRegex(NativeSessionRecoveryUnsafe, "INCOMPLETE"):
            combine_native_session_batches([a], expected_symbols=["A", "B", "C"])
        c = recovered(["C"], 2)
        with self.assertRaisesRegex(NativeSessionRecoveryUnsafe, "INCOMPLETE"):
            combine_native_session_batches([a, c], expected_symbols=["A", "B", "C"])

    def test_tampered_proof_or_signal_is_rejected(self):
        result = list(recovered(["A"], 0))
        result[1] = copy.deepcopy(result[1])
        result[1]["native_1m_rows"] += 1
        with self.assertRaisesRegex(NativeSessionRecoveryUnsafe, "DIGEST"):
            combine_native_session_batches([tuple(result)], expected_symbols=["A"])
        signals, proof = recovered(["A"], 0)
        with self.assertRaisesRegex(NativeSessionRecoveryUnsafe, "SIGNALS"):
            combine_native_session_batches(
                [(signals[:-1], proof)], expected_symbols=["A"])

    def test_batch_is_bounded_and_requires_explicit_complete_window(self):
        with self.assertRaisesRegex(NativeSessionRecoveryUnsafe, "SCOPE"):
            recovered([f"S{i}" for i in range(81)], 0)
        with self.assertRaisesRegex(NativeSessionRecoveryUnsafe, "WINDOW"):
            recover_native_session_batch(
                REST(), ["A"], session="2026-09-24", session_start=END,
                session_end=START, as_of=AS_OF, batch_index=0)


if __name__ == "__main__":
    unittest.main()
