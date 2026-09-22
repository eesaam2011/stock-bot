"""Step 3C: no historical ordering guesses or partial T1 Redis commits."""
import unittest,json
from datetime import timedelta
from test_step16d_active_trade_recovery import T,REST,trade,setup
from state_store import key_trade

def tick(seconds,price):
    return {'t':(T+timedelta(seconds=seconds)).isoformat(),'p':price}
def bar(start_seconds,close):
    return {'t':(T+timedelta(seconds=start_seconds)).isoformat(),'c':close}
def assert_unmodified(test,rd,original):
    test.assertEqual(rd.d[key_trade('TR')],original)
    test.assertFalse(any(':outbox:' in k for k in rd.d))

class TestSimultaneousRecovery(unittest.TestCase):
    def test_t1_and_stop_same_timestamp_ambiguous(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1),tick(1,8.9)]))
        before=rd.d[key_trade('TR')]
        z=rec.reconcile(t)
        self.assertTrue(z['ambiguous'])
        self.assertEqual(z['reason'],'RECOVERY_PATH_AMBIGUOUS')
        assert_unmodified(self,rd,before)
    def test_stop_then_t1_same_timestamp_also_ambiguous(self):
        t=trade();rec,rd=setup(t,REST([tick(1,8.9),tick(1,11.1)]))
        before=rd.d[key_trade('TR')]
        self.assertTrue(rec.reconcile(t)['ambiguous'])
        assert_unmodified(self,rd,before)
    def test_t1_then_t2_same_timestamp_has_different_outbox(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1),tick(1,12.5)]))
        before=rd.d[key_trade('TR')]
        self.assertTrue(rec.reconcile(t)['ambiguous'])
        assert_unmodified(self,rd,before)
    def test_later_ambiguity_does_not_commit_earlier_t1(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1),tick(2,8.9),tick(2,12.5)]))
        before=rd.d[key_trade('TR')]
        z=rec.reconcile(t)
        self.assertTrue(z['ambiguous'])
        assert_unmodified(self,rd,before)
    def test_duplicate_identical_trade_observations_safe(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1),tick(1,11.1),tick(2,12.5)]))
        z=rec.reconcile(t)
        self.assertFalse(z['ambiguous'])
        self.assertEqual(z['state'],'CLOSED_T2')
        self.assertEqual(json.loads(rd.d[key_trade('TR')])['state'],'CLOSED_T2')
    def test_same_timestamp_noop_and_stop_safe(self):
        t=trade();rec,rd=setup(t,REST([tick(1,10.2),tick(1,8.9)]))
        z=rec.reconcile(t)
        self.assertFalse(z['ambiguous'])
        self.assertEqual(z['state'],'CLOSED_STOP')
    def test_many_distinct_noop_trades_same_timestamp_safe(self):
        t=trade();rec,rd=setup(t,REST([tick(1,10+i/100) for i in range(20)]))
        z=rec.reconcile(t)
        self.assertFalse(z['ambiguous'])
        self.assertEqual(z['state'],'ACTIVE_PRE_T1')
        assert_unmodified(self,rd,rd.d[key_trade('TR')])
    def test_many_distinct_with_trigger_fails_bounded(self):
        t=trade();rec,rd=setup(t,REST([tick(1,10+i/100) for i in range(12)]+[tick(1,11.1)]))
        before=rd.d[key_trade('TR')]
        self.assertTrue(rec.reconcile(t)['ambiguous'])
        assert_unmodified(self,rd,before)
    def test_post_t1_simultaneous_stop_and_t2_ambiguous(self):
        t=trade('ACTIVE_POST_T1');rec,rd=setup(t,REST([tick(1,8.9),tick(1,12.5)]))
        before=rd.d[key_trade('TR')]
        self.assertTrue(rec.reconcile(t)['ambiguous'])
        assert_unmodified(self,rd,before)
    def test_preflight_ignores_events_after_proven_terminal(self):
        t=trade();rec,rd=setup(t,REST([tick(1,8.9),tick(2,11.1),tick(2,12.5)]))
        z=rec.reconcile(t)
        self.assertFalse(z['ambiguous'])
        self.assertEqual(z['state'],'CLOSED_STOP')
    def test_trade_and_bar_close_same_timestamp_post_t1_conflict(self):
        t=trade('ACTIVE_POST_T1')
        # Bar starts at T; its close at T+60s, same timestamp as the T2 trade.
        rec,rd=setup(t,REST([tick(60,12.5)],[bar(0,9.9)]))
        before=rd.d[key_trade('TR')]
        self.assertTrue(rec.reconcile(t)['ambiguous'])
        assert_unmodified(self,rd,before)
    def test_distinct_timestamps_preserve_original_order(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1),tick(2,8.9)]))
        z=rec.reconcile(t)
        self.assertFalse(z['ambiguous'])
        self.assertEqual(z['state'],'CLOSED_STOP')
        self.assertEqual(json.loads(rd.d[key_trade('TR')])['state'],'CLOSED_STOP')
    def test_preflight_cancellation_no_write(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1)]))
        before=rd.d[key_trade('TR')]
        import threading
        rec.cancel_event=threading.Event();rec.cancel_event.set()
        with self.assertRaisesRegex(Exception,'RECOVERY_CANCELLED'):
            rec.reconcile(t)
        assert_unmodified(self,rd,before)

if __name__=='__main__':unittest.main()
