"""Step3AA real Redis tests for atomic transport + semantic receipts."""
import hashlib
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone

from production_sip_semantic_journal import ProductionSIPSemanticJournal, ZERO
from redis_lua_production import AtomicConflict, LeaseLost, ProductionRedisLua
from sip_drain_coordinator import BoundedSIPDrainCoordinator
from sip_epoch_capture import BoundedEpochCapture
from test_step2n_real_redis import local_test_redis


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Token:
    worker_instance_id: str = "W"
    leader_generation: int = 1


class Leadership:
    def require_current(self):
        return Token()


class TestRealRedisSIPSemanticJournal(unittest.TestCase):
    def setUp(self):
        self.r = local_test_redis()
        self.lua = ProductionRedisLua(self.r)
        self.key = f"{self.lua.prefix}:test:sip_semantic"
        self.r.delete(self.key, self.lua.leader_key, self.lua.generation_key)
        ok, generation = self.lua.acquire("W", 30)
        self.assertTrue(ok); self.assertEqual(generation, 1)

    def tearDown(self):
        keys = list(self.r.scan_iter(f"{self.lua.prefix}:recovery:sip_semantic:*"))
        self.r.delete(self.key, self.lua.leader_key, self.lua.generation_key, *keys)

    def commit(self, first=1, last=3, previous=ZERO, semantic_previous=ZERO,
               bar_previous=ZERO, batch=None, transport_next=None,
               semantic_next=None, bar_next=None):
        batch = batch or digest(f"batch:{first}:{last}")
        transport_next = transport_next or digest(f"transport:{previous}:{batch}")
        semantic_batch = digest(f"semantic-batch:{first}:{last}")
        semantic_next = semantic_next or digest(
            f"semantic:{semantic_previous}:{semantic_batch}")
        bar_next = bar_next or digest(f"bar:{bar_previous}:{first}:{last}")
        return self.lua.atomic_sip_semantic_batch(
            "W", 1, self.key, schema="OPR_SIP_SEMANTIC_JOURNAL_V1", epoch=7,
            first_sequence=first, last_sequence=last,
            item_count=last-first+1, batch_sha256=batch,
            previous_chain_sha256=previous, next_chain_sha256=transport_next,
            symbols_sha256=digest("symbols"), bar_count=1,
            trade_count=last-first, status_count=0, bar_window_count=1,
            semantic_batch_sha256=semantic_batch,
            previous_semantic_chain_sha256=semantic_previous,
            next_semantic_chain_sha256=semantic_next,
            previous_bar_multiset_sha256=bar_previous,
            next_bar_multiset_sha256=bar_next, ttl_seconds=3600)

    def test_contiguous_commit_and_exact_retry(self):
        batch = digest("one"); transport = digest("transport-one")
        semantic = digest("semantic-one"); bars = digest("bars-one")
        self.assertEqual(self.commit(batch=batch, transport_next=transport,
                                     semantic_next=semantic, bar_next=bars),
                         {"inserted": True, "idempotent": False})
        before = self.r.hgetall(self.key)
        self.assertEqual(self.commit(previous=transport, semantic_previous=semantic,
                                     bar_previous=bars, batch=batch,
                                     transport_next=transport,
                                     semantic_next=semantic, bar_next=bars),
                         {"inserted": False, "idempotent": True})
        self.assertEqual(self.r.hgetall(self.key), before)

    def test_gap_and_chain_conflicts_are_rejected(self):
        self.commit()
        state = self.r.hgetall(self.key)
        with self.assertRaises(AtomicConflict):
            self.commit(first=5, last=6, previous=state["chain_sha256"],
                        semantic_previous=state["semantic_chain_sha256"],
                        bar_previous=state["bar_multiset_sha256"])
        with self.assertRaises(AtomicConflict):
            self.commit(first=4, last=5, previous=ZERO,
                        semantic_previous=state["semantic_chain_sha256"],
                        bar_previous=state["bar_multiset_sha256"])

    def test_owner_and_generation_fences_prevent_write(self):
        self.r.set(self.lua.leader_key, "SUCCESSOR")
        with self.assertRaises(LeaseLost):
            self.commit()
        self.assertFalse(self.r.exists(self.key))
        self.r.set(self.lua.leader_key, "W")
        self.r.set(self.lua.generation_key, "2")
        with self.assertRaises(LeaseLost):
            self.commit()

    def test_hash_is_payload_free_and_has_exact_counts(self):
        self.commit()
        state = self.r.hgetall(self.key)
        self.assertEqual(state["item_count"], "3")
        self.assertEqual(state["bar_count"], "1")
        self.assertEqual(state["bar_window_count"], "1")
        self.assertEqual(state["trade_count"], "2")
        self.assertEqual(state["status_count"], "0")
        self.assertFalse(any(name in state for name in (
            "payload", "symbol", "price", "size", "status_message")))

    def test_real_journal_drain_commits_before_ack(self):
        journal = ProductionSIPSemanticJournal(
            self.lua, "2026-09-24", ["A"])
        capture = BoundedEpochCapture(16, 1024 * 1024)
        capture.start(7); capture.begin_drain(7)
        now = datetime(2026, 9, 24, 13, 30, tzinfo=timezone.utc)
        capture.ingest(7, {"T": "t", "S": "A", "t": now.isoformat(),
                           "p": 10.0, "s": 1}, received_at=now)
        drain = BoundedSIPDrainCoordinator(
            capture, Leadership(), journal.reconcile_batch, batch_size=1)
        self.assertEqual(drain.drain_one(7)["acked"], 1)
        snap = journal.snapshot("W", 1, 7)
        self.assertEqual(snap["trade_count"], 1)
        self.assertEqual(capture.snapshot()["acked_upto"], 1)


if __name__ == "__main__":
    unittest.main()
