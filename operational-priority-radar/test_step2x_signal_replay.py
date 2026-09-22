"""Step 2X: real native-bar replay path, deterministic seams, never a trust proof."""
import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from recovery_chronology import plan_native_batch,ChronologyUnsafe
from recovery_signal_replay import reconstruct_window_signals

NOW=datetime(2026,9,22,16,0,tzinfo=timezone.utc)
START=NOW-timedelta(minutes=65)
def row(start,tf="1m",close=10):
    r={"t":start.isoformat(),"o":10,"h":max(11,close),
       "l":9,"c":close,"v":100,"n":10}
    if tf=="5m":r["_timeframe"]="native_5Min"
    return r
def plan():
    r1=[row(START+timedelta(minutes=i)) for i in range(35)]
    r5=[row(START+timedelta(minutes=5*i),"5m") for i in range(12)]
    return plan_native_batch({"A":r1},{"A":r5},["A"],
        window_start=START,window_end=NOW,recovered_at=NOW)[0]

class RecordingBase:
    seen=[]
    def __init__(self):self.count=0
    def on_completed_native_1m(self,symbol,bar,received_at):
        self.count+=1
        self.seen.append(bar["t"])
        if self.count==3:
            return {"base_ready":True,"features":{"opportunity":90},
                    "diagnostics":{"price":10}}
        return {"base_ready":False}
class RecordingEarly:
    seen=[]
    def __init__(self,provider):self.provider=provider
    def first_crossing(self,symbol,session,start,end,received_at):
        rows=self.provider.fetch_native_5min()
        self.seen.append((symbol,session,[r["t"] for r in rows],received_at))
        return SimpleNamespace(bar_start_ts=start,
            bar_end_ts=start+timedelta(minutes=5),
            score=.6,observed_weight=.9)

class TestRecoveredSignals(unittest.TestCase):
    def setUp(self):RecordingBase.seen=[];RecordingEarly.seen=[]
    def test_native_eb_reconstructed_in_bar_time_but_available_only_now(self):
        signals,a=reconstruct_window_signals(plan(),"2026-09-22",
            recovered_at=NOW,base_factory=RecordingBase,
            early_engine_factory=RecordingEarly)
        self.assertEqual([(s.kind,s.bar_end_ts) for s in signals],
            [("B",START+timedelta(minutes=3)),
             ("E",START+timedelta(minutes=5))])
        self.assertEqual([s.decision_available_ts for s in signals],[NOW,NOW])
        self.assertEqual(len(RecordingBase.seen),3)
        self.assertEqual(len(RecordingEarly.seen[0][2]),12)
        self.assertEqual((a["window_local_E"],a["window_local_B"]),(1,1))
        self.assertFalse(a["first_of_session_proven"])
        self.assertFalse(a["continuity_proven"])
        self.assertFalse(a["retroactive_entries_allowed"])
        self.assertEqual(a["canonical_writes"],0)
    def test_actual_frozen_engines_run_without_injected_thresholds(self):
        signals,a=reconstruct_window_signals(plan(),"2026-09-22",recovered_at=NOW)
        self.assertEqual(a["native_events_consumed"],47)
        self.assertTrue(all(s.decision_available_ts==NOW for s in signals))
        self.assertTrue(all(s.kind in {"B","E"} for s in signals))
        self.assertFalse(a["sip_merged"])
    def test_order_independent(self):
        p=plan()
        a,da=reconstruct_window_signals(p,"S",recovered_at=NOW,
            base_factory=RecordingBase,early_engine_factory=RecordingEarly)
        b,db=reconstruct_window_signals(tuple(reversed(p)),"S",recovered_at=NOW,
            base_factory=RecordingBase,early_engine_factory=RecordingEarly)
        self.assertEqual(a,b)
        self.assertEqual(da,db)
    def test_duplicate_rejected(self):
        p=plan()
        with self.assertRaisesRegex(ChronologyUnsafe,"REPLAY_DUPLICATE_EVENT"):
            reconstruct_window_signals(p+(p[0],),"S",recovered_at=NOW)
    def test_stale_epoch_recovery_time_rejected(self):
        with self.assertRaisesRegex(ChronologyUnsafe,"REPLAY_EPOCH_OR_TIMEFRAME_MISMATCH"):
            reconstruct_window_signals(plan(),"S",recovered_at=NOW+timedelta(seconds=1))
    def test_replay_bounded(self):
        with self.assertRaisesRegex(ChronologyUnsafe,"REPLAY_INVALID_INPUT_OR_LIMIT"):
            reconstruct_window_signals(plan(),"S",recovered_at=NOW,max_events=3)
    def test_unproven_native5_rejected(self):
        p=list(plan());e=next(x for x in p if x.timeframe=="5m")
        e.bar.pop("_timeframe")
        with self.assertRaisesRegex(ChronologyUnsafe,"REPLAY_SYNTHETIC_5M_FORBIDDEN"):
            reconstruct_window_signals(tuple(p),"S",recovered_at=NOW)
    def test_no_early_core_when_native5_lane_empty(self):
        p=tuple(x for x in plan() if x.timeframe=="1m")
        signals,a=reconstruct_window_signals(p,"S",recovered_at=NOW,
            base_factory=RecordingBase,early_engine_factory=RecordingEarly)
        self.assertEqual([s.kind for s in signals],["B"])
        self.assertFalse(RecordingEarly.seen)

if __name__=="__main__":unittest.main()
