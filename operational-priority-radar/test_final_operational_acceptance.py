"""Acceptance matrix must never mistake mock flags for production proof."""
import json,subprocess,sys,tempfile,unittest
from pathlib import Path
from operational_acceptance import assess_shadow_readiness,REQUIRED_EXTERNAL_PROOFS

class TestOperationalAcceptance(unittest.TestCase):
    def test_default_is_not_authorized(self):
        r=assess_shadow_readiness()
        self.assertEqual(r["status"],"NOT_AUTHORIZED_FOR_LIVE_SHADOW")
        self.assertEqual(tuple(r["outstanding_proofs"]),REQUIRED_EXTERNAL_PROOFS)
        self.assertFalse(r["render_deploy_authorized"])
        self.assertFalse(r["merge_authorized"])
    def test_fake_all_true_flags_do_not_grant_live_access(self):
        r=assess_shadow_readiness(
            ci_evidence={"pytest_passed":True,"real_redis_passed":True},
            recovery_audit={"api_page_chains_exhausted":True,
                "native_slot_audited_batches":1,
                "full_session_coverage_proven":True,
                "sip_continuity_proven":True,
                "replay_completed":True},
            capture_snapshot={"phase":"DRAINING","acked_upto":0,
                "direct_handoff_authorized":True},
            external_artifacts={x:True for x in REQUIRED_EXTERNAL_PROOFS})
        self.assertEqual(r["status"],"NOT_AUTHORIZED_FOR_LIVE_SHADOW")
        self.assertEqual(len(r["outstanding_proofs"]),8)
        self.assertFalse(r["external_proof_verification_implemented"])
        self.assertFalse(r["canonical_eb_backfill_authorized"])
        self.assertFalse(r["sip_direct_handoff_authorized"])
        self.assertFalse(r["retroactive_entry_authorized"])
    def test_ci_success_is_not_market_coverage_proof(self):
        r=assess_shadow_readiness(ci_evidence={
            "pytest_passed":True,"real_redis_passed":True})
        self.assertFalse(r["observed_offline_evidence"]["rest_page_chains_exhausted"])
        self.assertIn("rest_page_chains_not_verified",r["blockers"])
        self.assertIn("live_alpaca_sip_407_market_session_soak",r["blockers"])
    def test_draining_capture_is_not_direct_handoff(self):
        r=assess_shadow_readiness(capture_snapshot={
            "phase":"DRAINING","acked_upto":0})
        self.assertTrue(r["observed_offline_evidence"]["sip_capture_draining"])
        self.assertFalse(r["sip_direct_handoff_authorized"])
    def test_missing_redis_test_evidence_is_explicit_blocker(self):
        r=assess_shadow_readiness(ci_evidence={"pytest_passed":True})
        self.assertIn("ci_real_redis_not_verified",r["blockers"])
    def test_external_artifact_claims_are_unverified(self):
        r=assess_shadow_readiness(external_artifacts={
            "live_alpaca_sip_407_market_session_soak":"/tmp/fake.json"})
        self.assertEqual(len(r["observed_offline_evidence"][
            "unverified_external_artifacts_supplied"]),1)
        self.assertIn("live_alpaca_sip_407_market_session_soak",
                      r["outstanding_proofs"])
    def test_cli_generates_machine_readable_blocked_report(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"report.json"
            p=subprocess.run([sys.executable,"operational_acceptance.py",
                              "--output",str(path)],capture_output=True,
                             text=True,check=True)
            self.assertIn("NOT_AUTHORIZED_FOR_LIVE_SHADOW",p.stdout)
            self.assertEqual(json.loads(path.read_text())["schema"],
                             "OPR_SHADOW_ACCEPTANCE_V1")

    def test_cli_ci_success_still_blocked_on_live_proofs(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"report.json"
            subprocess.run([sys.executable,"operational_acceptance.py",
                "--ci-pytest-passed","--ci-real-redis-passed",
                "--output",str(path)],capture_output=True,text=True,check=True)
            report=json.loads(path.read_text())
            self.assertTrue(report["observed_offline_evidence"]["ci_pytest_passed"])
            self.assertTrue(report["observed_offline_evidence"]["ci_real_redis_passed"])
            self.assertEqual(len(report["outstanding_proofs"]),8)
            self.assertFalse(report["render_deploy_authorized"])

if __name__=="__main__":unittest.main()
