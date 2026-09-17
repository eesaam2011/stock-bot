import unittest,asyncio
from production_halt_status import ProductionStatusTracker
from production_recovery import ProductionStartupRecovery
from datetime import datetime,timezone
UTC=timezone.utc
class Reader:
 def __init__(self,trades):self.trades=trades
 def active_trades(self):return self.trades
 def get_e(self,*a):return None
 def get_b(self,*a):return None
class REST:
 def native_1m_gap(self,*a):return []
 def native_5m(self,*a):return []
class TR:
 def __init__(self,result=None):self.calls=[];self.result=result or {"ambiguous":False,"state":"ACTIVE_PRE_T1"}
 def reconcile(self,t):self.calls.append(t);return self.result

class TestStep16E(unittest.TestCase):
 def test_alpaca_utp_halt_codes(self):
  x=ProductionStatusTracker()
  for c in ("H","P"):self.assertEqual(x.ingest({"S":"A","sc":c})["state"],"HALTED")
 def test_alpaca_cta_halt_code(self):
  self.assertEqual(ProductionStatusTracker().ingest({"S":"A","sc":"2"})["state"],"HALTED")
 def test_resume_codes(self):
  for c in ("3","Q","T"):self.assertEqual(ProductionStatusTracker().ingest({"S":"A","sc":c})["state"],"TRADING")
 def test_unknown_status_not_treated_as_trading(self):
  self.assertEqual(ProductionStatusTracker().ingest({"S":"A","sc":"5"})["state"],"OTHER")
 def test_halted_active_deferred_not_inferred(self):
  tr=TR();st=ProductionStatusTracker();t={"symbol":"A","state":"HALTED_ACTIVE","pre_halt_state":"ACTIVE_PRE_T1"}
  r=ProductionStartupRecovery(Reader([t]),REST(),tr,"S",["A"],lambda:datetime(2026,1,1,tzinfo=UTC),status_tracker=st)
  z=r.run();self.assertTrue(z["gap_recovered"]);self.assertFalse(z["reconciled"]);self.assertFalse(r.ready_after_stream());self.assertEqual(tr.calls,[])
 def test_halt_status_keeps_pending(self):
  tr=TR();st=ProductionStatusTracker();t={"symbol":"A","state":"HALTED_ACTIVE","pre_halt_state":"ACTIVE_PRE_T1"}
  r=ProductionStartupRecovery(Reader([t]),REST(),tr,"S",["A"],lambda:datetime(2026,1,1,tzinfo=UTC),status_tracker=st);r.run()
  r.on_status({"T":"s","S":"A","sc":"H"});self.assertFalse(r.ready_after_stream());self.assertEqual(tr.calls,[])
 def test_resume_proves_and_reconciles(self):
  tr=TR();st=ProductionStatusTracker();t={"symbol":"A","state":"HALTED_ACTIVE","pre_halt_state":"ACTIVE_POST_T1"}
  r=ProductionStartupRecovery(Reader([t]),REST(),tr,"S",["A"],lambda:datetime(2026,1,1,tzinfo=UTC),status_tracker=st);r.run()
  r.on_status({"T":"s","S":"A","sc":"T"});self.assertTrue(r.ready_after_stream());self.assertEqual(tr.calls[0]["state"],"ACTIVE_POST_T1")
 def test_bad_prehalt_state_never_resumes(self):
  tr=TR();st=ProductionStatusTracker();t={"symbol":"A","state":"HALTED_ACTIVE","pre_halt_state":"BAD"}
  r=ProductionStartupRecovery(Reader([t]),REST(),tr,"S",["A"],lambda:datetime(2026,1,1,tzinfo=UTC),status_tracker=st);r.run()
  r.on_status({"S":"A","sc":"T"});self.assertFalse(r.ready_after_stream());self.assertEqual(tr.calls,[])
 def test_ambiguous_resume_recovery_stays_untrusted(self):
  tr=TR({"ambiguous":True});st=ProductionStatusTracker();t={"symbol":"A","state":"HALTED_ACTIVE","pre_halt_state":"ACTIVE_PRE_T1"}
  r=ProductionStartupRecovery(Reader([t]),REST(),tr,"S",["A"],lambda:datetime(2026,1,1,tzinfo=UTC),status_tracker=st);r.run()
  r.on_status({"S":"A","sc":"T"});self.assertFalse(r.ready_after_stream())
if __name__=="__main__":unittest.main()
