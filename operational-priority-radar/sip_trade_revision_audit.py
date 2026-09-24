"""Bounded read-only inspection of Alpaca SIP trade corrections and cancels.

Diagnostic only: does not prove tape completeness, bar equivalence or E/B.
"""
import hashlib
import json
import math
from datetime import datetime


class RevisionAuditError(ValueError):
    pass


def _number(v):
    return type(v) in (int, float) and math.isfinite(v) and v > 0


def _integer(v):
    return type(v) is int and v > 0


def _timestamp(v):
    try:
        return isinstance(v, str) and datetime.fromisoformat(v.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def audit_trade_revisions(frames, *, symbol, max_frames=4096, max_ids=4096):
    """Inspect ordered raw frames for exactly one symbol; retain no payload in report."""
    if not isinstance(symbol, str) or not symbol or type(max_frames) is not int or not 1 <= max_frames <= 100000 or type(max_ids) is not int or not 1 <= max_ids <= 100000:
        raise RevisionAuditError('SCOPE_INVALID')
    active = {}
    seen = set()
    counts = {'t': 0, 'c': 0, 'x': 0}
    digest = hashlib.sha256()
    total = 0
    for frame in frames:
        total += 1
        if total > max_frames:
            raise RevisionAuditError('FRAME_LIMIT')
        if not isinstance(frame, dict) or frame.get('S') != symbol or frame.get('T') not in counts or not _timestamp(frame.get('t')):
            raise RevisionAuditError('FRAME_SCOPE_OR_TIME')
        kind = frame['T']
        if kind == 't':
            trade_id = frame.get('i')
            if not _integer(trade_id) or not _number(frame.get('p')) or not _integer(frame.get('s')) or not isinstance(frame.get('c'), list) or not all(isinstance(x, str) for x in frame['c']) or not isinstance(frame.get('x'), str) or not frame['x'] or not isinstance(frame.get('z'), str) or not frame['z']:
                raise RevisionAuditError('TRADE_SCHEMA')
            if trade_id in seen:
                raise RevisionAuditError('TRADE_ID_REUSED')
            if len(seen) >= max_ids:
                raise RevisionAuditError('TRADE_ID_LIMIT')
            seen.add(trade_id)
            active[trade_id] = (frame['p'], frame['s'], frame['x'], frame['z'], frame['c'])
        else:
            trade_id = frame.get('i' if kind == 'x' else 'oi')
            if not _integer(trade_id) or trade_id not in active:
                raise RevisionAuditError('ORIGINAL_MISSING')
            original = active[trade_id]
            price = frame.get('p' if kind == 'x' else 'op')
            size = frame.get('s' if kind == 'x' else 'os')
            if not _number(price) or not _integer(size) or (price, size) != original[:2]:
                raise RevisionAuditError('ORIGINAL_CONFLICT')
            if kind == 'x':
                if frame.get('x') != original[2] or frame.get('z') != original[3] or not isinstance(frame.get('a'), str) or not frame['a']:
                    raise RevisionAuditError('CANCEL_SCHEMA')
                del active[trade_id]
            else:
                corrected_id = frame.get('ci')
                if frame.get('oc') != original[4] or not _integer(corrected_id) or corrected_id in seen or not _number(frame.get('cp')) or not _integer(frame.get('cs')) or not isinstance(frame.get('cc'), list) or not all(isinstance(x, str) for x in frame['cc']):
                    raise RevisionAuditError('CORRECTION_SCHEMA')
                if len(seen) >= max_ids:
                    raise RevisionAuditError('TRADE_ID_LIMIT')
                seen.add(corrected_id)
                del active[trade_id]
                active[corrected_id] = (frame['cp'], frame['cs'], original[2], original[3], frame['cc'])
        counts[kind] += 1
        digest.update(json.dumps(frame, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        digest.update(b'\n')
    return {'symbol': symbol, 'frames_observed': total, 'trade_frames': counts['t'],
            'correction_frames': counts['c'], 'cancel_frames': counts['x'],
            'active_trade_ids': len(active), 'observed_frame_sha256': digest.hexdigest(),
            'original_trade_links_valid_within_supplied_slice': True,
            'native_bars_reconciled': False, 'upstream_completeness_proven': False,
            'full_session_coverage_proven': False, 'continuity_proven': False,
            'capture_ack_authorized': False, 'direct_handoff_authorized': False,
            'eb_persistence_authorized': False, 'retroactive_entries_allowed': False}
