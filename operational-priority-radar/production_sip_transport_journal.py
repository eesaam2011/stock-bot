"""Durable, generation-fenced receipts for bounded SIP capture prefixes.

This is intentionally a transport journal, not a market-state reconciler. It
lets the in-memory capture ACK a committed exact prefix without retaining SIP
payloads in Redis.  Its receipt chain cannot prove an uninterrupted session,
reconstruct E/B, authorize DIRECT, or permit retroactive entries.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sip_drain_coordinator import PROOF_SCHEMA, captured_batch_digest
from sip_epoch_capture import CapturedSIP


class SIPTransportJournalUnsafe(RuntimeError):
    pass


JOURNAL_SCHEMA = "OPR_SIP_TRANSPORT_JOURNAL_V1"
ZERO_CHAIN = "0" * 64


def _utc_timestamp(value):
    if not isinstance(value, str):
        raise SIPTransportJournalUnsafe("SIP_JOURNAL_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SIPTransportJournalUnsafe("SIP_JOURNAL_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise SIPTransportJournalUnsafe("SIP_JOURNAL_TIMESTAMP_INVALID")


class ProductionSIPTransportJournal:
    """Commit exact batch receipts to one bounded Redis hash per ACK epoch."""

    def __init__(self, lua, session, *, ttl_seconds=604800):
        if not isinstance(session, str) or not session:
            raise ValueError("session required")
        if not callable(getattr(lua, "atomic_sip_transport_batch", None)):
            raise ValueError("lua.atomic_sip_transport_batch required")
        self.lua = lua
        self.session = session
        self.ttl_seconds = ttl_seconds

    def _key(self, worker, generation, epoch):
        scope = hashlib.sha256(
            f"{self.session}\n{worker}\n{generation}\n{epoch}".encode("utf-8")
        ).hexdigest()
        return f"{self.lua.prefix}:recovery:sip_transport:{scope}"

    def _state(self, key):
        raw = self.lua.r.hgetall(key) or {}
        if not raw:
            return 0, ZERO_CHAIN
        try:
            last = int(raw["last_sequence"])
            chain = raw["chain_sha256"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SIPTransportJournalUnsafe("SIP_JOURNAL_STATE_INVALID") from exc
        if last < 1 or not isinstance(chain, str) or len(chain) != 64:
            raise SIPTransportJournalUnsafe("SIP_JOURNAL_STATE_INVALID")
        return last, chain

    @staticmethod
    def _next_chain(previous, context):
        raw = json.dumps({
            "schema": JOURNAL_SCHEMA,
            "previous_chain_sha256": previous,
            "epoch": context["epoch"],
            "first_sequence": context["first_sequence"],
            "last_sequence": context["last_sequence"],
            "item_count": context["item_count"],
            "batch_sha256": context["batch_sha256"],
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def reconcile_batch(self, items, context):
        if (not isinstance(items, (tuple, list)) or not items
                or not isinstance(context, dict)
                or context.get("schema") != PROOF_SCHEMA):
            raise SIPTransportJournalUnsafe("SIP_JOURNAL_BATCH_INVALID")
        epoch = context.get("epoch")
        first = context.get("first_sequence")
        last = context.get("last_sequence")
        worker = context.get("worker_instance_id")
        generation = context.get("leader_generation")
        if (type(epoch) is not int or epoch < 1
                or type(first) is not int or type(last) is not int
                or not isinstance(worker, str) or not worker
                or type(generation) is not int or generation < 1):
            raise SIPTransportJournalUnsafe("SIP_JOURNAL_CONTEXT_INVALID")
        expected_sequences = list(range(first, last + 1))
        if len(items) != len(expected_sequences):
            raise SIPTransportJournalUnsafe("SIP_JOURNAL_SEQUENCE_GAP")
        for item, sequence in zip(items, expected_sequences):
            if (not isinstance(item, CapturedSIP) or item.epoch != epoch
                    or item.sequence != sequence
                    or item.kind not in {"BAR", "TRADE", "STATUS"}
                    or not isinstance(item.payload, dict)
                    or item.bytes < 1):
                raise SIPTransportJournalUnsafe("SIP_JOURNAL_ITEM_INVALID")
            _utc_timestamp(item.event_ts)
            _utc_timestamp(item.received_at)
        if (context.get("item_count") != len(items)
                or context.get("batch_sha256") != captured_batch_digest(items)):
            raise SIPTransportJournalUnsafe("SIP_JOURNAL_DIGEST_MISMATCH")

        key = self._key(worker, generation, epoch)
        prior_last, previous_chain = self._state(key)
        # A retry of the last committed batch is resolved atomically by Lua.
        # Any other stale/gapped attempt is rejected there against the same
        # authoritative state used for the write.
        if first != prior_last + 1 and last != prior_last:
            raise SIPTransportJournalUnsafe("SIP_JOURNAL_NONCONTIGUOUS")
        retry_candidate = last == prior_last
        next_chain = (previous_chain if retry_candidate
                      else self._next_chain(previous_chain, context))
        result = self.lua.atomic_sip_transport_batch(
            worker, generation, key, schema=JOURNAL_SCHEMA, epoch=epoch,
            first_sequence=first, last_sequence=last, item_count=len(items),
            batch_sha256=context["batch_sha256"],
            previous_chain_sha256=previous_chain,
            next_chain_sha256=next_chain, ttl_seconds=self.ttl_seconds)
        return {
            **context,
            "committed": True,
            "journal_schema": JOURNAL_SCHEMA,
            "journal_chain_sha256": next_chain,
            "idempotent_retry": result["idempotent"],
            "payload_retained": False,
            "direct_handoff_authorized": False,
            "full_session_coverage_proven": False,
            "retroactive_entries_allowed": False,
        }
