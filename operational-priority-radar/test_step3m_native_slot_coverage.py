"""Step3M: no-trade-vs-feed-loss ambiguity and native slot diagnostics."""
import unittest
from datetime import datetime,timedelta,timezone
from recovery_chronology import plan_native_batch
from recovery_slot_coverage import audit_native_session_slots,SlotCoverageUnsafe
import test_step3g_session_sip_preview as bridge
import test_step2z_recovery_preview as preview_fixture
from test_step2y_sip_overlap import NOW
from production_recovery import ProductionStartupRecovery

START=datetime(2026,9,22,15,45,tzinfo=timezone.utc)
END=START+timedelta(minutes=10)
def bar(start,native=False):
    x={"t":start.isoformat(),"o":10,"h":11,"l":9,"c":10,"v":100}
    if native:x["_timeframe"]="native_5Min"
    return x
def events(one=tuple(range(10)),five=(0,5),end=END):
    r1={"A":[bar(START+timedelta(minutes=i)) for i in one]}
    r5={"A":[bar(START+timedelta(minutes=i),True) for i in five]}
    return plan_native_batch(r1,r5,["A"],window_start=START,
        window_end=end,recovered_at=end)[0]
def audit(ev,**kw):
    return audit_native_session_slots(ev,["A"],session_start=START,
        rest_cutoff=END,**kw)

class TestNativeSlotDiagnostics(unittest.TestCase):
    def test_full_observation_still_never_proves_coverage(self):
        a=audit(events())
        self.assertEqual(a["observed_native_1m_slots"],10)
        self.assertEqual(a["observed_native_5m_slots"],2)
        self.assertEqual(a["cross_timeframe_observation_mismatches"],0)
        self.assertEqual(a["unobserved_1m_slots_unknown_cause"],0)
        self.assertFalse(a["full_session_coverage_proven"])
        self.assertFalse(a["canonical_backfill_authorized"])
        self.assertEqual(a["canonical_writes"],0)
        self.assertEqual(len(a["slot_observation_sha256"]),64)
    def test_missing_one_minute_is_unknown_not_proven_feed_loss(self):
        a=audit(events(one=(0,1,2,3,5,6,7,8,9)))
        self.assertEqual(a["unobserved_1m_slots_unknown_cause"],1)
        self.assertEqual(a["cross_timeframe_observation_mismatches"],0)
        self.assertTrue(a["rest_pagination_exhausted_is_not_coverage_proof"])
    def test_missing_native5_with_native1_is_diagnostic_mismatch(self):
        a=audit(events(five=(0,)))
        self.assertEqual(a["cross_timeframe_observation_mismatches"],1)
        self.assertEqual(a["unobserved_5m_slots_unknown_cause"],1)
        self.assertEqual(a["diagnostic_sample"][0]["classification"],
                         "CROSS_TIMEFRAME_OBSERVATION_MISMATCH")
        self.assertFalse(a["full_session_coverage_proven"])
    def test_missing_all_1m_with_native5_is_mismatch(self):
        a=audit(events(one=()))
        self.assertEqual(a["cross_timeframe_observation_mismatches"],2)
        self.assertEqual(a["unobserved_1m_slots_unknown_cause"],10)
    def test_both_absent_is_unknown_cause_not_no_trade_proof(self):
        a=audit(events(one=tuple(range(5)),five=(0,)))
        self.assertEqual(a["both_timeframes_absent_5m_windows_unknown_cause"],1)
        self.assertEqual(a["cross_timeframe_observation_mismatches"],0)
        self.assertEqual(a["diagnostic_sample"][0]["classification"],
                         "BOTH_ABSENT_CAUSE_UNKNOWN")
    def test_digest_is_order_independent(self):
        ev=events()
        self.assertEqual(audit(ev)["slot_observation_sha256"],
                         audit(tuple(reversed(ev)))["slot_observation_sha256"])
    def test_duplicate_native_event_fails_closed(self):
        ev=events()
        with self.assertRaisesRegex(SlotCoverageUnsafe,"DUPLICATE"):
            audit(ev+(ev[0],))
    def test_outside_symbol_fails_closed(self):
        ev=events()
        other=type(ev[0])("B",ev[0].timeframe,ev[0].start,
                         ev[0].end,ev[0].bar,ev[0].recovered_at)
        with self.assertRaisesRegex(SlotCoverageUnsafe,"UNEXPECTED_EVENT"):
            audit(ev+(other,))
    def test_misaligned_session_and_oversize_window_fail(self):
        ev=events()
        for start,end in ((START+timedelta(minutes=1),END),
                          (START,START+timedelta(minutes=1441)),
                          (START,START)):
            with self.assertRaisesRegex(SlotCoverageUnsafe,"INVALID_WINDOW"):
                audit_native_session_slots(ev,["A"],session_start=start,
                                           rest_cutoff=end)
    def test_partial_trailing_minute_not_expected(self):
        ev=events()
        a=audit_native_session_slots(ev,["A"],session_start=START,
                                     rest_cutoff=END+timedelta(seconds=40))
        self.assertEqual(a["complete_1m_slots_per_symbol"],10)
        self.assertEqual(a["complete_5m_slots_per_symbol"],2)
    def test_sample_bounded_and_invalid_batch_refused(self):
        a=audit(events(one=(),five=()),max_sample=1)
        self.assertEqual(len(a["diagnostic_sample"]),1)
        with self.assertRaisesRegex(SlotCoverageUnsafe,"INVALID_BATCH"):
            audit_native_session_slots(events(),["A","A"],
                session_start=START,rest_cutoff=END)
    def test_startup_opt_in_diagnostics_remain_untrusted(self):
        ev,c=preview_fixture.inputs()
        rec=ProductionStartupRecovery(
            bridge.TestStartupSessionSIPBridge.Reader(),
            bridge.TestStartupSessionSIPBridge.REST(ev),None,
            "2026-09-22",["A"],now_fn=lambda:NOW,
            session_start=preview_fixture.START,audit_native_slots=True)
        result=rec.run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        a=result["fetch_audit"]
        self.assertTrue(a["native_slot_audit_enabled"])
        self.assertEqual(a["native_slot_audited_batches"],1)
        self.assertGreater(a["native_slot_unknown_missing_1m"],0)
        self.assertFalse(a["full_session_coverage_proven"])
        self.assertFalse(rec.continuity_verified(7))
    def test_startup_opt_in_requires_audited_rest_before_fetch(self):
        class Unverified:
            def native_recovery_batch(self,*a,**kw):
                raise AssertionError("REST must not be called")
        rec=ProductionStartupRecovery(
            bridge.TestStartupSessionSIPBridge.Reader(),
            Unverified(),None,"2026-09-22",["A"],
            now_fn=lambda:NOW,session_start=START,audit_native_slots=True)
        self.assertEqual(rec.run()["reason"],"RecoveryFailure")
    def test_startup_opt_in_requires_explicit_session_start(self):
        ev,_=preview_fixture.inputs()
        rest=bridge.TestStartupSessionSIPBridge.REST(ev)
        rec=ProductionStartupRecovery(
            bridge.TestStartupSessionSIPBridge.Reader(),rest,None,
            "2026-09-22",["A"],now_fn=lambda:NOW,
            audit_native_slots=True)
        self.assertEqual(rec.run()["reason"],"RecoveryFailure")
        self.assertEqual(rest.calls,[])

if __name__=="__main__":unittest.main()
