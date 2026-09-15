import ast, hashlib, json, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
SRC=ROOT/"independent_priority_radar.py"
TEXT=SRC.read_text()
TREE=ast.parse(TEXT)

def assignment(name):
    for n in TREE.body:
        if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(name)

class TestPostOOSEvidenceFreezeRelease(unittest.TestCase):
    def test_version_build(self):
        self.assertEqual(assignment("VERSION"),"1.7.59")
        self.assertIn("POST-OOS-EVIDENCE-FREEZE-A",assignment("BUILD"))
    def test_frozen_result_sha(self):
        spec=assignment("BACKWARD_OOS_2018_H1_POST_OOS_FREEZE_SPEC")
        self.assertEqual(spec["required_h1_result_sha256"],"edf7d9402b10ddf24ee1c6e9ce36e2867379de9059bc80422c73d66b88ebd9fa")
    def test_formal_fail_immutable(self):
        spec=assignment("BACKWARD_OOS_2018_H1_POST_OOS_FREEZE_SPEC")
        self.assertEqual(spec["formal_decision"],"H1_BACKWARD_OOS_FAIL")
        self.assertTrue(spec["formal_decision_is_immutable"])
    def test_evidence_counts(self):
        e=assignment("BACKWARD_OOS_2018_H1_POST_OOS_FREEZE_SPEC")["evidence_summary"]
        self.assertEqual((e["mandatory_views"],e["views_passing_all_frozen_gates"]),(4,3))
        self.assertEqual((e["views_with_positive_primary_improvement"],e["views_passing_safety"],e["views_passing_retention"]),(4,4,4))
    def test_near_miss_exact(self):
        x=assignment("BACKWARD_OOS_2018_H1_POST_OOS_FREEZE_SPEC")["evidence_summary"]["sole_failed_view"]
        self.assertEqual((x["window_minutes"],x["selection"]),(30,"top_3"))
        self.assertAlmostEqual(x["primary_improvement_absolute"],0.046159162320494385)
        self.assertAlmostEqual(x["required"],0.05)
    def test_firewall(self):
        f=assignment("BACKWARD_OOS_2018_H1_POST_OOS_FREEZE_SPEC")["firewall"]
        self.assertTrue(all(v is False for v in f.values()))
    def test_protocol_only_no_start(self):
        spec=assignment("BACKWARD_OOS_2018_H1_POST_OOS_FREEZE_SPEC")
        self.assertTrue(spec["protocol_only"])
        self.assertNotIn('/h1-post-oos-freeze/start',TEXT)
    def test_route_before_main(self):
        self.assertLess(TEXT.index('/h1-post-oos-freeze/protocol'),TEXT.index('if __name__ == "__main__":'))
    def test_historical_exec_spec_unchanged(self):
        old=Path('/mnt/data/ipr1758/independent_priority_radar.py')
        if old.exists():
            oldtext=old.read_text()
            a=oldtext[oldtext.index('BACKWARD_OOS_2018_H1_EXEC_SPEC = {'):oldtext.index('BACKWARD_OOS_2018_H1_EXEC_SHA256=')]
            b=TEXT[TEXT.index('BACKWARD_OOS_2018_H1_EXEC_SPEC = {'):TEXT.index('BACKWARD_OOS_2018_H1_EXEC_SHA256=')]
            self.assertEqual(hashlib.sha256(a.encode()).hexdigest(),hashlib.sha256(b.encode()).hexdigest())
    def test_no_execution_logic_in_freeze_block(self):
        block=TEXT[TEXT.index('# v1.7.59 — 2018 H1 Post-OOS Evidence Freeze'):TEXT.index('if __name__ == "__main__":')]
        self.assertNotIn('threading.Thread(',block)
        self.assertNotIn('_fd_fetch_session_rows(',block)
        self.assertNotIn('_ctr_fetch_1m(',block)

if __name__ == "__main__": unittest.main()
