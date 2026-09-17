import unittest
from production_universe import build_operational_universe
class R:
 def active_us_equity_assets(self):return [
  {"symbol":"BRK.B","status":"active","asset_class":"us_equity","tradable":True},
  {"symbol":"BF.B","status":"active","asset_class":"us_equity","tradable":True},
  {"symbol":"AAPL","status":"active","asset_class":"us_equity","tradable":True},
  {"symbol":"BAD","status":"inactive","asset_class":"us_equity","tradable":True},
  {"symbol":"NO","status":"active","asset_class":"us_equity","tradable":False}]
class TestStep16ISymbolCoverage(unittest.TestCase):
 def test_dotted_symbols_preserved(self):
  self.assertEqual(build_operational_universe(R()),["AAPL","BF.B","BRK.B"])
 def test_no_isalpha_gate_remains(self):
  self.assertNotIn("isalpha",open("production_universe.py").read())
 def test_only_existing_operational_gates_remain(self):
  s=open("production_universe.py").read()
  self.assertIn('a.get("status")=="active"',s)
  self.assertIn('a.get("asset_class")=="us_equity"',s)
  self.assertIn('bool(a.get("tradable"))',s)
if __name__=="__main__":unittest.main()
