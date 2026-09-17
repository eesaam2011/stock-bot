import json, math, unittest, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from early_core_score import ARTIFACT, score_feature_values, passes_early_core, frozen_pass_from_score

EXPECTED_MODEL_SHA = "c543ed4320a9cbc7eecef311675fb8955642d9bcb81e31fe7888728ee1c5c7c3"
EXPECTED_THRESHOLD = 0.5205528990060366

def reference_score(defs, values):
    num=den=0.0
    for d in defs:
        v=(values.get(str(d["anchor_minutes"])) or {}).get(d["feature"])
        if not isinstance(v,(int,float)) or not math.isfinite(v): continue
        c=d["direction"]*(float(v)-d["hard_negative_center"])/d["pooled_symbol_sd"]
        c=max(-3.0,min(3.0,c))
        num += d["weight"]*c
        den += d["weight"]
    if den < 0.5: return None,den
    return num/den,den

class TestProvenance(unittest.TestCase):
    def test_exact_frozen_identity(self):
        self.assertEqual(ARTIFACT["freeze_id"], "IPR-FEATURE-SCORING-FREEZE-2026-09-08-B")
        self.assertEqual(ARTIFACT["frozen_model_sha256"], EXPECTED_MODEL_SHA)
        self.assertEqual(ARTIFACT["role_count"], 12)
        self.assertEqual(len(ARTIFACT["definitions"]), 12)
        self.assertEqual(ARTIFACT["threshold"], EXPECTED_THRESHOLD)

    def test_weights_normalized(self):
        self.assertAlmostEqual(sum(d["weight"] for d in ARTIFACT["definitions"]),1.0,places=14)

    def test_anchor_membership_is_frozen(self):
        self.assertTrue(all(d["anchor_minutes"] in (240,120,60) for d in ARTIFACT["definitions"]))

    def test_center_values_score_zero(self):
        vals={}
        for d in ARTIFACT["definitions"]:
            vals.setdefault(str(d["anchor_minutes"]),{})[d["feature"]]=d["hard_negative_center"]
        score,den,_=score_feature_values(vals)
        self.assertAlmostEqual(den,1.0,places=14)
        self.assertAlmostEqual(score,0.0,places=14)

    def test_positive_three_sd_clips_to_three(self):
        vals={}
        for d in ARTIFACT["definitions"]:
            x=d["hard_negative_center"] + d["direction"]*10*d["pooled_symbol_sd"]
            vals.setdefault(str(d["anchor_minutes"]),{})[d["feature"]]=x
        score,den,_=score_feature_values(vals)
        self.assertAlmostEqual(score,3.0,places=12)

    def test_negative_three_sd_clips_to_minus_three(self):
        vals={}
        for d in ARTIFACT["definitions"]:
            x=d["hard_negative_center"] - d["direction"]*10*d["pooled_symbol_sd"]
            vals.setdefault(str(d["anchor_minutes"]),{})[d["feature"]]=x
        score,den,_=score_feature_values(vals)
        self.assertAlmostEqual(score,-3.0,places=12)

    def test_missing_weight_below_half_is_unscoreable(self):
        vals={}
        # Add definitions in ascending weight until still below 0.5.
        total=0.0
        for d in sorted(ARTIFACT["definitions"], key=lambda x:x["weight"]):
            if total+d["weight"] >= 0.5: break
            vals.setdefault(str(d["anchor_minutes"]),{})[d["feature"]]=d["hard_negative_center"]
            total += d["weight"]
        score,den,_=score_feature_values(vals)
        self.assertLess(den,0.5)
        self.assertIsNone(score)

    def test_equivalence_fixture_mixed_values(self):
        vals={}
        multipliers=[-2.7,-1.2,-0.4,0.0,0.3,0.8,1.1,1.9,2.6,3.4,-4.0,0.55]
        for d,m in zip(ARTIFACT["definitions"],multipliers):
            vals.setdefault(str(d["anchor_minutes"]),{})[d["feature"]]=d["hard_negative_center"] + d["direction"]*m*d["pooled_symbol_sd"]
        a,den,_=score_feature_values(vals)
        b,bden=reference_score(ARTIFACT["definitions"],vals)
        self.assertAlmostEqual(den,bden,places=15)
        self.assertAlmostEqual(a,b,places=15)

    def test_literal_raw_float_threshold_comparator(self):
        self.assertTrue(frozen_pass_from_score(EXPECTED_THRESHOLD))
        self.assertFalse(frozen_pass_from_score(math.nextafter(EXPECTED_THRESHOLD, -math.inf)))
        self.assertTrue(frozen_pass_from_score(math.nextafter(EXPECTED_THRESHOLD, math.inf)))

    def test_inverse_constructed_fixture_documents_binary_roundoff(self):
        vals={}
        for d in ARTIFACT["definitions"]:
            vals.setdefault(str(d["anchor_minutes"]),{})[d["feature"]] = d["hard_negative_center"] + d["direction"]*EXPECTED_THRESHOLD*d["pooled_symbol_sd"]
        passed,score,den,_=passes_early_core(vals)
        self.assertAlmostEqual(score,EXPECTED_THRESHOLD,places=14)
        self.assertLess(score,EXPECTED_THRESHOLD)
        self.assertFalse(passed)


if __name__=="__main__":
    unittest.main()
