"""Synthetic evidence traverses actual finalizer, replay, and fenced committer."""
import unittest
from datetime import timedelta
from types import SimpleNamespace

from live_sip_soak import LiveSemanticLedger, build_evidence
from sip_epoch_capture import BoundedEpochCapture
from sip_drain_coordinator import BoundedSIPDrainCoordinator
from verified_session_recovery import recover_and_commit_verified_empty_session
from test_step3z_native_session_recovery_batches import REST, START, END, AS_OF
from test_step3t_recovery_commit_permit import Leadership
from test_step3ag_recovery_session_finalizer import scope_snapshot


def synthetic_evidence():
    rest=REST()
    one,_,_=rest.native_recovery_batch_audited(['A'],START,END)
    capture=BoundedEpochCapture();capture.start(1);capture.begin_drain(1)
    ledger=LiveSemanticLedger(['A'],'2026-09-24',START.isoformat(),END.isoformat())
    for row in one['A']:
        capture.ingest(1,{'T':'b',**row},received_at=AS_OF)
    drain=BoundedSIPDrainCoordinator(capture,Leadership(),ledger.reconcile,batch_size=16)
    drain.drain_available(1,max_batches=8)
    count=len(one['A'])
    terminal={'schema':'OPR_SIP_EPOCH_TERMINAL_V1','epoch':1,
        'subscription_ack_verified':True,'failure_class':'OTHER_OR_CANCELLED',
        'received':count,'handled':count,'market_data_received':count,
        'known_control_received':0,'unknown_nonmarket_received':0,
        'unknown_nonmarket_types':{},'received_not_confirmed_handled':0,
        'capture_before_teardown':capture.snapshot(),
        'dispatch_queue':{'limit':1024,'depth':0,'high_water':count,'overflows':0}}
    return build_evidence(started_at=(START-timedelta(minutes=10)).isoformat(),
        ended_at=AS_OF.isoformat(),duration_requested=2400,symbols_count=1,
        runtime=SimpleNamespace(last_epoch_diagnostic=terminal),drain=drain,ledger=ledger,
        terminal_error=None,subscription_ack_at=(START-timedelta(minutes=9)).isoformat(),
        expected_session_start=START.isoformat(),expected_session_end=END.isoformat())


class RecordingLua:
    def __init__(self):self.calls=[]
    def atomic_recovery_eb(self,worker,records,generation,**kwargs):
        self.calls.append((worker,records,generation))
        return {'inserted':len(records),'already_identical':0}


class TestVerifiedRecovery(unittest.TestCase):
    def test_unmocked_replay_produces_nonempty_canonical_records(self):
        lua=RecordingLua();leader=Leadership()
        result=recover_and_commit_verified_empty_session(synthetic_evidence(),REST(),['A'],
            scope_snapshot(leader),leader,lua,as_of=AS_OF)
        self.assertGreater(result['commit']['inserted'],0)
        self.assertTrue(lua.calls)
        self.assertFalse(result['direct_handoff_authorized'])
        self.assertFalse(result['retroactive_entries_allowed'])

    def test_invalid_evidence_never_reaches_writer(self):
        lua=RecordingLua();e=synthetic_evidence();e['terminal']['received']+=1
        with self.assertRaises(Exception):
            recover_and_commit_verified_empty_session(e,REST(),['A'],{},Leadership(),lua,as_of=AS_OF)
        self.assertEqual(lua.calls,[])
