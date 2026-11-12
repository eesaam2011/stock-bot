"""Step 3D: whole recovered trade trace must be one Redis transaction."""
import json,unittest
from datetime import timedelta
from types import SimpleNamespace
from active_trade_recovery import ActiveTradeChronologicalReconciler
from redis_lua_production import AtomicConflict
from state_store import key_trade,key_outbox
from event_ids import trade_event_id
from test_step16d_active_trade_recovery import T,REST,trade,setup

def tick(seconds,price):
    return {"t":(T+timedelta(seconds=seconds)).isoformat(),"p":price}

class TestAtomicReplay(unittest.TestCase):
    def test_t1_then_stop_one_atomic_write_two_outboxes(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1),tick(2,8.8)]))
        calls=[]
        original=rd.eval
        def spy(script,n,*args):
            calls.append((script,n))
            return original(script,n,*args)
        rd.eval=spy
        result=rec.reconcile(t)
        self.assertEqual(result["state"],"CLOSED_STOP")
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1],5) # leader, generation, trade, T1, STOP
        self.assertEqual(json.loads(rd.d[key_trade("TR")])["state"],"CLOSED_STOP")
        events=[json.loads(v)["payload"]["event"] for k,v in rd.d.items() if ":outbox:" in k]
        self.assertEqual(set(events),{"T1","STOP"})
        stop=json.loads(rd.d[key_outbox(trade_event_id("STOP","TR"))])
        self.assertEqual(stop["payload"]["first_observed_breach_price"],8.8)
        self.assertNotIn("fill_price",stop["payload"])
    def test_t1_then_t2_commits_both_outboxes(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1),tick(2,12.5)]))
        result=rec.reconcile(t)
        self.assertEqual(result["state"],"CLOSED_T2")
        for event in ("T1","T2"):
            out=json.loads(rd.d[key_outbox(trade_event_id(event,"TR"))])
            self.assertEqual(out["payload"]["event"],event)
            self.assertTrue(out["payload"]["recovered"])
    def test_second_outbox_conflict_prevents_first_and_trade(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1),tick(2,12.5)]))
        before=rd.d[key_trade("TR")]
        conflict_key=key_outbox(trade_event_id("T2","TR"))
        rd.d[conflict_key]="conflicting existing payload"
        with self.assertRaises(AtomicConflict):
            rec.reconcile(t)
        self.assertEqual(rd.d[key_trade("TR")],before)
        self.assertNotIn(key_outbox(trade_event_id("T1","TR")),rd.d)
        self.assertEqual(rd.d[conflict_key],"conflicting existing payload")
    def test_no_market_event_does_not_write(self):
        t=trade();rec,rd=setup(t,REST())
        rd.eval=lambda *a:(_ for _ in ()).throw(AssertionError("unexpected Redis write"))
        result=rec.reconcile(t)
        self.assertEqual(result["state"],"ACTIVE_PRE_T1")
    def test_first_t1_then_later_ambiguous_group_never_invokes_lua(self):
        t=trade();rec,rd=setup(t,REST([tick(1,11.1),tick(2,8.8),tick(2,12.5)]))
        before=rd.d[key_trade("TR")]
        rd.eval=lambda *a:(_ for _ in ()).throw(AssertionError("unexpected Redis write"))
        self.assertTrue(rec.reconcile(t)["ambiguous"])
        self.assertEqual(rd.d[key_trade("TR")],before)

if __name__=="__main__":unittest.main()
