import unittest
from datetime import datetime,timezone,timedelta
from entry_engine import *
T=datetime(2026,9,16,15,0,tzinfo=timezone.utc)
def x(p,to,ro,s=0,e=True):return SIPTrade(p,T+timedelta(seconds=to),T+timedelta(seconds=ro),s,e)
class TestStep7(unittest.TestCase):
 def test_wait(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=9),[],False).status,EntryStatus.WAITING)
 def test_deadline_expire(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=10),[],False).status,EntryStatus.EXPIRED)
 def test_exact_deadline_trade(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=10),[x(5,8,10)],False).status,EntryStatus.PRICE_READY)
 def test_age5_inclusive(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=7),[x(5,2,7)],False).status,EntryStatus.PRICE_READY)
 def test_over5_stale(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=7),[x(5,1.999,7)],False).status,EntryStatus.WAITING)
 def test_pretrigger_observation_forbidden(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=1),[x(5,-2,-1)],False).status,EntryStatus.WAITING)
 def test_pretrigger_trade_observed_after_allowed_if_fresh(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=1),[x(5,-1,1)],False).status,EntryStatus.PRICE_READY)
 def test_latest_observed(self):
  d=evaluate_entry_price(T,T+timedelta(seconds=5),[x(5,1,2,1),x(5.2,3,4,2)],False);self.assertEqual((d.entry_alert_price,d.trade_sequence),(5.2,2))
 def test_sequence_tie(self):
  d=evaluate_entry_price(T,T+timedelta(seconds=4),[x(5,2,4,10),x(5.1,3,4,11)],False);self.assertEqual(d.trade_sequence,11)
 def test_after_wait_rejected(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=11),[x(5,10,11)],False).status,EntryStatus.EXPIRED)
 def test_ineligible_ignored(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=2),[x(5,1,2,e=False)],False).status,EntryStatus.WAITING)
 def test_halt_final(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=2),[x(5,1,2)],True).status,EntryStatus.HALT_FINAL)
 def test_not_committed(self):self.assertEqual(evaluate_entry_price(T,T+timedelta(seconds=2),[x(7.25,1,2)],False).status.value,"ENTRY_PRICE_READY")
if __name__=="__main__":unittest.main()
