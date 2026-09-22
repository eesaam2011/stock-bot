import unittest,json
from production_universe import build_operational_universe,UniverseUnavailable
from alpaca_production_market import AlpacaREST,AlpacaCredentials
class Rest:
 def active_us_equity_assets(self):return [
  {"symbol":"AAPL","status":"active","asset_class":"us_equity","tradable":True},
  {"symbol":"BRK.B","status":"active","asset_class":"us_equity","tradable":True},
  {"symbol":"OLD","status":"inactive","asset_class":"us_equity","tradable":True},
  {"symbol":"CRYPTO","status":"active","asset_class":"crypto","tradable":True},
  {"symbol":"NO","status":"active","asset_class":"us_equity","tradable":False},
  {"symbol":"MSFT","status":"active","asset_class":"us_equity","tradable":True}]
class Resp:
 def __init__(self,data):self.data=data
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def read(self):return json.dumps(self.data).encode()
class TestStep16HAutoUniverse(unittest.TestCase):
 def test_filters_only_operational_eligibility(self):
  self.assertEqual(build_operational_universe(Rest()),["AAPL","BRK.B","MSFT"])
 def test_raw_alpaca_class_field_supported(self):
  class RawAlpaca:
   def active_us_equity_assets(self):return [
    {"symbol":"AAPL","status":"active","class":"us_equity","tradable":True},
    {"symbol":"BTCUSD","status":"active","class":"crypto","tradable":True},
    {"symbol":"BAD","status":"inactive","class":"us_equity","tradable":True}]
  self.assertEqual(build_operational_universe(RawAlpaca()),["AAPL"])
 def test_empty_fails_closed(self):
  class X:
   def active_us_equity_assets(self):return []
  with self.assertRaises(UniverseUnavailable):build_operational_universe(X())
 def test_invalid_fails_closed(self):
  class X:
   def active_us_equity_assets(self):return {}
  with self.assertRaises(UniverseUnavailable):build_operational_universe(X())
 def test_assets_endpoint_authenticated(self):
  seen={}
  def opener(req,timeout=0):
   seen["url"]=req.full_url;seen["headers"]=dict(req.header_items());return Resp([])
  AlpacaREST(AlpacaCredentials("K","S"),opener).active_us_equity_assets()
  self.assertIn("/v2/assets?status=active&asset_class=us_equity",seen["url"])
  self.assertEqual(seen["headers"]["Apca-api-key-id"],"K")
 def test_production_does_not_consume_opr_symbols(self):
  s=open("production_composition.py").read()
  self.assertNotIn('os.getenv("OPR_SYMBOLS"',s)
  self.assertIn("syms=build_operational_universe(rest)",s)
if __name__=="__main__":unittest.main()
