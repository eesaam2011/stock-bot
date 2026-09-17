import unittest,json
from datetime import datetime,timezone,timedelta
from alpaca_production_market import *
from production_recovery import *
T=datetime(2026,9,16,15,0,tzinfo=timezone.utc)
class Resp:
 def __init__(self,d):self.d=d
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def read(self):return json.dumps(self.d).encode()
class TestStep11(unittest.TestCase):
 def test_sip_url(self):self.assertEqual(SIP_STREAM_URL,"wss://stream.data.alpaca.markets/v2/sip")
 def test_subscription(self):
  s=AlpacaSIPProtocol.subscribe(["A"]);self.assertEqual(s,{"action":"subscribe","trades":["A"],"quotes":["A"],"bars":["A"],"statuses":["*"]})
 def test_auth(self):self.assertEqual(AlpacaSIPProtocol.auth("k","s"),{"action":"auth","key":"k","secret":"s"})
 def test_types(self):self.assertEqual([AlpacaSIPProtocol.classify({"T":x}) for x in ["t","q","b","s"]],["TRADE","QUOTE","BAR","STATUS"])
 def test_native5m_params(self):
  seen=[]
  def op(req,timeout=0):seen.append(req.full_url);return Resp({"bars":[{"t":"x"}],"next_page_token":None})
  r=AlpacaREST(AlpacaCredentials("k","s"),op).native_5m("A",T,T);self.assertEqual(r[0]["_timeframe"],"native_5Min")
  self.assertIn("timeframe=5Min",seen[0]);self.assertIn("feed=sip",seen[0]);self.assertIn("adjustment=raw",seen[0])
 def test_native1m_params(self):
  seen=[]
  def op(req,timeout=0):seen.append(req.full_url);return Resp({"bars":[],"next_page_token":None})
  AlpacaREST(AlpacaCredentials("k","s"),op).native_1m_gap("A",T,T);self.assertIn("timeframe=1Min",seen[0]);self.assertIn("feed=sip",seen[0])
 def test_pagination(self):
  c=[]
  def op(req,timeout=0):
   c.append(req.full_url);return Resp({"bars":[{"t":len(c)}],"next_page_token":"N" if len(c)==1 else None})
  self.assertEqual(len(AlpacaREST(AlpacaCredentials("k","s"),op).bars("A",T,T,"1Min")),2);self.assertIn("page_token=N",c[1])
 def test_native5_failclosed(self):
  def op(*a,**k):raise OSError()
  with self.assertRaisesRegex(Native5MinDataUnavailable,"EARLY_CORE_DATA_UNAVAILABLE"):AlpacaREST(AlpacaCredentials("k","s"),op).native_5m("A",T,T)
 def test_recovery_overlap(self):
  class R:
   def __init__(self):self.c=[]
   def native_1m_gap(self,s,a,b):self.c.append(("1",a));return [{"t":"2026-09-16T15:00:00Z"}]
   def native_5m(self,s,a,b):self.c.append(("5",a));return [{"t":"2026-09-16T15:00:00Z"}]
  r=R();o=ProductionGapRecovery(r).recover("A",T,T);self.assertEqual(r.c[0][1],T-timedelta(seconds=120));self.assertFalse(o["trusted"])
 def test_recovery_5m_failure(self):
  class R:
   def native_1m_gap(self,*a):return []
   def native_5m(self,*a):raise Native5MinDataUnavailable()
  o=ProductionGapRecovery(R()).recover("A",T,T);self.assertEqual(o["status"],"EARLY_CORE_DATA_UNAVAILABLE");self.assertFalse(o["trusted"])
if __name__=="__main__":unittest.main()
