import datetime as dt
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).with_name("ehr_g5_web_pilot.py")
spec=importlib.util.spec_from_file_location("ehr_g5_web_pilot", PATH)
import sys
sys.path.insert(0,str(PATH.parent))
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class WebPilotTests(unittest.TestCase):
    def test_market_hours_rejected(self):
        # 2026-09-24 14:00 NY, regular session.
        self.assertFalse(module.after_close(dt.datetime(2026,9,24,18,0,tzinfo=dt.timezone.utc)))
    def test_after_close_allowed(self):
        self.assertTrue(module.after_close(dt.datetime(2026,9,24,22,0,tzinfo=dt.timezone.utc)))
    def test_weekend_allowed(self):
        self.assertTrue(module.after_close(dt.datetime(2026,9,26,15,0,tzinfo=dt.timezone.utc)))
    def test_1730_boundary(self):
        self.assertFalse(module.after_close(dt.datetime(2026,9,24,21,29,tzinfo=dt.timezone.utc)))
        self.assertTrue(module.after_close(dt.datetime(2026,9,24,21,30,tzinfo=dt.timezone.utc)))
    def test_disabled_by_default(self):
        from flask import Flask
        from unittest.mock import patch
        app=Flask(__name__)
        module.register(app,lambda: True)
        with patch.dict("os.environ", {"EHR_G5_ENABLED":"0"}):
            response=app.test_client().post("/ehr-g5/start")
            self.assertEqual(response.status_code,403)
            self.assertEqual(app.test_client().post("/ehr-g5/requirements", data=b"{}").status_code,403)
    def test_unauthorized(self):
        from flask import Flask
        app=Flask(__name__)
        module.register(app,lambda: False)
        self.assertEqual(app.test_client().post("/ehr-g5/start").status_code,401)
        self.assertEqual(app.test_client().get("/ehr-g5/status").status_code,401)
        self.assertEqual(app.test_client().post("/ehr-g5/requirements", data=b"{}").status_code,401)
        self.assertEqual(app.test_client().get("/ehr-g5/download").status_code,401)

if __name__=="__main__":
    unittest.main()
