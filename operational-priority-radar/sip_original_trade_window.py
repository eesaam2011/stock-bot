"""Time-bounded original-trade evidence, independent of capture ACKs.

The index is diagnostic evidence only. It survives capture drain and reconnect
epochs in the same process, but every match remains fail-closed: it does not
prove a complete tape, corrected bars, continuity, or permission to persist
E/B. Age and resource limits are explicit; recent records are never silently
evicted to make room.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import json

from sip_trade_revision_audit import (
    audit_trade_revisions, RevisionAuditError, _number, _timestamp,
)


class OriginalTradeIndexOverflow(RuntimeError):
    pass


class OriginalTradeWindow:
    """Compatibility name for the v2 time-bounded original-trade index."""

    def __init__(self, max_items=750_000, max_bytes=192*1024*1024,
                 max_age_seconds=15*60):
        if type(max_items) is not int or not 1 <= max_items <= 2_000_000:
            raise ValueError('ORIGINAL_INDEX_ITEMS_INVALID')
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 512*1024*1024:
            raise ValueError('ORIGINAL_INDEX_BYTES_INVALID')
        if (type(max_age_seconds) is not int
                or not 60 <= max_age_seconds <= 60*60):
            raise ValueError('ORIGINAL_INDEX_AGE_INVALID')
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.max_age_seconds = max_age_seconds
        self.clear()

    def clear(self):
        self.items = OrderedDict()
        self.bytes = self.expired = self.rejected = 0
        self.overflowed = False
        self.last_received_us = None

    @staticmethod
    def _received_us(received_at):
        if received_at is None:
            received_at = datetime.now(timezone.utc)
        if not isinstance(received_at, datetime) or received_at.tzinfo is None:
            raise ValueError('ORIGINAL_INDEX_RECEIVED_AT_INVALID')
        return int(received_at.astimezone(timezone.utc).timestamp()*1_000_000)

    def _prune(self, now_us):
        cutoff = now_us - self.max_age_seconds*1_000_000
        while self.items:
            key, (_, size, _, observed_us, _) = next(iter(self.items.items()))
            if observed_us >= cutoff:
                break
            self.items.pop(key)
            self.bytes -= size
            self.expired += 1

    @staticmethod
    def _pack(row):
        return (row['T'], row['S'], row['i'], row['p'], row['s'], row['x'],
                row['z'], tuple(row['c']), row['t'])

    @staticmethod
    def _unpack(value):
        return dict(zip(('T', 'S', 'i', 'p', 's', 'x', 'z', 'c', 't'),
                        value[:7] + (list(value[7]), value[8])))

    def observe(self, message, *, epoch=None, received_at=None):
        if message.get('T') != 't':
            return
        if epoch is not None and (type(epoch) is not int or epoch < 1):
            self.rejected += 1
            return
        try:
            observed_us = self._received_us(received_at)
        except ValueError:
            self.rejected += 1
            return
        if self.last_received_us is not None and observed_us < self.last_received_us:
            self.rejected += 1
            return
        self.last_received_us = observed_us
        self._prune(observed_us)

        fields = ('T', 'S', 'i', 'p', 's', 'x', 'z', 'c', 't')
        row = {key: message.get(key) for key in fields}
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
            size = len(json.dumps(row, allow_nan=False,
                                  separators=(',', ':')).encode())
        except (RevisionAuditError, ValueError, OverflowError):
            self.rejected += 1
            return

        key = (row['S'], row['i'])
        if key in self.items:
            prior, old_size, _, old_observed, old_epoch = self.items[key]
            self.items[key] = (prior, old_size, True, old_observed, old_epoch)
            return
        if (size > self.max_bytes or len(self.items) >= self.max_items
                or self.bytes + size > self.max_bytes):
            self.overflowed = True
            raise OriginalTradeIndexOverflow('ORIGINAL_TRADE_INDEX_OVERFLOW')
        self.items[key] = (self._pack(row), size, False, observed_us, epoch)
        self.bytes += size

    def inspect(self, revision, *, epoch=None, received_at=None):
        if received_at is not None:
            try:
                observed_us = self._received_us(received_at)
                if self.last_received_us is None or observed_us >= self.last_received_us:
                    self.last_received_us = observed_us
                    self._prune(observed_us)
                else:
                    self.rejected += 1
            except ValueError:
                self.rejected += 1
        result = {
            'schema': 'OPR_ORIGINAL_TRADE_TIME_INDEX_V2',
            'retention_model': 'TIME_BOUNDED_FAIL_CLOSED',
            'retention_seconds': self.max_age_seconds,
            'max_items': self.max_items,
            'max_bytes': self.max_bytes,
            'retained': len(self.items), 'bytes': self.bytes,
            'expired': self.expired, 'rejected': self.rejected,
            'overflowed': self.overflowed,
            'original_trade_lookup_performed': True,
            'matched_within_retained_window': False,
            'upstream_completeness_proven': False,
            'native_bars_reconciled': False,
            'capture_ack_authorized': False,
            'eb_persistence_authorized': False,
            'direct_handoff_authorized': False,
        }
        symbol = revision.get('S')
        trade_id = revision.get('i' if revision.get('T') == 'x' else 'oi')
        if not isinstance(symbol, str) or type(trade_id) is not int:
            result['reason'] = 'REVISION_ID_INVALID'
            return result
        if revision.get('T') == 'x':
            action = revision.get('a')
            result['cancel_error_action'] = action if isinstance(action, str) else None
            result['cancel_error_action_documented'] = action in ('C', 'E')
            result['cancel_error_action_contract'] = 'ALPACA_V2_C_OR_E'
            if action not in ('C', 'E'):
                result['reason'] = 'CANCEL_ACTION_UNDOCUMENTED'
                return result
        if self.overflowed:
            result['reason'] = 'ORIGINAL_INDEX_OVERFLOW'
            return result
        found = self.items.get((symbol, trade_id))
        if found is None:
            result['reason'] = 'ORIGINAL_NOT_RETAINED'
            return result
        packed, _, ambiguous, _, original_epoch = found
        original = self._unpack(packed)
        result['original_frame'] = deepcopy(original)
        result['original_epoch'] = original_epoch
        result['revision_epoch'] = epoch
        result['same_epoch'] = epoch is not None and original_epoch == epoch
        if ambiguous:
            result['reason'] = 'ORIGINAL_ID_REPEATED'
            return result
        try:
            audit = audit_trade_revisions([original, revision], symbol=symbol,
                                          max_frames=2, max_ids=2)
        except RevisionAuditError as exc:
            result['reason'] = str(exc)
            return result
        result.update(
            reason=('PAIR_MATCHED_NOT_BAR_RECONCILED' if result['same_epoch']
                    else 'PAIR_MATCHED_CROSS_EPOCH_NOT_BAR_RECONCILED'),
            matched_within_retained_window=True,
            pair_sha256=audit['observed_frame_sha256'],
        )
        return result
