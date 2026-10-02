"""Bounded, generation-fenced draining for one live SIP ACK epoch.

This module solves one narrow transport problem: a DRAINING capture can be
reconciled and ACKed in small contiguous prefixes while the websocket keeps
appending later messages.  It deliberately does *not* prove session coverage,
authorize DIRECT, persist E/B by itself, or allow retroactive entries.

The injected reconciler owns the durable/idempotent application of a batch.
It must return a proof bound to the exact epoch, contiguous sequence range,
payload digest and current leadership generation.  Only then is that prefix
removed from the bounded in-memory capture.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping

from sip_epoch_capture import CapturedSIP, EpochCaptureError


class SIPDrainUnsafe(RuntimeError):
    pass


PROOF_SCHEMA = "OPR_SIP_RECONCILED_BATCH_V1"


def _leader_identity(token):
    worker = getattr(token, "worker_instance_id", None)
    generation = getattr(token, "leader_generation", None)
    if (not isinstance(worker, str) or not worker
            or isinstance(generation, bool)
            or not isinstance(generation, int) or generation < 1):
        raise SIPDrainUnsafe("DRAIN_LEADERSHIP_TOKEN_INVALID")
    return worker, generation


def captured_batch_digest(items):
    """Stable proof digest; the digest is retained, never the payload."""
    if not isinstance(items, (tuple, list)) or not items:
        raise SIPDrainUnsafe("DRAIN_BATCH_EMPTY")
    rows = []
    for item in items:
        if not isinstance(item, CapturedSIP):
            raise SIPDrainUnsafe("DRAIN_BATCH_ITEM_INVALID")
        rows.append({
            "epoch": item.epoch,
            "sequence": item.sequence,
            "kind": item.kind,
            "event_ts": item.event_ts,
            "received_at": item.received_at,
            "bytes": item.bytes,
            "payload": item.payload,
        })
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reconciliation_context(items, worker_instance_id, leader_generation):
    first, last = items[0], items[-1]
    return {
        "schema": PROOF_SCHEMA,
        "epoch": first.epoch,
        "first_sequence": first.sequence,
        "last_sequence": last.sequence,
        "item_count": len(items),
        "batch_sha256": captured_batch_digest(items),
        "worker_instance_id": worker_instance_id,
        "leader_generation": leader_generation,
    }


class BoundedSIPDrainCoordinator:
    """Serially reconcile and ACK bounded, contiguous capture prefixes."""

    def __init__(self, capture, leadership, reconcile_batch, *, batch_size=None):
        if batch_size is None:
            batch_size = min(256, capture.max_messages)
        if (not isinstance(batch_size, int) or isinstance(batch_size, bool)
                or not 1 <= batch_size <= min(1024, capture.max_messages)):
            raise ValueError("invalid drain batch size")
        if not callable(getattr(leadership, "require_current", None)):
            raise ValueError("leadership.require_current required")
        if not callable(reconcile_batch):
            raise ValueError("reconcile_batch callback required")
        self.capture = capture
        self.leadership = leadership
        self.reconcile_batch = reconcile_batch
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._batches = 0
        self._acked = 0
        self._last_digest = None

    @staticmethod
    def _validate_proof(proof, expected):
        if not isinstance(proof, Mapping):
            raise SIPDrainUnsafe("DRAIN_RECONCILIATION_PROOF_MISSING")
        if proof.get("committed") is not True:
            raise SIPDrainUnsafe("DRAIN_BATCH_NOT_COMMITTED")
        for key, value in expected.items():
            if proof.get(key) != value:
                raise SIPDrainUnsafe(f"DRAIN_PROOF_MISMATCH:{key}")
        # This is a batch commit proof only.  It must not smuggle in a claim
        # that session coverage or a DIRECT handoff has been established.
        if (proof.get("direct_handoff_authorized") is True
                or proof.get("full_session_coverage_proven") is True
                or proof.get("retroactive_entries_allowed") is True):
            raise SIPDrainUnsafe("DRAIN_PROOF_SCOPE_ESCALATION")

    def drain_one(self, epoch):
        """Reconcile one immutable prefix and ACK it only after exact proof."""
        with self._lock:
            try:
                items, prefix = self.capture.snapshot_prefix(
                    epoch, min(self.batch_size, self.capture.max_messages))
            except EpochCaptureError as exc:
                raise SIPDrainUnsafe("DRAIN_CAPTURE_NOT_AVAILABLE") from exc
            if not items:
                return {"status": "EMPTY", "epoch": epoch, "acked": 0}
            if (prefix["first_sequence"] != prefix["acked_upto_at_snapshot"] + 1
                    or prefix["last_sequence"]
                    != prefix["first_sequence"] + len(items) - 1):
                raise SIPDrainUnsafe("DRAIN_CAPTURE_SEQUENCE_GAP")

            before = self.leadership.require_current()
            worker, generation = _leader_identity(before)
            expected = reconciliation_context(items, worker, generation)
            proof = self.reconcile_batch(items, dict(expected))
            self._validate_proof(proof, expected)

            after = self.leadership.require_current()
            if _leader_identity(after) != (worker, generation):
                raise SIPDrainUnsafe("DRAIN_LEADERSHIP_CHANGED_BEFORE_ACK")
            if not self.capture.prefix_still_valid(
                    epoch, prefix["first_sequence"], prefix["last_sequence"]):
                raise SIPDrainUnsafe("DRAIN_PREFIX_CHANGED_BEFORE_ACK")
            try:
                acked = self.capture.ack_batch(epoch, prefix["last_sequence"])
            except EpochCaptureError as exc:
                raise SIPDrainUnsafe("DRAIN_ACK_REJECTED") from exc
            if acked != len(items):
                raise SIPDrainUnsafe("DRAIN_ACK_COUNT_MISMATCH")
            self._batches += 1
            self._acked += acked
            self._last_digest = expected["batch_sha256"]
            return {
                "status": "ACKED",
                "epoch": epoch,
                "first_sequence": prefix["first_sequence"],
                "last_sequence": prefix["last_sequence"],
                "acked": acked,
                "batch_sha256": expected["batch_sha256"],
            }

    def drain_available(self, epoch, *, max_batches=16):
        """Bound work per call; a caller may invoke this continuously."""
        if (not isinstance(max_batches, int) or isinstance(max_batches, bool)
                or not 1 <= max_batches <= 256):
            raise ValueError("invalid drain batch budget")
        batches = acked = 0
        for _ in range(max_batches):
            result = self.drain_one(epoch)
            if result["status"] == "EMPTY":
                break
            batches += 1
            acked += result["acked"]
        snap = self.capture.snapshot()
        return {
            "epoch": epoch,
            "batches": batches,
            "acked": acked,
            "buffered": snap["buffered"],
            "phase": snap["phase"],
            "direct_handoff_authorized": False,
            "full_session_coverage_proven": False,
            "retroactive_entries_allowed": False,
        }

    def snapshot(self):
        return {
            "schema": "OPR_SIP_BOUNDED_DRAIN_V1",
            "batch_size": self.batch_size,
            "reconciled_batches": self._batches,
            "acked_messages": self._acked,
            "last_batch_sha256": self._last_digest,
            "direct_handoff_authorized": False,
            "full_session_coverage_proven": False,
        }
