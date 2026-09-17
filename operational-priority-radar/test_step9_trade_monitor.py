import unittest
from datetime import datetime,timezone,timedelta
from trade_monitor import *
T=datetime(2026,9,16,15,0,tzinfo=timezone.utc)
def S(state="ACTIVE_PRE_T1"):
    return TradeState(state,10,9.5,10.5,11,T+timedelta(minutes=120))
def E(kind,mins,price=None,seq=1,eligible=True):
    return MarketEvent(kind,T+timedelta(minutes=mins),seq,price,eligible)

class TestStep9TradeMonitor(unittest.TestCase):
 def test_t1_moves_post_t1(self):
  s,e=apply_event(S(),E("TRADE",1,10.5));self.assertEqual((s.state,e),("ACTIVE_POST_T1",MonitorEvent.T1))
 def test_t2_can_close_directly_pre_t1(self):
  s,e=apply_event(S(),E("TRADE",1,11));self.assertEqual((s.state,e),("CLOSED_T2",MonitorEvent.T2))
 def test_stop_pre_t1(self):
  s,e=apply_event(S(),E("TRADE",1,9.5));self.assertEqual((s.state,e),("CLOSED_STOP",MonitorEvent.STOP))
 def test_structural_stop_remains_active_post_t1(self):
  s,e=apply_event(S("ACTIVE_POST_T1"),E("TRADE",2,9.4));self.assertEqual((s.state,e),("CLOSED_STOP",MonitorEvent.STOP))
 def test_post_t1_completed_1m_close_at_entry_exits(self):
  s,e=apply_event(S("ACTIVE_POST_T1"),E("BAR_CLOSE",2,10));self.assertEqual((s.state,e),("CLOSED_POST_T1_BREAKEVEN_EXIT",MonitorEvent.POST_T1_EXIT))
 def test_pre_t1_close_below_entry_does_not_breakeven_exit(self):
  s,e=apply_event(S(),E("BAR_CLOSE",2,9.9));self.assertEqual(s.state,"ACTIVE_PRE_T1")
 def test_t2_post_t1(self):
  s,e=apply_event(S("ACTIVE_POST_T1"),E("TRADE",2,11));self.assertEqual(s.state,"CLOSED_T2")
 def test_chronology_stop_before_t2_wins(self):
  s,e=process_chronological(S("ACTIVE_POST_T1"),[E("TRADE",2,11,2),E("TRADE",2,9.4,1)])
  self.assertEqual((s.state,e),("CLOSED_STOP",MonitorEvent.STOP))
 def test_chronology_t2_before_stop_wins(self):
  s,e=process_chronological(S("ACTIVE_POST_T1"),[E("TRADE",2,9.4,2),E("TRADE",2,11,1)])
  self.assertEqual((s.state,e),("CLOSED_T2",MonitorEvent.T2))
 def test_terminal_state_immutable(self):
  s,e=apply_event(S("CLOSED_STOP"),E("TRADE",3,11));self.assertEqual((s.state,e),("CLOSED_STOP",MonitorEvent.NONE))
 def test_halt_preserves_pre_t1(self):
  s,e=apply_event(S(),E("HALT",1));self.assertEqual((s.state,s.pre_halt_state),("HALTED_ACTIVE","ACTIVE_PRE_T1"))
 def test_halt_preserves_post_t1(self):
  s,e=apply_event(S("ACTIVE_POST_T1"),E("HALT",1));self.assertEqual(s.pre_halt_state,"ACTIVE_POST_T1")
 def test_resume_restores_pre_halt_state(self):
  h,_=apply_event(S("ACTIVE_POST_T1"),E("HALT",1));r,e=apply_event(h,E("RESUME",2));self.assertEqual((r.state,e),("ACTIVE_POST_T1",MonitorEvent.RESUME))
 def test_market_events_ignored_while_halted(self):
  h,_=apply_event(S(),E("HALT",1));r,e=apply_event(h,E("TRADE",2,11));self.assertEqual((r.state,e),("HALTED_ACTIVE",MonitorEvent.NONE))
 def test_ambiguous_recovery_is_terminal_ambiguous(self):
  s,e=apply_event(S(),E("RECOVERY_AMBIGUOUS",2));self.assertEqual((s.state,e),("RECOVERY_PATH_AMBIGUOUS",MonitorEvent.RECOVERY_AMBIGUOUS))
 def test_after_deadline_is_monitoring_expired_not_closed_timeout(self):
  s,e=apply_event(S(),E("TRADE",121,10.2));self.assertEqual((s.state,e),("MONITORING_EXPIRED",MonitorEvent.MONITORING_EXPIRED))
 def test_event_at_exact_deadline_still_processed(self):
  s,e=apply_event(S(),E("TRADE",120,11));self.assertEqual((s.state,e),("CLOSED_T2",MonitorEvent.T2))
 def test_ineligible_trade_cannot_trigger_stop_or_target(self):
  s,e=apply_event(S(),E("TRADE",1,9,eligible=False));self.assertEqual((s.state,e),("ACTIVE_PRE_T1",MonitorEvent.NONE))
 def test_same_timestamp_sequence_controls_first_terminal(self):
  a=MarketEvent("TRADE",T+timedelta(minutes=1),20,9.4,True)
  b=MarketEvent("TRADE",T+timedelta(minutes=1),21,11,True)
  s,e=process_chronological(S("ACTIVE_POST_T1"),[b,a]);self.assertEqual(s.state,"CLOSED_STOP")
 def test_no_fill_claim_in_state_machine(self):
  self.assertFalse(hasattr(MonitorEvent,"FILLED_STOP"));self.assertFalse(hasattr(MonitorEvent,"FILLED_T2"))

if __name__=="__main__":unittest.main()
