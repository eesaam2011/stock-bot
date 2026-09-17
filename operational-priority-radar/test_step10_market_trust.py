import unittest
from datetime import datetime,timezone,timedelta
from market_trust import *
from gap_recovery import *
T=datetime(2026,9,16,15,0,tzinfo=timezone.utc)
class TestStep10Trust(unittest.TestCase):
 def test_startup_full_path(self):
  t=Trust();t=transition(t,"START_RECOVERY");t=transition(t,"GAP_RECOVERED");t=transition(t,"STREAM_CONNECTED");t=transition(t,"RECONCILED");t=transition(t,"CONTINUITY_OK")
  self.assertEqual(t.state,TrustState.LIVE_TRUSTED);self.assertTrue(new_entries_allowed(t))
 def test_connected_not_trusted(self):
  t=transition(transition(transition(Trust(),"START_RECOVERY"),"GAP_RECOVERED"),"STREAM_CONNECTED")
  self.assertEqual(t.state,TrustState.VERIFYING_CONTINUITY);self.assertFalse(new_entries_allowed(t))
 def test_continuity_alone_not_enough(self):
  t=transition(Trust(),"CONTINUITY_OK");self.assertFalse(new_entries_allowed(t))
 def test_disconnect_revokes_trust(self):
  t=Trust(TrustState.LIVE_TRUSTED,True,True,True,True);t=transition(t,"DISCONNECT");self.assertEqual(t.state,TrustState.STREAM_UNTRUSTED);self.assertFalse(new_entries_allowed(t))
 def test_reconnect_requires_recovery_again(self):
  t=transition(Trust(TrustState.LIVE_TRUSTED,True,True,True,True),"DISCONNECT");t=transition(t,"BEGIN_RECONNECT");self.assertFalse(new_entries_allowed(t))
 def test_gap_dedup_overlap(self):
  rows=[{"symbol":"A","timeframe":"1m","bar_start_ts":T,"sequence":2},{"symbol":"A","timeframe":"1m","bar_start_ts":T,"sequence":1},{"symbol":"A","timeframe":"1m","bar_start_ts":T+timedelta(minutes=1),"sequence":1}]
  self.assertEqual(len(dedup_chronological(rows)),2)
 def test_both_e_b_inside_gap_is_missed_no_retro_entry(self):
  o=reconcile_opportunity(T+timedelta(minutes=2),T+timedelta(minutes=5),T,T+timedelta(minutes=10));self.assertEqual(o.status,"OPPORTUNITY_MISSED_DURING_DATA_GAP");self.assertFalse(o.new_entry_allowed)
 def test_active_trade_priority(self):
  x=active_trade_recovery_order([{"id":1,"active_trade":False},{"id":2,"active_trade":True}]);self.assertEqual(x[0]["id"],2)
 def test_ohlc_cannot_infer_stop_vs_target(self):
  self.assertEqual(recovery_path_from_ohlc(True,True,False),"RECOVERY_PATH_AMBIGUOUS")
 def test_chronology_can_resolve(self):
  self.assertEqual(recovery_path_from_ohlc(True,True,True),"CHRONOLOGY_PROVEN")
if __name__=="__main__":unittest.main()
