import unittest
from datetime import datetime,timezone,timedelta
from risk_engine import *
T=datetime(2026,9,16,15,0,tzinfo=timezone.utc)
def bar(end_min,low,recv_sec=0,tf="native_1Min",trusted=True):
    e=T+timedelta(minutes=end_min)
    return StructureBar(e-timedelta(minutes=1),e,low,e+timedelta(seconds=recv_sec),tf,trusted)

class TestStep8StructuralRisk(unittest.TestCase):
    def test_inclusive_both_boundaries(self):
        d=evaluate_structural_risk(10,T,T+timedelta(minutes=2),[bar(0,9.8),bar(2,9.7)])
        self.assertEqual(d.status,RiskStatus.APPROVED);self.assertEqual(d.structure_low,9.7)
    def test_b_producing_bar_at_trigger_can_be_structure(self):
        d=evaluate_structural_risk(10,T,T+timedelta(minutes=1),[bar(1,9.7)])
        self.assertEqual(d.structure_low,9.7)
    def test_delta_zero_can_have_valid_structure(self):
        d=evaluate_structural_risk(10,T,T,[bar(0,9.8)])
        self.assertEqual(d.status,RiskStatus.APPROVED)
    def test_no_valid_structure_final(self):
        self.assertEqual(evaluate_structural_risk(10,T,T,[]).status,RiskStatus.NO_STRUCTURE)
    def test_old_bar_forbidden(self):
        self.assertEqual(evaluate_structural_risk(10,T,T+timedelta(minutes=1),[bar(-1,9.8)]).status,RiskStatus.NO_STRUCTURE)
    def test_future_bar_forbidden(self):
        self.assertEqual(evaluate_structural_risk(10,T,T+timedelta(minutes=1),[bar(2,9.8)]).status,RiskStatus.NO_STRUCTURE)
    def test_delayed_bar_unknown_at_trigger_forbidden(self):
        self.assertEqual(evaluate_structural_risk(10,T,T+timedelta(minutes=1),[bar(1,9.8,recv_sec=1)]).status,RiskStatus.NO_STRUCTURE)
    def test_synthetic_or_wrong_timeframe_forbidden(self):
        self.assertEqual(evaluate_structural_risk(10,T,T,[bar(0,9.8,tf="synthetic_1Min")]).status,RiskStatus.NO_STRUCTURE)
    def test_untrusted_bar_forbidden(self):
        self.assertEqual(evaluate_structural_risk(10,T,T,[bar(0,9.8,trusted=False)]).status,RiskStatus.NO_STRUCTURE)
    def test_stop_buffer_exact_half_percent(self):
        d=evaluate_structural_risk(10,T,T,[bar(0,9.8)])
        self.assertAlmostEqual(d.structural_stop,9.8*.995,12)
    def test_risk_exactly_six_percent_allowed(self):
        # stop must be 9.4 for entry 10, therefore structure low=9.4/.995
        d=evaluate_structural_risk(10,T,T,[bar(0,9.4/.995)])
        self.assertEqual(d.status,RiskStatus.APPROVED);self.assertAlmostEqual(d.risk_pct,6.0,10)
    def test_risk_over_six_rejected(self):
        d=evaluate_structural_risk(10,T,T,[bar(0,9.39/.995)])
        self.assertEqual(d.status,RiskStatus.STRUCTURAL_RISK)
    def test_nonpositive_risk_rejected(self):
        d=evaluate_structural_risk(10,T,T,[bar(0,10.1)])
        self.assertEqual(d.status,RiskStatus.STRUCTURAL_RISK)
    def test_t1_t2_are_one_and_two_r(self):
        d=evaluate_structural_risk(10,T,T,[bar(0,9.8)])
        R=10-d.structural_stop
        self.assertAlmostEqual(d.t1,10+R,12);self.assertAlmostEqual(d.t2,10+2*R,12)
    def test_lowest_eligible_low_is_structure(self):
        d=evaluate_structural_risk(10,T,T+timedelta(minutes=2),[bar(0,9.9),bar(1,9.75),bar(2,9.85)])
        self.assertEqual(d.structure_low,9.75)

if __name__=="__main__":unittest.main()
