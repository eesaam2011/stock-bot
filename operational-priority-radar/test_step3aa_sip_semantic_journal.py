"""Step3AA: every drained SIP item is validated and atomically summarized."""
import copy
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from production_sip_semantic_journal import (
    JOURNAL_SCHEMA, ProductionSIPSemanticJournal, SIPSemanticJournalUnsafe,
    ZERO,
)
from sip_drain_coordinator import BoundedSIPDrainCoordinator, reconciliation_context
from sip_epoch_capture import BoundedEpochCapture


START = datetime(2026, 9, 24, 13, 30, tzinfo=timezone.utc)


def bar(i, symbol="A"):
    price = 10 + i / 100
    return {"T": "b", "S": symbol, "t": (START + timedelta(minutes=i)).isoformat(),
            "o": price, "h": price + .2, "l": price - .1,
            "c": price + .1, "v": 1000 + i}


def trade(i, symbol="A"):
    return {"T": "t", "S": symbol, "t": (START + timedelta(microseconds=i)).isoformat(),
            "p": 10 + i / 1000, "s": i + 1, "i": i, "x": "V"}


def status(i=0):
    return {"T": "s", "S": "A", "t": (START + timedelta(seconds=i)).isoformat(),
            "sc": "T", "sm": "Trading"}


@dataclass(frozen=True)
class Token:
    worker_instance_id: str = "W"
    leader_generation: int = 3


class Leadership:
    def require_current(self):
        return Token()


class FakeRedis:
    def __init__(self):
        self.hashes = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


class FakeLua:
    prefix = "opr:test"

    def __init__(self):
        self.r = FakeRedis()
        self.calls = 0

    def atomic_sip_semantic_batch(self, worker, generation, key, **kw):
        self.calls += 1
        state = self.r.hashes.get(key)
        if state and (int(state["last_sequence"]) == kw["last_sequence"]
                      and int(state["last_batch_first"]) == kw["first_sequence"]
                      and state["last_batch_sha256"] == kw["batch_sha256"]
                      and state["last_batch_chain_sha256"] == kw["next_chain_sha256"]
                      and state["last_batch_semantic_chain_sha256"]
                      == kw["next_semantic_chain_sha256"]
                      and state["last_batch_bar_multiset_sha256"]
                      == kw["next_bar_multiset_sha256"]):
            return {"inserted": False, "idempotent": True}
        last = int(state["last_sequence"]) if state else 0
        transport = state["chain_sha256"] if state else ZERO
        semantic = state["semantic_chain_sha256"] if state else ZERO
        bar_acc = state["bar_multiset_sha256"] if state else ZERO
        if (kw["first_sequence"] != last + 1
                or kw["previous_chain_sha256"] != transport
                or kw["previous_semantic_chain_sha256"] != semantic
                or kw["previous_bar_multiset_sha256"] != bar_acc):
            raise RuntimeError("CHAIN_CONFLICT")
        prior = state or {"item_count": "0", "bar_count": "0",
                          "bar_window_count": "0", "trade_count": "0",
                          "status_count": "0"}
        self.r.hashes[key] = {
            "schema": JOURNAL_SCHEMA,
            "last_sequence": str(kw["last_sequence"]),
            "item_count": str(int(prior["item_count"]) + kw["item_count"]),
            "bar_count": str(int(prior["bar_count"]) + kw["bar_count"]),
            "bar_window_count": str(int(prior["bar_window_count"])
                                    + kw["bar_window_count"]),
            "trade_count": str(int(prior["trade_count"]) + kw["trade_count"]),
            "status_count": str(int(prior["status_count"]) + kw["status_count"]),
            "chain_sha256": kw["next_chain_sha256"],
            "semantic_chain_sha256": kw["next_semantic_chain_sha256"],
            "bar_multiset_sha256": kw["next_bar_multiset_sha256"],
            "last_batch_first": str(kw["first_sequence"]),
            "last_batch_sha256": kw["batch_sha256"],
            "last_batch_chain_sha256": kw["next_chain_sha256"],
            "last_batch_semantic_sha256": kw["semantic_batch_sha256"],
            "last_batch_semantic_chain_sha256": kw["next_semantic_chain_sha256"],
            "last_batch_bar_multiset_sha256": kw["next_bar_multiset_sha256"],
            "epoch": str(kw["epoch"]), "symbols_sha256": kw["symbols_sha256"],
        }
        return {"inserted": True, "idempotent": False}


class TestSIPSemanticJournal(unittest.TestCase):
    def setup(self, batch_size=3):
        lua = FakeLua()
        journal = ProductionSIPSemanticJournal(lua, "2026-09-24", ["A", "B"])
        capture = BoundedEpochCapture(128, 1024 * 1024)
        capture.start(7); capture.begin_drain(7)
        drain = BoundedSIPDrainCoordinator(
            capture, Leadership(), journal.reconcile_batch,
            batch_size=batch_size)
        return lua, journal, capture, drain

    def test_mixed_batch_is_validated_counted_and_payload_free(self):
        lua, journal, capture, drain = self.setup()
        for message in (bar(0), trade(1), status(2)):
            capture.ingest(7, message, received_at=START)
        result = drain.drain_one(7)
        self.assertEqual(result["acked"], 3)
        snap = journal.snapshot("W", 3, 7)
        self.assertEqual((snap["bar_count"], snap["trade_count"],
                          snap["status_count"]), (1, 1, 1))
        self.assertEqual(snap["bar_window_count"], 1)
        self.assertEqual(snap["last_sequence"], 3)
        self.assertTrue(snap["sip_semantics_validated"])
        self.assertFalse(snap["sip_semantics_reconciled"])
        state = next(iter(lua.r.hashes.values()))
        self.assertNotIn("payload", state)
        self.assertNotIn("price", state)
        self.assertNotIn("symbol", state)

    def test_exact_retry_is_idempotent(self):
        lua, journal, capture, _ = self.setup()
        for message in (bar(0), trade(1), status(2)):
            capture.ingest(7, message, received_at=START)
        items, _ = capture.snapshot_prefix(7, 3)
        context = reconciliation_context(items, "W", 3)
        first = journal.reconcile_batch(items, context)
        second = journal.reconcile_batch(items, context)
        self.assertFalse(first["idempotent_retry"])
        self.assertTrue(second["idempotent_retry"])
        self.assertEqual(first["semantic_chain_sha256"],
                         second["semantic_chain_sha256"])
        self.assertEqual(lua.calls, 2)

    def test_bar_multiset_is_independent_of_batch_boundaries(self):
        outputs = []
        for batch_size in (1, 4):
            _, journal, capture, drain = self.setup(batch_size)
            for i in range(4):
                capture.ingest(7, bar(i), received_at=START)
            drain.drain_available(7, max_batches=8)
            outputs.append(journal.snapshot("W", 3, 7))
        self.assertEqual(outputs[0]["bar_multiset_sha256"],
                         outputs[1]["bar_multiset_sha256"])
        self.assertNotEqual(outputs[0]["semantic_chain_sha256"],
                            outputs[1]["semantic_chain_sha256"])

    def test_invalid_scope_trade_status_and_tampering_fail_before_commit(self):
        cases = [bar(0, "OUT"), {**trade(1), "s": 0},
                 {**status(2), "sc": ""}]
        for message in cases:
            with self.subTest(message=message):
                lua, journal, capture, _ = self.setup(batch_size=1)
                capture.ingest(7, message, received_at=START)
                items, _ = capture.snapshot_prefix(7, 1)
                with self.assertRaises(Exception):
                    journal.reconcile_batch(
                        items, reconciliation_context(items, "W", 3))
                self.assertEqual(lua.calls, 0)
        lua, journal, capture, _ = self.setup(batch_size=1)
        capture.ingest(7, trade(1), received_at=START)
        items, _ = capture.snapshot_prefix(7, 1)
        context = reconciliation_context(items, "W", 3)
        bad = dict(context); bad["batch_sha256"] = ZERO
        with self.assertRaisesRegex(SIPSemanticJournalUnsafe, "DIGEST"):
            journal.reconcile_batch(items, bad)
        changed = [copy.deepcopy(items[0])]
        object.__setattr__(changed[0], "sequence", 2)
        with self.assertRaisesRegex(SIPSemanticJournalUnsafe, "SEQUENCE"):
            journal.reconcile_batch(changed, context)


if __name__ == "__main__":
    unittest.main()
