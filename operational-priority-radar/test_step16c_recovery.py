import unittest,json
from datetime import datetime,timezone
from production_recovery import *
UTC=timezone.utc
class Reader:
 def __init__(self,trades=None,e=None,b=None):self.trades=trades or [];self.e=e;self.b=b
 def active_trades(self):return self.trades
 def get_e(self,*a):return self.e
 def get_b(self,*a):return self.b
class REST:
 def __init__(self):self.calls=[]
 def native_1m_gap(self,s,a,b):self.calls.append(("1",s,a,b));return [{"t":"2026-01-01T00:01:00Z"},{"t":"2026-01-01T00:01:00Z"}]
 def native_5m(self,s,a,b):self.calls.append(("5",s,a,b));return [{"t":"2026-01-01T00:00:00Z","_timeframe":"native_5Min"}]
class TR:
 def __init__(self,result=True):self.result=result;self.calls=[]
 def reconcile(self,t):self.calls.append(t);return self.result
class TestStep16C(unittest.TestCase):
 def test_success_gap_and_reconcile(self):
  r=REST();x=ProductionStartupRecovery(Reader(),r,TR(),"2026-01-01",["A"],lambda:datetime(2026,1,1,1,tzinfo=UTC))
  self.assertEqual(x.run(),{"gap_recovered":True,"reconciled":True});self.assertEqual(len(r.calls),2)
 def test_overlap_dedup(self):
  x=ProductionStartupRecovery(Reader(),REST(),TR(),"S",["A"],lambda:datetime(2026,1,1,1,tzinfo=UTC));x.run();self.assertEqual(len(x.recovered_1m["A"]),1)
 def test_active_trade_first_and_failure_blocks(self):
  tr=TR(False);r=REST();x=ProductionStartupRecovery(Reader([{"state":"ACTIVE_PRE_T1"}]),r,tr,"S",["A"],lambda:datetime(2026,1,1,1,tzinfo=UTC))
  z=x.run();self.assertFalse(z["reconciled"]);self.assertEqual(len(r.calls),0)
 def test_ambiguous_trade_blocks(self):
  tr=TR({"ambiguous":True});x=ProductionStartupRecovery(Reader([{"state":"ACTIVE_PRE_T1"}]),REST(),tr,"S",["A"],lambda:datetime(2026,1,1,1,tzinfo=UTC));self.assertFalse(x.run()["gap_recovered"])
 def test_native5_failure_blocks(self):
  class X(REST):
   def native_5m(self,*a):raise RuntimeError("EARLY_CORE_DATA_UNAVAILABLE")
  x=ProductionStartupRecovery(Reader(),X(),TR(),"S",["A"],lambda:datetime(2026,1,1,1,tzinfo=UTC));self.assertFalse(x.run()["gap_recovered"])
 def test_canonical_anchor_gets_120s_overlap(self):
  e={"decision_available_ts":"2026-01-01T00:30:00+00:00"};r=REST()
  x=ProductionStartupRecovery(Reader(e=e),r,TR(),"S",["A"],lambda:datetime(2026,1,1,1,tzinfo=UTC));x.run()
  self.assertEqual(r.calls[0][2],datetime(2026,1,1,0,28,tzinfo=UTC))
if __name__=="__main__":unittest.main()
