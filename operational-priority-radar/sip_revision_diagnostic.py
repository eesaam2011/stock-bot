"""Small allowlisted revision evidence; never a reconciliation or ACK permit."""
import math


def revision_diagnostic(message, *, epoch, last_sequence, acked_upto):
    frame = {}
    invalid = []
    fields = ('T', 'S', 'x', 'z', 't', 'i', 'p', 's', 'a') if message.get('T') == 'x' else (
        'T', 'S', 'x', 'z', 't', 'oi', 'op', 'os', 'oc', 'ci', 'cp', 'cs', 'cc')
    for key in fields:
        value = message.get(key)
        if key in ('oc', 'cc'):
            valid = (isinstance(value, list) and len(value) <= 32
                     and all(isinstance(v, str) and len(v) <= 16 for v in value))
            if valid: value = list(value)
        elif key in ('i', 's', 'oi', 'os', 'ci', 'cs'):
            valid = type(value) is int and 0 <= value < 2**64
        elif key in ('p', 'op', 'cp'):
            valid = type(value) in (int, float) and 0 < value < 1e12 and math.isfinite(value)
        else:
            valid = isinstance(value, str) and 0 < len(value) <= 128
        if valid:
            frame[key] = value
        else:
            invalid.append(key)
    return {'schema': 'OPR_SIP_REVISION_DIAGNOSTIC_V1',
            'epoch': epoch, 'last_sequence_before_revision': last_sequence,
            'acked_upto_before_revision': acked_upto,
            'frame': frame, 'invalid_or_oversized_fields': invalid,
            'original_trade_lookup_performed': False,
            'revision_reconciled': False, 'capture_ack_authorized': False,
            'direct_handoff_authorized': False}
