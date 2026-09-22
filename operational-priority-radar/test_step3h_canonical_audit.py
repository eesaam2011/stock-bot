"""Step3H: read-only canonical E/B reconciliation of session REST/SIP preview."""
import copy,unittest
from datetime import timedelta
from unittest.mock import patch
from recovery_canonical_audit import audit_session_canonical,CanonicalAuditUnsafe
from recovery_session_preview import preview_session_overlap
from test_step2z_recovery_preview import inputs,START
from test_step2y_sip_overlap import NOW
from test_step2x_signal_replay import RecordingBase
from production_recovery import ProductionStartupRecovery
from state_store import base_record
from early_core_score import ARTIFACT

SESSION="2026-09-22"
def observed():
    events,c=inputs()
    signals,a=preview_session_overlap(
        events,c,session=SESSION,epoch=7,symbols=["A"],
        session_start=START,session_end=NOW,requested_start=START,
        as_of=NOW,base_factory=RecordingBase,
        early_score=lambda rows,end:(.9,.9))
    return signals,c

def record(s):
    typ="early_core" if s.kind=="E" else "base_ready"
    r=base_record(typ,s.session,s.symbol,
                  "FIRST_CROSSING" if s.kind=="E" else "FIRST_TRUE",
                  NOW.isoformat())
    r.update(bar_start_ts=s.bar_start_ts.isoformat(),
             bar_end_ts=s.bar_end_ts.isoformat(),
             received_at=NOW.isoformat(),
             decision_available_ts=NOW.isoformat(),
             source_timeframe="native_5Min" if s.kind=="E" else "completed_1Min",
             leader_generation=1,worker_instance_id="W",source="alpaca_sip")
    if s.kind=="E":
        r.update(score=s.score,threshold=ARTIFACT["threshold"],
                 observed_weight=s.observed_weight)
    else:
        r["features"]={**{k:s.features[k] for k in
                          ("opportunity","failure_pressure")},
                       **{k:s.diagnostics[k] for k in
                          ("price","vwap","demand_efficiency",
                           "price_acceptance","volume_acceleration")}}
    return r

class Reader:
    def __init__(self,records=None):self.records=records or {};self.calls=[]
    def get_e(self,sess,sym):
        self.calls.append((sess,sym,"E"))
        return self.records.get((sym,"E"))
    def get_b(self,sess,sym):
        self.calls.append((sess,sym,"B"))
        return self.records.get((sym,"B"))
    def active_trades(self):return []
    def earliest_decision_anchor(self,session):return None

def audit(signals,reader,**kw):
    return audit_session_canonical(signals,reader,session=SESSION,
                                    symbols=["A"],as_of=NOW,**kw)

class TestCanonicalSessionAudit(unittest.TestCase):
    def setUp(self):
        RecordingBase.seen=[]
        self.signals,self.capture=observed()
        self.records={(s.symbol,s.kind):record(s) for s in self.signals}
    def test_both_frozen_signals_match_without_writes(self):
        reader=Reader(copy.deepcopy(self.records))
        before=copy.deepcopy(reader.records)
        a=audit(self.signals,reader)
        self.assertEqual(a["matched"],["A:E","A:B"])
        self.assertEqual(a["divergent"],[])
        self.assertEqual(reader.records,before)
        self.assertEqual(a["canonical_writes"],0)
        self.assertFalse(a["canonical_backfill_authorized"])
        self.assertFalse(a["multi_key_snapshot_atomic"])
        self.assertFalse(a["first_of_session_proven"])
        self.assertEqual(reader.calls,[(SESSION,"A","E"),(SESSION,"A","B")])
    def test_missing_canonical_is_not_backfill_permission(self):
        a=audit(self.signals,Reader())
        self.assertEqual(a["observed_without_canonical"],["A:E","A:B"])
        self.assertEqual(a["canonical_writes"],0)
        self.assertFalse(a["canonical_backfill_authorized"])
    def test_existing_canonical_without_observed_is_indeterminate(self):
        a=audit((),Reader(self.records))
        self.assertEqual(a["canonical_without_observed"],["A:E","A:B"])
        self.assertEqual(a["divergent"],[])
        self.assertFalse(a["full_session_coverage_proven"])
    def test_e_score_divergence_reported(self):
        records=copy.deepcopy(self.records);records[("A","E")]["score"]+=.01
        a=audit(self.signals,Reader(records))
        self.assertEqual(a["divergent"],[
            {"signal":"A:E","reason":"E_FROZEN_METRICS_DIVERGENCE"}])
    def test_b_diagnostic_divergence_reported(self):
        records=copy.deepcopy(self.records);records[("A","B")]["features"]["price"]+=.01
        a=audit(self.signals,Reader(records))
        self.assertEqual(a["divergent"],[
            {"signal":"A:B","reason":"B_DIAGNOSTIC_DIVERGENCE"}])
    def test_b_feature_divergence_reported(self):
        records=copy.deepcopy(self.records);records[("A","B")]["features"]["opportunity"]+=.01
        a=audit(self.signals,Reader(records))
        self.assertEqual(a["divergent"],[
            {"signal":"A:B","reason":"B_FEATURE_DIVERGENCE"}])
    def test_bar_time_divergence_reported(self):
        records=copy.deepcopy(self.records)
        records[("A","E")]["bar_start_ts"]=(START+timedelta(minutes=1)).isoformat()
        records[("A","E")]["bar_end_ts"]=(START+timedelta(minutes=6)).isoformat()
        a=audit(self.signals,Reader(records))
        self.assertEqual(a["divergent"][0]["reason"],"BAR_TIME_DIVERGENCE")
    def test_canonical_wrong_symbol_or_session_fails_closed(self):
        for field,value in (("symbol","B"),("session","2026-09-21")):
            records=copy.deepcopy(self.records);records[("A","E")][field]=value
            with self.assertRaisesRegex(CanonicalAuditUnsafe,
                                         "CANONICAL_IDENTITY"):
                audit(self.signals,Reader(records))
    def test_canonical_bad_time_or_future_decision_fails_closed(self):
        for field,value in (("bar_end_ts","not-a-time"),
                            ("decision_available_ts",
                             (NOW+timedelta(seconds=1)).isoformat())):
            records=copy.deepcopy(self.records);records[("A","E")][field]=value
            with self.assertRaises(CanonicalAuditUnsafe):
                audit(self.signals,Reader(records))
    def test_canonical_wrong_frozen_threshold_fails_closed(self):
        records=copy.deepcopy(self.records)
        records[("A","E")]["threshold"]+=.01
        with self.assertRaisesRegex(CanonicalAuditUnsafe,
                                     "CANONICAL_E_METRICS_INVALID"):
            audit(self.signals,Reader(records))
    def test_canonical_invalid_b_features_fails_closed(self):
        records=copy.deepcopy(self.records)
        records[("A","B")]["features"]["price"]=float("nan")
        with self.assertRaisesRegex(CanonicalAuditUnsafe,
                                     "CANONICAL_B_FEATURES_INVALID"):
            audit(self.signals,Reader(records))
    def test_canonical_read_error_fails_closed(self):
        class Broken(Reader):
            def get_e(self,*args):raise RuntimeError("Redis unavailable")
        with self.assertRaisesRegex(CanonicalAuditUnsafe,
                                     "CANONICAL_READ_FAILED"):
            audit(self.signals,Broken())
    def test_duplicate_or_out_of_batch_signal_rejected(self):
        with self.assertRaisesRegex(CanonicalAuditUnsafe,
                                     "CANONICAL_AUDIT_INVALID_SIGNAL"):
            audit(self.signals+self.signals,Reader(self.records))
        with self.assertRaisesRegex(CanonicalAuditUnsafe,
                                     "CANONICAL_AUDIT_INVALID_BATCH"):
            audit(self.signals,Reader(self.records),max_symbols=0)
    def test_deterministic_audit_digest(self):
        reader=Reader(self.records)
        self.assertEqual(audit(self.signals,reader)["audit_sha256"],
                         audit(tuple(reversed(self.signals)),reader)["audit_sha256"])
    def test_canonical_unfenced_generation_fails_closed(self):
        records=copy.deepcopy(self.records)
        records[("A","E")]["leader_generation"]=0
        with self.assertRaisesRegex(CanonicalAuditUnsafe,
                                     "CANONICAL_IDENTITY"):
            audit(self.signals,Reader(records))

class TestStartupCanonicalComparison(unittest.TestCase):
    class REST:
        def __init__(self,events):self.events=events
        def native_recovery_batch_audited(self,symbols,start,end,**kw):
            one={s:[] for s in symbols};five={s:[] for s in symbols}
            for e in self.events:
                (one if e.timeframe=="1m" else five)[e.symbol].append(e.bar)
            return one,five,{"both_api_page_chains_exhausted":True,
                             "native_1m":{"pages":1},"native_5m":{"pages":1}}
        def native_recovery_batch(self,*a,**kw):raise AssertionError
    def test_startup_audits_missing_canonical_without_backfill(self):
        events,c=inputs()
        rec=ProductionStartupRecovery(Reader(),self.REST(events),None,SESSION,
            ["A"],now_fn=lambda:NOW,audit_session_signals=True,
            session_start=START,audit_session_overlap=True,
            sip_capture=c,sip_epoch=7,audit_session_canonical_records=True)
        result=rec.run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        a=result["fetch_audit"]
        self.assertTrue(a["session_canonical_comparison_enabled"])
        self.assertEqual(a["session_canonical_audited_batches"],1)
        self.assertFalse(a["session_canonical_backfill_authorized"])
        self.assertFalse(rec.continuity_verified(7))
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_startup_divergence_blocks_recovery(self):
        events,c=inputs()
        rec=ProductionStartupRecovery(Reader(),self.REST(events),None,SESSION,
            ["A"],now_fn=lambda:NOW,audit_session_signals=True,
            session_start=START,audit_session_overlap=True,
            sip_capture=c,sip_epoch=7,audit_session_canonical_records=True)
        with patch("production_recovery.audit_session_canonical",
                   return_value={"divergent":[{"signal":"A:E","reason":"BAR_TIME_DIVERGENCE"}]}):
            result=rec.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertFalse(result["gap_recovered"])
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_startup_rejects_canonical_audit_without_overlap(self):
        events,c=inputs()
        rec=ProductionStartupRecovery(Reader(),self.REST(events),None,SESSION,
            ["A"],now_fn=lambda:NOW,audit_session_signals=True,
            session_start=START,audit_session_canonical_records=True)
        result=rec.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertFalse(rec.ready_after_stream())

if __name__=="__main__":unittest.main()
