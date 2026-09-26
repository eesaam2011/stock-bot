"""Bounded same-epoch original-trade evidence, independent of capture ACKs.

A retained pair is diagnostic evidence only. Eviction is explicit; this is not
an authoritative trade tape or permission to reconcile bars or advance trust.
"""
from collections import OrderedDict
from copy import deepcopy
import json
from sip_trade_revision_audit import audit_trade_revisions, RevisionAuditError, _number, _timestamp


class OriginalTradeWindow:
    def __init__(self, max_items=4096, max_bytes=2*1024*1024):
        if type(max_items) is not int or not 1 <= max_items <= 100000:
            raise ValueError('ORIGINAL_WINDOW_ITEMS_INVALID')
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 8*1024*1024:
            raise ValueError('ORIGINAL_WINDOW_BYTES_INVALID')
        self.max_items, self.max_bytes = max_items, max_bytes
        self.clear()

    def clear(self):
        self.items = OrderedDict()
        self.bytes = self.evicted = self.rejected = 0

    def observe(self, message):
        if message.get('T') != 't':
            return
        fields = ('T', 'S', 'i', 'p', 's', 'x', 'z', 'c', 't')
        row = {key: message.get(key) for key in fields}
        # Bound allowlisted field sizes before retaining or hashing payloads.
        if (any(not isinstance(row[k], str) or not 0 < len(row[k]) <= 128
                for k in ('T', 'S', 'x', 'z', 't'))
                or not isinstance(row['c'], list) or len(row['c']) > 32
                or any(not isinstance(v, str) or len(v) > 16 for v in row['c'])
                or any(type(row[k]) is not int or not 0 < row[k] < 2**64
                       for k in ('i', 's'))):
            self.rejected += 1
            return
        try:
            if not _number(row['p']) or not _timestamp(row['t']):
                raise ValueError('TRADE_INVALID')
            size = len(json.dumps(row, allow_nan=False).encode())
        except (RevisionAuditError, ValueError, OverflowError):
            self.rejected += 1
            return
        if size > self.max_bytes:
            self.rejected += 1
            return
        key = (row['S'], row['i'])
        if key in self.items:
            # Never choose one original when the supplied slice repeats an ID.
            prior, old_size, _ = self.items[key]
            self.items[key] = (prior, old_size, True)
            return
        while self.items and (len(self.items) >= self.max_items or self.bytes+size > self.max_bytes):
            _, (_, old_size, _) = self.items.popitem(last=False)
            self.bytes -= old_size
            self.evicted += 1
        self.items[key] = (deepcopy(row), size, False)
        self.bytes += size

    def inspect(self, revision):
        result = {'schema': 'OPR_ORIGINAL_TRADE_WINDOW_V1',
                  'retained': len(self.items), 'bytes': self.bytes,
                  'evicted': self.evicted, 'rejected': self.rejected,
                  'original_trade_lookup_performed': True,
                  'matched_within_retained_window': False,
                  'upstream_completeness_proven': False,
                  'native_bars_reconciled': False,
                  'capture_ack_authorized': False,
                  'eb_persistence_authorized': False,
                  'direct_handoff_authorized': False}
        symbol = revision.get('S')
        trade_id = revision.get('i' if revision.get('T') == 'x' else 'oi')
        if not isinstance(symbol, str) or type(trade_id) is not int:
            result['reason'] = 'REVISION_ID_INVALID'
            return result
        found = self.items.get((symbol, trade_id))
        if found is None:
            result['reason'] = 'ORIGINAL_NOT_RETAINED'
            return result
        original, _, ambiguous = found
        result['original_frame'] = deepcopy(original)
        if ambiguous:
            result['reason'] = 'ORIGINAL_ID_REPEATED'
            return result
        try:
            audit = audit_trade_revisions([original, revision], symbol=symbol,
                                          max_frames=2, max_ids=2)
        except RevisionAuditError as exc:
            result['reason'] = str(exc)
            return result
        result.update(reason='PAIR_MATCHED_NOT_BAR_RECONCILED',
                      matched_within_retained_window=True,
                      pair_sha256=audit['observed_frame_sha256'])
        return result
