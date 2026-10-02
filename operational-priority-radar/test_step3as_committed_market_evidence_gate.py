"""Reject actual committed short market probes before any REST or E/B write."""
import json
import unittest
from pathlib import Path

from recovery_session_finalizer import finalize_empty_scope_session_recovery
from test_step3ag_recovery_session_finalizer import Leadership
from test_step3ad_full_session_semantic_validation import END


EVIDENCE = Path(__file__).parent / 'evidence'


class NoExternalSideEffects:
    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            self.calls += 1
            raise AssertionError('external effect must not run: ' + name)
        return forbidden


class TestCommittedEvidenceFailsBeforeRest(unittest.TestCase):
    def test_all_retained_market_probes_are_insufficient_for_eb(self):
        cases = (
            ('STEP3S_LIVE_SIP_MARKET_SESSION_EVIDENCE.json',
             'FULL_SESSION_EVIDENCE_INCOMPLETE'),
            ('STEP3AL_LIVE_CONTROLLED_RECONNECT_20260924.json',
             'FULL_SESSION_EVIDENCE_SCHEMA_INVALID'),
            ('STEP3AN_LIVE_RECONNECT_20260924.json',
             'FULL_SESSION_EVIDENCE_SCHEMA_INVALID'),
        )
        for filename, reason in cases:
            with self.subTest(filename=filename):
                evidence = json.loads((EVIDENCE / filename).read_text())
                rest = NoExternalSideEffects()
                with self.assertRaisesRegex(Exception, reason):
                    finalize_empty_scope_session_recovery(
                        evidence, rest, ['A'], {}, Leadership(), as_of=END)
                self.assertEqual(rest.calls, 0)


if __name__ == '__main__':
    unittest.main()
