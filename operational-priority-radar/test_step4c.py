import unittest,math
from datetime import datetime,timezone,timedelta
from early_core_engine import *
from early_core_features import first_early_core_crossing
from alpaca_native5m import AlpacaNative5MinAdapter
def bars(n=100):
    s=datetime(2026,9,16,10,tzinfo=timezone.utc); out=[]
    for i in range(n):
        b=2+i*.02+math.sin(i/5)*.03
        out.append({"t":(s+timedelta(minutes=5*i)).isoformat().replace("+00:00","Z"),"o":b,"h":b+.04,"l":b-.02,"c":b+.015,"v":20000+i*500,"_timeframe":"native_5Min"})
    return out
class P:
    def __init__(self,r):self.r=r
    def fetch_native_5min(self,*a):return self.r
class C:
    def __init__(self):self.kw=None
    def bars(self,symbols,start,end,**kw):self.kw=kw;return {symbols[0]:[{"t":"2026-09-16T10:00:00Z","o":1,"h":2,"l":1,"c":2,"v":3}]}
class TestStep4C(unittest.TestCase):
    def test_unavailable_fail_closed(self):
        with self.assertRaisesRegex(Native5MinUnavailable,"EARLY_CORE_DATA_UNAVAILABLE"):
            FrozenEarlyCoreEngine(P(None)).first_crossing("A","R",datetime.now(timezone.utc),datetime.now(timezone.utc),datetime.now(timezone.utc))
    def test_synthetic_forbidden(self):
        r=bars();r[0]["_timeframe"]="aggregated_1m"
        with self.assertRaises(Synthetic5MinForbidden):
            FrozenEarlyCoreEngine(P(r)).first_crossing("A","R",datetime.now(timezone.utc),datetime.now(timezone.utc),datetime.now(timezone.utc))
    def test_reference_crossing_equivalence(self):
        r=bars(); ref=first_early_core_crossing(r); rec=datetime(2026,9,17,tzinfo=timezone.utc)
        got=FrozenEarlyCoreEngine(P(r)).first_crossing("A","R",datetime.now(timezone.utc),datetime.now(timezone.utc),rec)
        if ref is None:self.assertIsNone(got)
        else:self.assertEqual((got.bar_end_ts,got.score,got.observed_weight),(ref[0],ref[2],ref[3]))
    def test_decision_available_late(self):
        r=bars();ref=first_early_core_crossing(r)
        if ref is None:self.skipTest("no crossing")
        rec=ref[0]+timedelta(seconds=7);g=FrozenEarlyCoreEngine(P(r)).first_crossing("A","R",ref[0],ref[0],rec)
        self.assertEqual(g.decision_available_ts,rec)
    def test_decision_available_not_before_end(self):
        r=bars();ref=first_early_core_crossing(r)
        if ref is None:self.skipTest("no crossing")
        rec=ref[0]-timedelta(seconds=7);g=FrozenEarlyCoreEngine(P(r)).first_crossing("A","R",ref[0],ref[0],rec)
        self.assertEqual(g.decision_available_ts,g.bar_end_ts)
    def test_alpaca_literal_request(self):
        c=C();rows=AlpacaNative5MinAdapter(c).fetch_native_5min("A","R",datetime.now(timezone.utc),datetime.now(timezone.utc))
        self.assertEqual(c.kw,{"feed":"sip","adjustment":"raw","timeframe":"5Min"});self.assertEqual(rows[0]["_timeframe"],"native_5Min")
if __name__=="__main__":unittest.main()
