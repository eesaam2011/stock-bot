import unittest
from sip_trade_revision_audit import RevisionAuditError, audit_trade_revisions

TIME = '2026-09-24T14:00:00Z'


def trade(i=1, **kw):
    item = {'T':'t','S':'AAPL','t':TIME,'i':i,'p':10.5,'s':100,'c':[],'x':'V','z':'C'}
    item.update(kw)
    return item


def cancel(i=1, **kw):
    item = {'T':'x','S':'AAPL','t':TIME,'i':i,'p':10.5,'s':100,'x':'V','z':'C','a':'C'}
    item.update(kw)
    return item


def correction(**kw):
    item = {'T':'c','S':'AAPL','t':TIME,'oi':1,'op':10.5,'os':100,'oc':[],'ci':2,'cp':11.0,'cs':80,'cc':[]}
    item.update(kw)
    return item


class TestAudit(unittest.TestCase):
    def inspect(self, rows, **kw):
        return audit_trade_revisions(rows, symbol='AAPL', **kw)

    def test_cancel_diagnostic_only(self):
        result = self.inspect([trade(), cancel()])
        self.assertEqual((result['trade_frames'],result['cancel_frames'],result['active_trade_ids']), (1,1,0))
        for key in ('native_bars_reconciled','upstream_completeness_proven','full_session_coverage_proven','continuity_proven','capture_ack_authorized','direct_handoff_authorized','eb_persistence_authorized','retroactive_entries_allowed'):
            self.assertIs(result[key], False)

    def test_correct_then_cancel_new_identity(self):
        self.assertEqual(self.inspect([trade(),correction(),cancel(2,p=11.0,s=80)])['active_trade_ids'],0)

    def test_missing_duplicate_conflict_and_scope(self):
        cases = ([cancel()],[correction()],[trade(),trade()],[trade(),cancel(),cancel()],
                 [trade(),cancel(p=9)],[trade(),correction(op=9)],[trade(S='MSFT')],
                 [trade(t='yesterday')],[trade(p=float('nan'))],[trade(),correction(ci=1)],
                 [trade(),correction(cc='bad')],[trade(),cancel(a='')])
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(RevisionAuditError):
                self.inspect(rows)

    def test_limits_and_digest(self):
        with self.assertRaisesRegex(RevisionAuditError,'FRAME_LIMIT'):
            self.inspect([trade(),cancel()],max_frames=1)
        with self.assertRaisesRegex(RevisionAuditError,'TRADE_ID_LIMIT'):
            self.inspect([trade(),trade(2)],max_ids=1)
        with self.assertRaisesRegex(RevisionAuditError,'TRADE_ID_LIMIT'):
            self.inspect([trade(),correction()],max_ids=1)
        self.assertNotEqual(self.inspect([trade()])['observed_frame_sha256'],self.inspect([trade(s=101)])['observed_frame_sha256'])
