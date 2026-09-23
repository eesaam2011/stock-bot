"""Machine-readable offline acceptance report; cannot authorize live Shadow.

CI is evidence of isolated correctness, not live SIP 407 or independent
full-session market-data completeness. This module never reads secrets,
deploys, merges, or modifies Redis.
"""
import argparse,json
from pathlib import Path

REQUIRED_EXTERNAL_PROOFS=(
    "independent_native_1m_5m_session_coverage",
    "independent_no_trade_and_halt_evidence",
    "sip_epoch_continuity_with_active_producer",
    "bounded_zero_loss_drain_ack_direct_handoff",
    "generation_fenced_production_canonical_eb_replay",
    "active_trade_and_halted_trade_reconciliation",
    "live_alpaca_sip_407_market_session_soak",
    "render_runtime_configuration_and_deployment_review",
)
class AcceptanceUnsafe(RuntimeError):pass

def assess_shadow_readiness(*,ci_evidence=None,recovery_audit=None,
                            capture_snapshot=None,external_artifacts=None):
    ci=ci_evidence or {}
    recovery=recovery_audit or {}
    capture=capture_snapshot or {}
    external=external_artifacts or {}
    # No self-reported boolean, test mock or fabricated path can serve as
    # independent production proof. Actual evidence needs a separate
    # provenance verifier and a reviewed, generation-fenced handoff.
    observed={
        "ci_pytest_passed":ci.get("pytest_passed") is True,
        "ci_real_redis_passed":ci.get("real_redis_passed") is True,
        "rest_page_chains_exhausted":recovery.get("api_page_chains_exhausted") is True,
        "native_slot_audited":recovery.get("native_slot_audited_batches",0)>0,
        "sip_capture_draining":capture.get("phase")=="DRAINING",
        "sip_epoch_unacknowledged":capture.get("acked_upto")==0,
        "unverified_external_artifacts_supplied":sorted(
            k for k,v in external.items() if v),
    }
    blockers=list(REQUIRED_EXTERNAL_PROOFS)
    if not observed["ci_pytest_passed"]:blockers.insert(0,"ci_pytest_not_verified")
    if not observed["ci_real_redis_passed"]:blockers.insert(0,"ci_real_redis_not_verified")
    if not observed["rest_page_chains_exhausted"]:
        blockers.insert(0,"rest_page_chains_not_verified")
    if not observed["native_slot_audited"]:
        blockers.insert(0,"native_slot_diagnostics_not_run")
    if not observed["sip_capture_draining"]:
        blockers.insert(0,"sip_drain_not_observed")
    return {
        "schema":"OPR_SHADOW_ACCEPTANCE_V1",
        "status":"NOT_AUTHORIZED_FOR_LIVE_SHADOW",
        "draft_pr_required":True,
        "merge_authorized":False,
        "render_deploy_authorized":False,
        "canonical_eb_backfill_authorized":False,
        "sip_direct_handoff_authorized":False,
        "retroactive_entry_authorized":False,
        "observed_offline_evidence":observed,
        "external_proof_verification_implemented":False,
        "outstanding_proofs":list(REQUIRED_EXTERNAL_PROOFS),
        "blockers":blockers,
        "next_action":"Collect and independently verify real session/407, halt/no-trade, active-trade, and SIP handoff traces; implement reviewed production gates before re-assessment.",
    }

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",default="SHADOW_READINESS_REPORT.json")
    args=parser.parse_args()
    report=assess_shadow_readiness()
    path=Path(args.output)
    path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",
                    encoding="utf-8")
    print(f"SHADOW_READINESS={report['status']}")
    print(f"OUTSTANDING_EXTERNAL_PROOFS={len(report['outstanding_proofs'])}")
    print(f"REPORT_PATH={path}")
    return 0

if __name__=="__main__":raise SystemExit(main())
