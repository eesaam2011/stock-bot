import unittest
from pathlib import Path
from shadow_worker_main import startup_probe_env
ROOT=Path(__file__).resolve().parents[1]
def blueprint_text():
    path=ROOT/"render.yaml"
    if not path.is_file():
        raise unittest.SkipTest("root render.yaml absent: Render release NOT CERTIFIED")
    return path.read_text()

class TestStep16(unittest.TestCase):
 def test_render_is_background_worker(self):
  y=blueprint_text();self.assertIn("type: worker",y)
 def test_start_command(self):
  y=blueprint_text();self.assertIn("startCommand: python shadow_worker_main.py",y)
 def test_shadow_forced_in_blueprint(self):
  y=blueprint_text();self.assertIn('value: "true"',y)
 def test_no_secrets_embedded(self):
  y=blueprint_text();self.assertIn("sync: false",y);self.assertNotIn("https://",y)
 def test_missing_redis_fails(self):
  p=startup_probe_env({"OPR_SHADOW_MODE":"true","ALPACA_API_KEY":"k","ALPACA_SECRET_KEY":"s"});self.assertFalse(p["ok"]);self.assertIn("REDIS_URL",p["missing"])
 def test_missing_alpaca_fails(self):
  p=startup_probe_env({"OPR_SHADOW_MODE":"true","REDIS_URL":"redis://x"});self.assertFalse(p["ok"])
 def test_nonshadow_fails_closed(self):
  p=startup_probe_env({"OPR_SHADOW_MODE":"false","REDIS_URL":"x","ALPACA_API_KEY":"k","ALPACA_SECRET_KEY":"s"});self.assertEqual(p["status"],"ACTIONABLE_MODE_BLOCKED")
 def test_shadow_config_ready(self):
  p=startup_probe_env({"OPR_SHADOW_MODE":"true","REDIS_URL":"x","ALPACA_API_KEY":"k","ALPACA_SECRET_KEY":"s"});self.assertEqual(p,{"ok":True,"status":"SHADOW_CONFIG_READY"})
 def test_slow_consumer_still_pending(self):
  import constants;self.assertIsNone(constants.SLOW_CONSUMER_THRESHOLD)
if __name__=="__main__":unittest.main()
