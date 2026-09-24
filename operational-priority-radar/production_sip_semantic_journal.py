"""Fenced, payload-free validation journal for every captured SIP message."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from recovery_chronology import _normalize
from sip_drain_coordinator import PROOF_SCHEMA, captured_batch_digest
from sip_epoch_capture import CapturedSIP
from sip_semantic_digest import (
    ZERO_MULTISET, add_bar_members, canonical_sha256, normalized_bar_member,
)


JOURNAL_SCHEMA = "OPR_SIP_SEMANTIC_JOURNAL_V1"
ZERO = ZERO_MULTISET


class SIPSemanticJournalUnsafe(RuntimeError):
    pass


def _sha(value):
    return canonical_sha256(value)


def _utc(value):
    if not isinstance(value, str):
        raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_TIME_INVALID") from exc
    if parsed.tzinfo is None:
        raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_TIME_INVALID")
    return parsed.astimezone(timezone.utc).isoformat()


def _finite_positive(value):
    return (type(value) in (int, float) and math.isfinite(float(value))
            and float(value) > 0)


def semantic_row(item, scope):
    if (not isinstance(item, CapturedSIP) or item.kind not in {
            "BAR", "TRADE", "STATUS"} or not isinstance(item.payload, dict)):
        raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_ITEM_INVALID")
    payload = item.payload
    expected_type = {"BAR": "b", "TRADE": "t", "STATUS": "s"}[item.kind]
    symbol = payload.get("S")
    if (payload.get("T") != expected_type or not isinstance(symbol, str)
            or not symbol or (item.kind != "STATUS" and symbol not in scope)
            or _utc(payload.get("t")) != _utc(item.event_ts)):
        raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_PROVENANCE_INVALID")
    received = _utc(item.received_at)
    event = _utc(item.event_ts)
    base = {"sequence": item.sequence, "kind": item.kind,
            "symbol": symbol, "event_ts": event, "received_at": received}
    if item.kind == "BAR":
        values = _normalize(payload, "1m")
        return {**base, "ohlcv": values}
    if item.kind == "TRADE":
        if not (_finite_positive(payload.get("p"))
                and _finite_positive(payload.get("s"))):
            raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_TRADE_INVALID")
        return {**base, "price": float(payload["p"]),
                "size": float(payload["s"]), "exchange": payload.get("x"),
                "trade_id": payload.get("i"),
                "conditions": payload.get("c"), "tape": payload.get("z")}
    code = payload.get("sc")
    if not isinstance(code, str) or not code:
        raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_STATUS_INVALID")
    return {**base, "status_code": code, "status_message": payload.get("sm"),
            "reason_code": payload.get("rc"), "reason_message": payload.get("rm")}


class ProductionSIPSemanticJournal:
    """Validate and atomically receipt exact semantic batches without payloads."""

    def __init__(self, lua, session, symbols, *, bar_window_start=None,
                 bar_window_end=None, ttl_seconds=604800):
        if (not isinstance(session, str) or not session
                or not isinstance(symbols, (tuple, list)) or not symbols
                or len(symbols) > 12000 or len(set(symbols)) != len(symbols)
                or any(not isinstance(symbol, str) or not symbol
                       for symbol in symbols)
                or not callable(getattr(lua, "atomic_sip_semantic_batch", None))):
            raise ValueError("semantic journal arguments invalid")
        self.lua = lua
        self.session = session
        self.symbols = tuple(sorted(symbols))
        self.scope = set(self.symbols)
        self.symbols_sha256 = _sha(list(self.symbols))
        self.ttl_seconds = ttl_seconds
        if (bar_window_start is None) != (bar_window_end is None):
            raise ValueError("semantic bar window incomplete")
        if bar_window_start is None:
            self.bar_window_start = self.bar_window_end = None
        else:
            start = datetime.fromisoformat(_utc(bar_window_start))
            end = datetime.fromisoformat(_utc(bar_window_end))
            if not start < end:
                raise ValueError("semantic bar window invalid")
            self.bar_window_start, self.bar_window_end = start, end

    def _key(self, worker, generation, epoch):
        identity = _sha([self.session, worker, generation, epoch,
                         self.symbols_sha256])
        return f"{self.lua.prefix}:recovery:sip_semantic:{identity}"

    def _state(self, key):
        raw = self.lua.r.hgetall(key) or {}
        if not raw:
            return {"last": 0, "transport": ZERO, "semantic": ZERO,
                    "bar_acc": ZERO}
        try:
            state = {"last": int(raw["last_sequence"]),
                     "transport": raw["chain_sha256"],
                     "semantic": raw["semantic_chain_sha256"],
                     "bar_acc": raw["bar_multiset_sha256"]}
        except (KeyError, TypeError, ValueError) as exc:
            raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_STATE_INVALID") from exc
        if (state["last"] < 1 or any(not isinstance(state[key], str)
                or len(state[key]) != 64 for key in (
                    "transport", "semantic", "bar_acc"))):
            raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_STATE_INVALID")
        return state

    @staticmethod
    def _chain(schema, previous, context, extra):
        return _sha({"schema": schema, "previous_sha256": previous,
                     "epoch": context["epoch"],
                     "first_sequence": context["first_sequence"],
                     "last_sequence": context["last_sequence"],
                     "item_count": context["item_count"], **extra})

    def reconcile_batch(self, items, context):
        if (not isinstance(items, (tuple, list)) or not items
                or not isinstance(context, dict)
                or context.get("schema") != PROOF_SCHEMA):
            raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_BATCH_INVALID")
        epoch = context.get("epoch")
        first = context.get("first_sequence")
        last = context.get("last_sequence")
        worker = context.get("worker_instance_id")
        generation = context.get("leader_generation")
        if (type(epoch) is not int or epoch < 1 or type(first) is not int
                or type(last) is not int or not isinstance(worker, str)
                or not worker or type(generation) is not int or generation < 1
                or len(items) != last - first + 1):
            raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_CONTEXT_INVALID")
        for sequence, item in enumerate(items, first):
            if item.epoch != epoch or item.sequence != sequence or item.bytes < 1:
                raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_SEQUENCE_INVALID")
        if (context.get("item_count") != len(items)
                or context.get("batch_sha256") != captured_batch_digest(items)):
            raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_DIGEST_MISMATCH")
        rows = [semantic_row(item, self.scope) for item in items]
        counts = {kind: sum(item.kind == kind for item in items)
                  for kind in ("BAR", "TRADE", "STATUS")}
        semantic_batch = _sha(rows)
        # Exclude receive order/time so the commutative accumulator can later
        # be reproduced from the same native REST bar multiset.
        bar_members = []
        for row, item in zip(rows, items):
            if row["kind"] != "BAR":
                continue
            event = datetime.fromisoformat(row["event_ts"])
            if (self.bar_window_start is not None
                    and not self.bar_window_start <= event < self.bar_window_end):
                continue
            bar_members.append(normalized_bar_member(
                row["symbol"], row["event_ts"], item.payload))
        key = self._key(worker, generation, epoch)
        state = self._state(key)
        if first != state["last"] + 1 and last != state["last"]:
            raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_NONCONTIGUOUS")
        retry = last == state["last"]
        transport_next = (state["transport"] if retry else self._chain(
            "OPR_SIP_TRANSPORT_CHAIN_V1", state["transport"], context,
            {"batch_sha256": context["batch_sha256"]}))
        semantic_next = (state["semantic"] if retry else self._chain(
            JOURNAL_SCHEMA, state["semantic"], context,
            {"semantic_batch_sha256": semantic_batch, "counts": counts}))
        bar_next = (state["bar_acc"] if retry
                    else add_bar_members(state["bar_acc"], bar_members))
        result = self.lua.atomic_sip_semantic_batch(
            worker, generation, key, schema=JOURNAL_SCHEMA, epoch=epoch,
            first_sequence=first, last_sequence=last, item_count=len(items),
            batch_sha256=context["batch_sha256"],
            previous_chain_sha256=state["transport"],
            next_chain_sha256=transport_next,
            symbols_sha256=self.symbols_sha256,
            bar_count=counts["BAR"], trade_count=counts["TRADE"],
            status_count=counts["STATUS"],
            bar_window_count=len(bar_members),
            semantic_batch_sha256=semantic_batch,
            previous_semantic_chain_sha256=state["semantic"],
            next_semantic_chain_sha256=semantic_next,
            previous_bar_multiset_sha256=state["bar_acc"],
            next_bar_multiset_sha256=bar_next,
            ttl_seconds=self.ttl_seconds)
        return {**context, "committed": True,
                "journal_schema": JOURNAL_SCHEMA,
                "journal_chain_sha256": transport_next,
                "semantic_chain_sha256": semantic_next,
                "bar_multiset_sha256": bar_next,
                "batch_bar_count": counts["BAR"],
                "batch_trade_count": counts["TRADE"],
                "batch_status_count": counts["STATUS"],
                "batch_bar_window_count": len(bar_members),
                "idempotent_retry": result["idempotent"],
                "payload_retained": False,
                "sip_semantics_validated": True,
                "sip_semantics_reconciled": False,
                "direct_handoff_authorized": False,
                "full_session_coverage_proven": False,
                "retroactive_entries_allowed": False}

    def snapshot(self, worker, generation, epoch):
        key = self._key(worker, generation, epoch)
        raw = self.lua.r.hgetall(key) or {}
        if not raw:
            return None
        state = self._state(key)
        try:
            item_count = int(raw["item_count"])
            counts = {kind: int(raw[f"{kind.lower()}_count"])
                      for kind in ("BAR", "TRADE", "STATUS")}
            window_count = int(raw["bar_window_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_STATE_INVALID") from exc
        if sum(counts.values()) != item_count:
            raise SIPSemanticJournalUnsafe("SIP_SEMANTIC_COUNT_INVALID")
        body = {"schema": JOURNAL_SCHEMA, "session": self.session,
                "epoch": epoch,
                "last_sequence": state["last"], "item_count": item_count,
                "bar_count": counts["BAR"], "trade_count": counts["TRADE"],
                "status_count": counts["STATUS"],
                "bar_window_count": window_count,
                "bar_window_start_utc": (self.bar_window_start.isoformat()
                                         if self.bar_window_start else None),
                "bar_window_end_utc": (self.bar_window_end.isoformat()
                                       if self.bar_window_end else None),
                "symbols_sha256": self.symbols_sha256,
                "transport_chain_sha256": state["transport"],
                "semantic_chain_sha256": state["semantic"],
                "bar_multiset_sha256": state["bar_acc"],
                "payloads_retained": 0,
                "sip_semantics_validated": True,
                "sip_semantics_reconciled": False,
                "full_session_coverage_proven": False,
                "direct_handoff_authorized": False,
                "retroactive_entries_allowed": False}
        body["snapshot_sha256"] = _sha(body)
        return body
