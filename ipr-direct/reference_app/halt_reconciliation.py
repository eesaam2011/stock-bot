class HaltStateUnknown(RuntimeError):pass
class HaltReconciler:
 def __init__(self,status_source,trade_reconciler):self.status_source=status_source;self.trade_reconciler=trade_reconciler
 def on_halt(self,trade):return {**trade,"state":"HALTED_ACTIVE","pre_halt_state":trade["state"]}
 def on_resume(self,trade):
  status=self.status_source.current(trade["symbol"])
  if status!="TRADING":raise HaltStateUnknown("HALT_STATUS_UNPROVEN")
  self.trade_reconciler.reconcile(trade)
  pre=trade.get("pre_halt_state")
  if pre not in {"ACTIVE_PRE_T1","ACTIVE_POST_T1"}:raise HaltStateUnknown("PRE_HALT_STATE_UNPROVEN")
  return {**trade,"state":pre,"pre_halt_state":None}
