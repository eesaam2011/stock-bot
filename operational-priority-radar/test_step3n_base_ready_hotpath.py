"""Step3N: protect BASE_READY's frozen signal output and bounded tuple storage."""
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from production_adapters import OperationalBaseReady
from base_ready import phase2_features

START=datetime(2026,9,22,14,30,tzinfo=timezone.utc)
def bar(i,valid=True):
    return {"t":(START+timedelta(minutes=i)).isoformat() if valid else None,
            "o":10+i*.01,"h":10.3+i*.01,"l":9.9+i*.01,
            "c":10.1+i*.01,"v":1000+i*50,"vw":10.05+i*.01,"n":5}
def reference(rows):
    if not rows:return {"accepted":False,"base_ready":False}
    end=datetime.fromisoformat(rows[-1]["t"])+timedelta(minutes=1)
    x=phase2_features(rows[-60:],end)
    if x is None:return {"accepted":False,"base_ready":False}
    features,diag=x
    return {"accepted":True,"base_ready":diag["base_ready"],
            "features":features,"diagnostics":diag,
            "decision_available_ts":end}

class TestBaseReadyHotPath(unittest.TestCase):
    def test_short_histories_never_materialize_or_call_frozen_features(self):
        b=OperationalBaseReady()
        with patch.object(b,"_materialize_history",side_effect=AssertionError("allocated dicts")),patch(
            "production_adapters.phase2_features",side_effect=AssertionError("ran frozen features")):
            for i in range(23):
                self.assertEqual(b.on_completed_native_1m("A",bar(i),
                    START+timedelta(minutes=i+1)),
                    {"accepted":False,"base_ready":False})
        self.assertEqual(b.buffered_bars(),23)
        self.assertTrue(all(isinstance(x,tuple) for x in b.history["A"]))
    def test_24th_bar_invokes_original_frozen_features_once(self):
        b=OperationalBaseReady()
        for i in range(23):b.on_completed_native_1m("A",bar(i),
            START+timedelta(minutes=i+1))
        with patch("production_adapters.phase2_features",wraps=phase2_features) as feature:
            out=b.on_completed_native_1m("A",bar(23),
                START+timedelta(minutes=24))
        self.assertEqual(feature.call_count,1)
        self.assertEqual(out,reference([bar(i) for i in range(24)]))
    def test_frozen_reference_parity_at_24_30_45_60_90(self):
        b=OperationalBaseReady();rows=[]
        for i in range(90):
            x=bar(i);rows.append(x)
            out=b.on_completed_native_1m("A",x,START+timedelta(minutes=i+1))
            if i+1 in (24,30,45,60,90):
                self.assertEqual(out,reference(rows))
            self.assertLessEqual(b.buffered_bars(),60)
        self.assertEqual(len(b.history["A"]),60)
    def test_many_short_lived_symbols_have_no_persistent_dict_cache(self):
        b=OperationalBaseReady()
        for s in range(250):
            for i in range(5):
                b.on_completed_native_1m(str(s),bar(i),
                    START+timedelta(minutes=i+1))
        self.assertEqual(b.buffered_bars(),1250)
        self.assertEqual(len(b.__dict__),1)
        self.assertTrue(all(isinstance(x,tuple) for h in b.history.values() for x in h))
        for s in range(250):b.release_symbol(str(s))
        self.assertEqual(b.buffered_bars(),0)
        self.assertEqual(b.history,{})
    def test_invalid_timestamp_at_24_does_not_get_silently_cached(self):
        b=OperationalBaseReady()
        for i in range(23):b.on_completed_native_1m("A",bar(i))
        x=bar(23);x["t"]="not-a-date"
        with self.assertRaises(ValueError):
            b.on_completed_native_1m("A",x)
        self.assertEqual(len(b.history["A"]),24)
        self.assertTrue(all(isinstance(x,tuple) for x in b.history["A"]))
    def test_preflight_never_changes_early_core_or_frozen_threshold(self):
        from early_core_score import ARTIFACT
        b=OperationalBaseReady()
        self.assertEqual(b._BAR_KEYS,("t","o","h","l","c","v","vw","n"))
        self.assertEqual(len(b._compact_bar(bar(0))),8)
        self.assertAlmostEqual(float(ARTIFACT["threshold"]),0.5205528990060366)

if __name__=="__main__":unittest.main()
