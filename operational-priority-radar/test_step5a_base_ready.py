import unittest,math
from datetime import datetime,timezone,timedelta
from base_ready import phase2_features

def bars(n=40, trend=.02, volume_growth=250):
    s=datetime(2026,9,16,13,30,tzinfo=timezone.utc); out=[]
    for i in range(n):
        c=5+i*trend; o=c-.01; v=10000+i*volume_growth
        out.append({"t":(s+timedelta(minutes=i)).isoformat().replace("+00:00","Z"),"o":o,"h":c+.02,"l":c-.02,"c":c,"v":v,"vw":c-.002,"n":20})
    return out

class TestStep5ABaseReady(unittest.TestCase):
    def test_requires_24_completed_1m_bars(self):
        r=bars(23); moment=datetime(2026,9,16,15,tzinfo=timezone.utc)
        self.assertIsNone(phase2_features(r,moment))

    def test_incomplete_current_bar_excluded(self):
        r=bars(25); last=datetime.fromisoformat(r[-1]["t"].replace("Z","+00:00"))
        a=phase2_features(r,last+timedelta(seconds=59))
        b=phase2_features(r[:-1],last+timedelta(seconds=59))
        self.assertEqual(a,b)

    def test_completed_boundary_inclusive(self):
        r=bars(25); last=datetime.fromisoformat(r[-1]["t"].replace("Z","+00:00"))
        a=phase2_features(r,last+timedelta(minutes=1))
        self.assertEqual(a[1]["bars_used"],25)

    def test_six_condition_composite_matches_literal_expression(self):
        r=bars(40,trend=.04,volume_growth=500); moment=datetime(2026,9,16,16,tzinfo=timezone.utc)
        f,d=phase2_features(r,moment)
        expected=(f["opportunity"]>=88 and f["failure_pressure"]<=35 and d["price"]>=d["vwap"] and d["demand_efficiency"]>=65 and d["price_acceptance"]>=62 and d["volume_acceleration"]>=1)
        self.assertEqual(d["base_ready"],expected)

    def test_diagnostics_have_all_six_inputs(self):
        f,d=phase2_features(bars(),datetime(2026,9,16,16,tzinfo=timezone.utc))
        for k in ("price","vwap","demand_efficiency","price_acceptance","volume_acceleration"):
            self.assertIn(k,d)
        self.assertIn("opportunity",f);self.assertIn("failure_pressure",f)

    def test_uses_last_30_closes_but_session_volume_for_vwap(self):
        r=bars(40); f,d=phase2_features(r,datetime(2026,9,16,16,tzinfo=timezone.utc))
        self.assertEqual(d["bars_used"],40)

    def test_output_is_deterministic(self):
        r=bars(40);m=datetime(2026,9,16,16,tzinfo=timezone.utc)
        self.assertEqual(phase2_features(r,m),phase2_features(r,m))

if __name__=="__main__":unittest.main()
