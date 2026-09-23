"""Step3U: production SIP transport receipts remain bounded and fail closed."""
import copy
import hashlib
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from production_sip_transport_journal import (
    JOURNAL_SCHEMA, ProductionSIPTransportJournal, SIPTransportJournalUnsafe,
    ZERO_CHAIN,
)
from sip_drain_coordinator import BoundedSIPDrainCoordinator
from sip_epoch_capture import BoundedEpochCapture


START = datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc)


def trade(i):
    return {"T": "t", "S": "A", "p": 10 + i / 1000,
            "t": (START + timedelta(microseconds=i)).isoformat()}


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

    def atomic_sip_transport_batch(self, worker, generation, key, **kw):
        self.calls += 1
        state = self.r.hashes.get(key)
        if state and (int(state["last_sequence"]) == kw["last_sequence"]
                      and int(state["last_batch_first"]) == kw["first_sequence"]
                      and state["last_batch_sha256"] == kw["batch_sha256"]):
            return {"inserted": False, "idempotent": True}
        last = int(state["last_sequence"]) if state else 0
        chain = state["chain_sha256"] if state else ZERO_CHAIN
        if kw["first_sequence"] != last + 1 or kw["previous_chain_sha256"] != chain:
            raise RuntimeError("CHAIN_CONFLICT")
        self.r.hashes[key] = {
            "schema": JOURNAL_SCHEMA,
            "last_sequence": str(kw["last_sequence"]),
            "item_count": str((int(state["item_count"]) if state else 0)
                              + kw["item_count"]),
            "chain_sha256": kw["next_chain_sha256"],
            "last_batch_first": str(kw["first_sequence"]),
            "last_batch_sha256": kw["batch_sha256"],
        }
        return {"inserted": True, "idempotent": False}


class TestSIPTransportJournal(unittest.TestCase):
    def setup(self, limit=256, batch=32):
        lua = FakeLua()
        journal = ProductionSIPTransportJournal(lua, "2026-09-23")
        capture = BoundedEpochCapture(limit, 1024 * 1024)
        capture.start(7)
        capture.begin_drain(7)
        drain = BoundedSIPDrainCoordinator(
            capture, Leadership(), journal.reconcile_batch, batch_size=batch)
        return lua, journal, capture, drain

    def test_ten_thousand_messages_use_one_payload_free_bounded_hash(self):
        lua, _, capture, drain = self.setup(limit=128, batch=32)
        for i in range(1, 10001):
            capture.ingest(7, trade(i), received_at=START)
            if capture.snapshot()["buffered"] >= 32:
                drain.drain_available(7, max_batches=2)
        drain.drain_available(7, max_batches=256)
        self.assertEqual(capture.snapshot()["acked_upto"], 10000)
        self.assertEqual(len(lua.r.hashes), 1)
        state = next(iter(lua.r.hashes.values()))
        self.assertEqual(int(state["item_count"]), 10000)
        self.assertEqual(int(state["last_sequence"]), 10000)
        self.assertNotIn("payload", state)
        self.assertNotIn("symbol", state)
        self.assertFalse(drain.snapshot()["full_session_coverage_proven"])

    def test_exact_committed_batch_retry_is_idempotent(self):
        lua, journal, capture, _ = self.setup(batch=4)
        for i in range(1, 5):
            capture.ingest(7, trade(i), received_at=START)
        items, _ = capture.snapshot_prefix(7, 4)
        from sip_drain_coordinator import reconciliation_context
        context = reconciliation_context(items, "W", 3)
        first = journal.reconcile_batch(items, context)
        second = journal.reconcile_batch(items, context)
        self.assertFalse(first["idempotent_retry"])
        self.assertTrue(second["idempotent_retry"])
        self.assertEqual(first["journal_chain_sha256"],
                         second["journal_chain_sha256"])
        self.assertEqual(len(lua.r.hashes), 1)

    def test_tampered_digest_and_sequence_are_rejected_before_commit(self):
        lua, journal, capture, _ = self.setup()
        capture.ingest(7, trade(1), received_at=START)
        items, _ = capture.snapshot_prefix(7, 1)
        from sip_drain_coordinator import reconciliation_context
        context = reconciliation_context(items, "W", 3)
        bad = dict(context); bad["batch_sha256"] = hashlib.sha256(b"bad").hexdigest()
        with self.assertRaisesRegex(SIPTransportJournalUnsafe, "DIGEST"):
            journal.reconcile_batch(items, bad)
        broken = [copy.deepcopy(items[0])]
        object.__setattr__(broken[0], "sequence", 2)
        with self.assertRaisesRegex(SIPTransportJournalUnsafe, "ITEM"):
            journal.reconcile_batch(broken, context)
        self.assertEqual(lua.calls, 0)

    def test_receipt_never_escalates_scope(self):
        _, journal, capture, _ = self.setup()
        capture.ingest(7, trade(1), received_at=START)
        items, _ = capture.snapshot_prefix(7, 1)
        from sip_drain_coordinator import reconciliation_context
        proof = journal.reconcile_batch(
            items, reconciliation_context(items, "W", 3))
        self.assertTrue(proof["committed"])
        self.assertFalse(proof["payload_retained"])
        self.assertFalse(proof["direct_handoff_authorized"])
        self.assertFalse(proof["full_session_coverage_proven"])
        self.assertFalse(proof["retroactive_entries_allowed"])


if __name__ == "__main__":
    unittest.main()
