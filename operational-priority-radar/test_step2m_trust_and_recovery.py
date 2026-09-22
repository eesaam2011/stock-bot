"""Step 2M regression: REST fetch != replay; epoch-specific SIP trust."""
import asyncio
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace
from market_trust import Trust,TrustState
from production_recovery import ProductionStartupRecovery
from runtime_wiring import RuntimeComponents,RuntimeOrchestrator,RuntimeFailClosed
from trust_gate import require_live_trust_proof,ContinuityUnproven

NOW=datetime(2026,9,22,16,tzinfo=timezone.utc)
class Reader:
    def active_trades(self):return []
    def earliest_decision_anchor(self,session):return None
class Rest:
    def native_recovery_batch(self,symbols,start,end,**kw):
        return ({s:[{"t":"2026-09-22T15:59:00Z"}] for s in symbols},
                {s:[{"t":"2026-09-22T15:55:00Z"}] for s in symbols})
class Leader:
    def __init__(self):self.current=True;self.checks=0
    def require_current(self):
        self.checks+=1
        if not self.current:raise RuntimeError("NOT_CURRENT_LEADER")

class TestStep2M(unittest.TestCase):
    def test_fetch_only_never_claims_gap_recovery(self):
        rec=ProductionStartupRecovery(Reader(),Rest(),None,"2026-09-22",["ABC"],now_fn=lambda:NOW)
        result=rec.run()
        self.assertFalse(result["gap_recovered"])
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["reason"],"CANONICAL_REPLAY_NOT_IMPLEMENTED")
        self.assertFalse(result["fetch_audit"]["replay_completed"])
        self.assertFalse(rec.ready_after_stream())
        self.assertFalse(rec.continuity_verified(1))
    def test_disconnect_invalidates_fetch_audit(self):
        rec=ProductionStartupRecovery(Reader(),Rest(),None,"2026-09-22",["ABC"],now_fn=lambda:NOW)
        rec.run();self.assertIsNotNone(rec._fetch_audit)
        rec.on_disconnect()
        self.assertIsNone(rec._fetch_audit)
        self.assertFalse(rec.ready_after_stream())
    def test_orchestrator_rejects_unproven_epoch(self):
        rec=SimpleNamespace(continuity_verified=lambda epoch:False,ready_after_stream=lambda:True)
        c=RuntimeComponents(None,None,None,None,None,None,SimpleNamespace(new_entries_allowed=lambda:True),rec)
        orch=RuntimeOrchestrator(c,"W")
        orch.trust=Trust(TrustState.VERIFYING_CONTINUITY,True,True,False,False)
        with self.assertRaisesRegex(RuntimeFailClosed,"CONTINUITY_UNPROVEN"):
            orch.finish_reconciliation(continuity_ok=True,epoch=3)
        self.assertFalse(orch.new_decisions_allowed())
    def test_legacy_startup_cannot_bypass_epoch_gate(self):
        rec=SimpleNamespace(continuity_verified=lambda epoch:True,ready_after_stream=lambda:True)
        c=RuntimeComponents(None,None,None,None,None,None,SimpleNamespace(new_entries_allowed=lambda:True),rec)
        orch=RuntimeOrchestrator(c,"W")
        with self.assertRaisesRegex(RuntimeFailClosed,"LEGACY_STARTUP_RECOVERY_UNSAFE"):
            orch.startup_recovery()
    def test_epoch_and_direct_handoff_required(self):
        connected=asyncio.Event();connected.set()
        capture=SimpleNamespace(DIRECT="DIRECT",phase="CAPTURING",buffer=SimpleNamespace(epoch=4))
        ws=SimpleNamespace(connected_event=connected,connection_epoch=4,epoch_capture=capture)
        rec=SimpleNamespace(ready_after_stream=lambda:True,continuity_verified=lambda epoch:True)
        with self.assertRaisesRegex(ContinuityUnproven,"SIP_DIRECT_HANDOFF_UNPROVEN"):
            require_live_trust_proof(ws,rec,Leader(),4)
        capture.phase="DIRECT"
        with self.assertRaisesRegex(ContinuityUnproven,"SIP_CONNECTION_EPOCH_CHANGED"):
            require_live_trust_proof(ws,rec,Leader(),3)
    def test_leader_loss_during_proof_rejected(self):
        leader=Leader();connected=asyncio.Event();connected.set()
        capture=SimpleNamespace(DIRECT="DIRECT",phase="DIRECT",buffer=SimpleNamespace(epoch=4))
        ws=SimpleNamespace(connected_event=connected,connection_epoch=4,epoch_capture=capture)
        def verify(epoch):
            leader.current=False
            return True
        rec=SimpleNamespace(ready_after_stream=lambda:True,continuity_verified=verify)
        with self.assertRaisesRegex(RuntimeError,"NOT_CURRENT_LEADER"):
            require_live_trust_proof(ws,rec,leader,4)

if __name__=="__main__":unittest.main()
