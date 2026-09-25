import importlib.util
from pathlib import Path
import unittest

class ActualEntrypointWiringTests(unittest.TestCase):
    def test_actual_render_entrypoint_registers_g5(self):
        text=Path(__file__).with_name("independent_priority_radar.py").read_text()
        self.assertIn("register_ehr_g5(app, export_authorized)", text)\n        self.assertNotIn("symbols.\\\\nfrom ehr_g5_web_pilot", text)
        self.assertLess(text.index("register_ehr_g5(app, export_authorized)"),
                        text.index('if __name__ == "__main__":'))
    def test_pilot_files_present(self):
        base=Path(__file__).parent
        for name in ("ehr_g5_web_pilot.py","ehr_g5_isolated_collector.py",
                     "ehr_g5_preflight.py","ehr_g5_pilot_page.html"):
            self.assertTrue((base/name).is_file(), name)

if __name__=="__main__":
    unittest.main()
