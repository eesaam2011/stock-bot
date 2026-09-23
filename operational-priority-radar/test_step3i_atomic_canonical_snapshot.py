"""Step3I: atomic E/B snapshot read, completeness, and SIP race fail-closed."""
import copy,unittest
from recovery_canonical_audit import audit_session_canonical,CanonicalAuditUnsafe
from production_recovery import ProductionStartupRecovery,RedisCanonicalReader,RecoveryFailure
from test_step3h_canonical_audit import observed,record,Reader,SESSION,NOW
from test_step2z_recovery_preview import inputs,START

class AtomicReader(Reader):
    def __init__(self,records=None):
        super().__init__(records);self.snapshot_calls=[]
    def get_eb_snapshot(self,session,symbols):
        self.snapshot_calls.append((session,tuple(symbols)))
        return {(s,k):self.records.get((s,k))
                for s in symbols for k in ("E","B")}
    def get_e(self,*a):raise AssertionError("serial E read forbidden")
    def get_b(self,*a):raise AssertionError("serial B read forbidden")

class TestAtomicCanonicalSnapshot(unittest.TestCase):
    def setUp(self):
        self.signals,_=observed()
        self.records={(s.symbol,s.kind):record(s) for s in self.signals}
    def test_atomic_snapshot_match_and_no_write(self):
        r=AtomicReader(copy.deepcopy(self.records))
        a=audit_session_canonical(self.signals,r,session=SESSION,
            symbols=["A"],as_of=NOW,require_atomic_snapshot=True)
        self.assertEqual(a["matched"],["A:E","A:B"])
        self.assertTrue(a["multi_key_snapshot_atomic"])
        self.assertEqual(a["snapshot_scope"],"EB_KEYS_ONLY")
        self.assertEqual(r.snapshot_calls,[(SESSION,("A",))])
        self.assertEqual(r.records,self.records)
        self.assertEqual(a["canonical_writes"],0)
        self.assertFalse(a["direct_handoff_authorized"])
    def test_missing_atomic_snapshot_capability_fails_closed(self):
        with self.assertRaisesRegex(CanonicalAuditUnsafe,"CANONICAL_AUDIT_INVALID_BATCH"):
            audit_session_canonical(self.signals,Reader(self.records),
                session=SESSION,symbols=["A"],as_of=NOW,
                require_atomic_snapshot=True)
    def test_incomplete_snapshot_fails_closed(self):
        class Incomplete(AtomicReader):
            def get_eb_snapshot(self,*a):return {("A","E"):self.records[("A","E")]}
        with self.assertRaisesRegex(CanonicalAuditUnsafe,"CANONICAL_SNAPSHOT_INCOMPLETE"):
            audit_session_canonical(self.signals,Incomplete(self.records),
                session=SESSION,symbols=["A"],as_of=NOW,
                require_atomic_snapshot=True)
    def test_snapshot_exception_fails_closed(self):
        class Broken(AtomicReader):
            def get_eb_snapshot(self,*a):raise TimeoutError("redis")
        with self.assertRaisesRegex(CanonicalAuditUnsafe,"CANONICAL_SNAPSHOT_READ_FAILED"):
            audit_session_canonical(self.signals,Broken(self.records),
                session=SESSION,symbols=["A"],as_of=NOW,
                require_atomic_snapshot=True)
    def test_atomic_snapshot_divergence_is_not_write_permission(self):
        records=copy.deepcopy(self.records)
        records[("A","E")]["score"]+=.01
        a=audit_session_canonical(self.signals,AtomicReader(records),
            session=SESSION,symbols=["A"],as_of=NOW,
            require_atomic_snapshot=True)
        self.assertTrue(a["multi_key_snapshot_atomic"])
        self.assertEqual(a["divergent"][0]["reason"],"E_FROZEN_METRICS_DIVERGENCE")
        self.assertFalse(a["canonical_backfill_authorized"])

class TestStartupAtomicSnapshot(unittest.TestCase):
    class REST:
        def __init__(self,events):self.events=events
        def native_recovery_batch_audited(self,symbols,start,end,**kw):
            one={s:[] for s in symbols};five={s:[] for s in symbols}
            for e in self.events:
                (one if e.timeframe=="1m" else five)[e.symbol].append(e.bar)
            return one,five,{"both_api_page_chains_exhausted":True,
                "native_1m":{"pages":1},"native_5m":{"pages":1}}
        def native_recovery_batch(self,*a,**kw):raise AssertionError
    def rec(self,reader,events,c,**kw):
        return ProductionStartupRecovery(reader,self.REST(events),None,SESSION,
            ["A"],now_fn=lambda:NOW,audit_session_signals=True,
            session_start=START,audit_session_overlap=True,
            sip_capture=c,sip_epoch=7,
            audit_session_canonical_records=True,
            atomic_canonical_snapshot=True,**kw)
    def test_startup_atomic_missing_records_still_no_trust(self):
        events,c=inputs()
        r=self.rec(AtomicReader(),events,c)
        result=r.run()
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        a=result["fetch_audit"]
        self.assertTrue(a["session_canonical_multi_key_snapshot_atomic"])
        self.assertEqual(a["session_canonical_snapshot_scope"],"EB_KEYS_ONLY")
        self.assertEqual(a["session_observed_without_canonical"],2)
        self.assertFalse(a["session_canonical_backfill_authorized"])
        self.assertFalse(r.continuity_verified(7))
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_mid_canonical_read_sip_append_blocks_result(self):
        events,c=inputs()
        class Racing(AtomicReader):
            def get_eb_snapshot(self,session,symbols):
                c.ingest(7,{"T":"t","S":"A","t":"2026-09-22T16:00:21Z","p":10.4})
                return super().get_eb_snapshot(session,symbols)
        r=self.rec(Racing(),events,c)
        result=r.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertFalse(result["gap_recovered"])
        self.assertEqual(c.snapshot()["acked_upto"],0)
    def test_mid_canonical_read_disconnect_blocks_result(self):
        events,c=inputs()
        class Racing(AtomicReader):
            def get_eb_snapshot(self,session,symbols):
                c.invalidate("407")
                return super().get_eb_snapshot(session,symbols)
        r=self.rec(Racing(),events,c)
        result=r.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertFalse(r.continuity_verified(7))
    def test_atomic_mode_without_capability_fails_before_rest(self):
        events,c=inputs()
        rest=self.REST(events)
        r=ProductionStartupRecovery(Reader(),rest,None,SESSION,
            ["A"],now_fn=lambda:NOW,audit_session_signals=True,
            session_start=START,audit_session_overlap=True,
            sip_capture=c,sip_epoch=7,audit_session_canonical_records=True,
            atomic_canonical_snapshot=True)
        result=r.run()
        self.assertEqual(result["reason"],"RecoveryFailure")
        self.assertFalse(r.ready_after_stream())

if __name__=="__main__":unittest.main()
