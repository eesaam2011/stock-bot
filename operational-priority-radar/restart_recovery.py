class RestartRecoveryError(RuntimeError):pass
class CanonicalRestartRecovery:
 def __init__(self,store,native5,one_minute,trade_reconciler):self.store=store;self.native5=native5;self.one=one_minute;self.trade_reconciler=trade_reconciler
 def recover_symbol(self,session,symbol):
  # Existing canonical first events are authoritative.
  e=self.store.get_e(session,symbol);b=self.store.get_b(session,symbol);trade=self.store.get_trade(session,symbol)
  if trade:self.trade_reconciler.reconcile(trade)  # P0 first
  if e is None:e=self.native5.reconstruct_first_e_chronologically(session,symbol)
  if b is None:b=self.one.reconstruct_first_b_chronologically(session,symbol)
  return {"e":e,"b":b,"trade":trade}
