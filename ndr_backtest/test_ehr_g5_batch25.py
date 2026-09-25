import datetime as dt
import importlib.util
import sys
from pathlib import Path
import unittest

BASE=Path(__file__).parent
sys.path.insert(0,str(BASE))
spec=importlib.util.spec_from_file_location("ehr_g5_batch25",BASE/"ehr_g5_batch25.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Batch25SafetyTests(unittest.TestCase):
    def test_market_gate(self):
        self.assertFalse(m.after_close(dt.datetime(2026,9,24,18,0,tzinfo=dt.timezone.utc)))
        self.assertTrue(m.after_close(dt.datetime(2026,9,24,22,0,tzinfo=dt.timezone.utc)))
    def test_batch_cap_is_literal_25(self):
        text=(BASE/"ehr_g5_batch25.py").read_text()
        self.assertIn("symbols=pending[:25]",text)
    def test_hard_budget_capped_at_180(self):
        text=(BASE/"ehr_g5_batch25.py").read_text()
        self.assertIn("min(180.0,max(30.0",text)
    def test_no_redis_or_returns(self):
        text=(BASE/"ehr_g5_batch25.py").read_text().lower()
        self.assertNotIn("redis.",text)
        self.assertNotIn("profit",text)
        self.assertNotIn("return_pct",text)
    def test_entrypoint_registration(self):
        text=(BASE/"independent_priority_radar.py").read_text()
        self.assertIn("register_ehr_g5_batch25(app, export_authorized)",text)

if __name__=="__main__": unittest.main()
