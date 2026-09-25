from pathlib import Path
import unittest
class Batch25StaticSafety(unittest.TestCase):
 def test_caps_and_no_redis(self):
  s=Path(__file__).with_name("ehr_g5_batch25.py").read_text().lower()
  self.assertIn("pending[:25]",s); self.assertIn("min(180.0,max(30.0",s)
  self.assertNotIn("redis.",s); self.assertNotIn("return_pct",s); self.assertNotIn("profit_factor",s)
 def test_after_close_gate(self):
  s=Path(__file__).with_name("ehr_g5_batch25.py").read_text()
  self.assertIn("(17,30)",s)
if __name__=="__main__":unittest.main()
