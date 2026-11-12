"""Step 3Q: bounded, fenced SIP drain; still no DIRECT or trust claim."""
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sip_epoch_capture import BoundedEpochCapture
from sip_drain_coordinator import (
    BoundedSIPDrainCoordinator, SIPDrainUnsafe,
)


START = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)


def trade(i):
    return {"T": "t", "S": "A", "p": 10 + i / 1000,
            "t": (START + timedelta(microseconds=i)).isoformat()}


@dataclass(frozen=True)
class Token:
    worker_instance_id: str = "worker-a"
    leader_generation: int = 7


class Leadership:
    def __init__(self):
        self.token = Token()

    def require_current(self):
        return self.token


class Recorder:
    def __init__(self):
        self.sequences = []

    def __call__(self, items, context):
        self.sequences.extend(x.sequence for x in items)
        return {**context, "committed": True,
                "direct_handoff_authorized": False,
                "full_session_coverage_proven": False,
                "retroactive_entries_allowed": False}


class TestBoundedSIPDrain(unittest.TestCase):
    def setup_capture(self, limit=64):
        capture = BoundedEpochCapture(max_messages=limit, max_bytes=1024 * 1024)
        capture.start(7)
        capture.begin_drain(7)
        return capture

    def test_ten_thousand_messages_do_not_fill_bounded_capture(self):
        capture = self.setup_capture()
        leadership = Leadership()
        recorder = Recorder()
        drain = BoundedSIPDrainCoordinator(
            capture, leadership, recorder, batch_size=16)
        high_water = 0
        for i in range(1, 10001):
            capture.ingest(7, trade(i), received_at=START + timedelta(seconds=1))
            high_water = max(high_water, capture.snapshot()["buffered"])
            if i % 8 == 0:
                drain.drain_available(7, max_batches=2)
        drain.drain_available(7, max_batches=256)
        self.assertEqual(recorder.sequences, list(range(1, 10001)))
        self.assertLess(high_water, capture.max_messages)
        self.assertEqual(capture.snapshot()["buffered"], 0)
        self.assertEqual(capture.snapshot()["acked_upto"], 10000)
        self.assertEqual(capture.phase, capture.DRAINING)
        self.assertFalse(drain.snapshot()["direct_handoff_authorized"])

    def test_reconciler_failure_never_acks(self):
        capture = self.setup_capture()
        capture.ingest(7, trade(1), received_at=START)

        def fail(_items, _context):
            raise RuntimeError("COMMIT_FAILED")

        drain = BoundedSIPDrainCoordinator(capture, Leadership(), fail)
        with self.assertRaisesRegex(RuntimeError, "COMMIT_FAILED"):
            drain.drain_one(7)
        self.assertEqual(capture.snapshot()["acked_upto"], 0)
        self.assertEqual(capture.snapshot()["buffered"], 1)

    def test_proof_is_bound_to_exact_digest_and_range(self):
        for changed_key in ("batch_sha256", "last_sequence", "epoch"):
            with self.subTest(changed_key=changed_key):
                capture = self.setup_capture()
                capture.ingest(7, trade(1), received_at=START)

                def bad(_items, context, key=changed_key):
                    proof = {**context, "committed": True}
                    proof[key] = "wrong"
                    return proof

                drain = BoundedSIPDrainCoordinator(capture, Leadership(), bad)
                with self.assertRaisesRegex(SIPDrainUnsafe, "PROOF_MISMATCH"):
                    drain.drain_one(7)
                self.assertEqual(capture.snapshot()["acked_upto"], 0)

    def test_leadership_change_after_commit_blocks_ack(self):
        capture = self.setup_capture()
        capture.ingest(7, trade(1), received_at=START)
        leadership = Leadership()

        def takeover(_items, context):
            leadership.token = Token("worker-b", 8)
            return {**context, "committed": True}

        drain = BoundedSIPDrainCoordinator(capture, leadership, takeover)
        with self.assertRaisesRegex(SIPDrainUnsafe, "LEADERSHIP_CHANGED"):
            drain.drain_one(7)
        self.assertEqual(capture.snapshot()["acked_upto"], 0)
        self.assertEqual(capture.snapshot()["buffered"], 1)

    def test_append_during_commit_remains_for_next_batch(self):
        capture = self.setup_capture()
        capture.ingest(7, trade(1), received_at=START)
        calls = []

        def append(items, context):
            calls.append([x.sequence for x in items])
            if len(calls) == 1:
                capture.ingest(7, trade(2), received_at=START)
            return {**context, "committed": True}

        drain = BoundedSIPDrainCoordinator(
            capture, Leadership(), append, batch_size=1)
        first = drain.drain_one(7)
        self.assertEqual(first["last_sequence"], 1)
        self.assertEqual(capture.snapshot()["buffered"], 1)
        drain.drain_one(7)
        self.assertEqual(calls, [[1], [2]])
        self.assertEqual(capture.snapshot()["acked_upto"], 2)

    def test_disconnect_during_commit_blocks_ack_and_direct(self):
        capture = self.setup_capture()
        capture.ingest(7, trade(1), received_at=START)

        def disconnect(_items, context):
            capture.invalidate("SIP_DISCONNECTED")
            return {**context, "committed": True}

        drain = BoundedSIPDrainCoordinator(capture, Leadership(), disconnect)
        with self.assertRaisesRegex(SIPDrainUnsafe, "PREFIX_CHANGED"):
            drain.drain_one(7)
        self.assertEqual(capture.phase, capture.INVALID)

    def test_scope_escalation_in_batch_proof_is_rejected(self):
        capture = self.setup_capture()
        capture.ingest(7, trade(1), received_at=START)

        def escalate(_items, context):
            return {**context, "committed": True,
                    "direct_handoff_authorized": True}

        drain = BoundedSIPDrainCoordinator(capture, Leadership(), escalate)
        with self.assertRaisesRegex(SIPDrainUnsafe, "SCOPE_ESCALATION"):
            drain.drain_one(7)
        self.assertEqual(capture.snapshot()["acked_upto"], 0)

    def test_work_budget_is_bounded(self):
        capture = self.setup_capture()
        for i in range(1, 21):
            capture.ingest(7, trade(i), received_at=START)
        drain = BoundedSIPDrainCoordinator(
            capture, Leadership(), Recorder(), batch_size=3)
        result = drain.drain_available(7, max_batches=2)
        self.assertEqual((result["batches"], result["acked"]), (2, 6))
        self.assertEqual(capture.snapshot()["buffered"], 14)


if __name__ == "__main__":
    unittest.main()
