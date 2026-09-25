from pathlib import Path
import importlib.util,sys,unittest
BASE=Path(__file__).parent
sys.path.insert(0,str(BASE))
spec=importlib.util.spec_from_file_location("ehr_g5_batch25",BASE/"ehr_g5_batch25.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class T(unittest.TestCase):
 def test_unique_endpoint_names(self):
  from flask import Flask
  app=Flask(__name__)
  m.register(app,lambda:True)
  self.assertIn("ehr_g5_batch25_start",app.view_functions)
  self.assertIn("ehr_g5_batch25_status",app.view_functions)
 def test_can_coexist_with_generic_endpoint_names(self):
  from flask import Flask
  app=Flask(__name__)
  @app.get("/dummy")
  def status(): return "ok"
  @app.post("/dummy2")
  def start(): return "ok"
  m.register(app,lambda:True)
  self.assertIn("ehr_g5_batch25_status",app.view_functions)
if __name__=="__main__":unittest.main()
