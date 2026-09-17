import unittest
from runtime_provenance import stamp,RUNTIME_MODE,SHADOW_MODE,RELEASE_ID
class TestStep16GShadowProvenance(unittest.TestCase):
 def test_stamp_is_explicit_shadow(self):
  x=stamp({"record_type":"trade"})
  self.assertTrue(x["shadow_mode"]);self.assertEqual(x["runtime_mode"],"SHADOW")
  self.assertEqual(x["release_id"],RELEASE_ID)
 def test_stamp_does_not_mutate_input(self):
  x={"a":1};y=stamp(x);self.assertNotIn("shadow_mode",x);self.assertTrue(y["shadow_mode"])
 def test_canonical_writers_stamp(self):
  for f in ("early_core_state.py","base_ready_state.py","confluence_state.py"):
   s=open(f).read();self.assertIn("stamp(r)",s)
 def test_entry_and_trade_transition_stamp(self):
  s=open("production_pipeline.py").read()
  self.assertIn("new,trade,out=stamp(new),stamp(trade),stamp(out)",s)
  self.assertIn("new,out=stamp(new),stamp(out)",s)
 def test_shadow_telegram_remains_forbidden(self):
  s=open("production_composition.py").read();self.assertIn("ForbiddenTelegram",s)
if __name__=="__main__":unittest.main()
