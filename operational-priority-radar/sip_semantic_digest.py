"""Shared canonical digests for SIP/REST semantic equality checks."""
from __future__ import annotations

import hashlib
import json

from recovery_chronology import _normalize, _utc


ZERO_MULTISET = "0" * 64
MODULUS = 1 << 256


def canonical_sha256(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalized_bar_member(symbol, event_ts, payload):
    if not isinstance(symbol, str) or not symbol or not isinstance(payload, dict):
        raise ValueError("BAR_MULTISET_ARGUMENT_INVALID")
    return {"symbol": symbol, "event_ts": _utc(event_ts).isoformat(),
            "ohlcv": _normalize(payload, "1m")}


def add_bar_members(previous, members):
    if (not isinstance(previous, str) or len(previous) != 64
            or any(char not in "0123456789abcdef" for char in previous)
            or not isinstance(members, (tuple, list))):
        raise ValueError("BAR_MULTISET_STATE_INVALID")
    hashes = [canonical_sha256(member) for member in members]
    return format((int(previous, 16) + sum(int(value, 16) for value in hashes))
                  % MODULUS, "064x")


def combine_bar_multisets(values):
    if (not isinstance(values, (tuple, list)) or any(
            not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in values)):
        raise ValueError("BAR_MULTISET_DIGEST_INVALID")
    return format(sum(int(value, 16) for value in values) % MODULUS, "064x")
